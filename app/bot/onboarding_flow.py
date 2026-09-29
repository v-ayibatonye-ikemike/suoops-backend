"""Guided first-invoice onboarding flow for new WhatsApp users.

After signup, the bot proactively walks the user through creating their
first invoice step by step:

  Step 1: Ask customer name
  Step 2: Ask amount and item description
  Step 3: Ask customer phone (optional)
  Step 4: Create the invoice

Uses the same ephemeral in-memory state pattern as PendingPriceSession
and CartSession. Sessions expire after 30 minutes of inactivity.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_ONBOARDING_TTL = 1800  # 30 minutes
_REDIS_KEY_PREFIX = "bot:onboarding:"


@dataclass
class OnboardingSession:
    """Tracks a user's progress through the guided first-invoice flow."""

    user_id: int
    step: str = "customer_name"  # customer_name → amount → phone → review → done
    customer_name: str = ""
    amount: float = 0
    description: str = ""
    customer_phone: str = ""
    currency: str = "NGN"
    # Multi-line items captured at the amount step (e.g. "5000 shoe, 4000 belt").
    # When non-empty, this is the source of truth for the invoice contents;
    # ``amount``/``description`` mirror the totals/first item for backwards
    # compatibility with the review prompt.
    lines: list = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.updated_at) > _ONBOARDING_TTL

    def touch(self) -> None:
        self.updated_at = time.time()


# In-memory fallback (used only if Redis is unavailable). Onboarding
# state MUST be visible across gunicorn workers, so the canonical store
# is Redis; this dict is a best-effort fallback for local dev.
_sessions: dict[str, OnboardingSession] = {}


def _redis_key(phone: str) -> str:
    return f"{_REDIS_KEY_PREFIX}{phone}"


def _redis():
    """Return a Redis client, or None if unavailable."""
    try:
        from app.db.redis_client import get_redis_client

        return get_redis_client()
    except Exception:
        return None


def _save_session(phone: str, session: OnboardingSession) -> None:
    """Persist session to Redis (with TTL); also keep in-memory copy."""
    _sessions[phone] = session
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_redis_key(phone), _ONBOARDING_TTL, json.dumps(asdict(session)))
    except Exception:
        logger.exception("Failed to persist onboarding session for %s", phone)


def get_onboarding_session(phone: str) -> OnboardingSession | None:
    """Get active onboarding session, or None if expired/missing.

    Reads from Redis first so all gunicorn workers share state. Falls
    back to the in-process dict for local dev when Redis isn't running.
    """
    r = _redis()
    if r is not None:
        try:
            raw = r.get(_redis_key(phone))
            if raw:
                data = json.loads(raw)
                session = OnboardingSession(**data)
                if session.is_expired:
                    try:
                        r.delete(_redis_key(phone))
                    except Exception:
                        pass
                    _sessions.pop(phone, None)
                    return None
                _sessions[phone] = session
                return session
            # Redis is the source of truth — if it has no key, the
            # session is gone, even if a stale in-memory copy exists
            # on this worker.
            _sessions.pop(phone, None)
            return None
        except Exception:
            logger.exception("Failed to load onboarding session for %s", phone)

    # Redis unavailable — fall back to local dict
    session = _sessions.get(phone)
    if session and session.is_expired:
        del _sessions[phone]
        return None
    return session


def start_onboarding(phone: str, user_id: int) -> OnboardingSession:
    """Start a new onboarding session for a user."""
    session = OnboardingSession(user_id=user_id)
    _save_session(phone, session)
    return session


def _money_examples(currency: str) -> list[str]:
    """Return example amount/item strings tailored to the user's currency."""
    if (currency or "NGN").upper() == "USD":
        return ["50 wig", "150 website design", "500"]
    return ["5000 wig", "15000 website design", "50000"]


def _money_min(currency: str) -> tuple[float, str]:
    """(min amount, label) for currency-aware low-amount validation."""
    if (currency or "NGN").upper() == "USD":
        return 1.0, "$1"
    return 100.0, "₦100"


def _money_label(amount: float, currency: str) -> str:
    """Render an amount for prompts. Uses fmt_money when available."""
    try:
        from app.utils.currency_fmt import fmt_money

        return fmt_money(float(amount), (currency or "NGN").upper(), convert=False)
    except Exception:
        if (currency or "NGN").upper() == "USD":
            return f"${amount:,.2f}"
        return f"₦{amount:,.0f}"


def _lookup_customer_phone(db, user_id: int, name: str) -> str | None:
    """Return phone for an existing customer of this issuer matching `name`.

    Best-effort: silently returns None on any failure (missing db, ORM
    error, etc.) so the guided flow keeps working in tests.
    """
    if db is None or not name or not user_id:
        return None
    try:
        from sqlalchemy import func as sqlfunc

        from app.models import models

        # Match invoices issued by this user where the customer name is
        # a case-insensitive exact match. We pick the most recently
        # invoiced one to favour the freshest contact info.
        row = (
            db.query(models.Customer)
            .join(models.Invoice, models.Invoice.customer_id == models.Customer.id)
            .filter(
                models.Invoice.issuer_id == user_id,
                sqlfunc.lower(models.Customer.name) == name.strip().lower(),
                models.Customer.phone.isnot(None),
            )
            .order_by(models.Invoice.created_at.desc())
            .first()
        )
        if row and row.phone:
            return row.phone
    except Exception:
        logger.exception("customer-autocomplete lookup failed")
    return None


def _send_review(client, phone: str, session: OnboardingSession) -> None:
    """Confirm-before-send review with interactive buttons (text fallback)."""
    money = _money_label(session.amount, session.currency)
    phone_line = f"\n📱 {session.customer_phone}" if session.customer_phone else "\n📱 _no phone_"

    # Multi-item invoices get a per-line breakdown so the user can verify
    # the parser correctly split "5000 shoe, 4000 belt" into two items.
    if session.lines and len(session.lines) > 1:
        items_block = "\n".join(
            f"• {_money_label(float(li.get('unit_price') or 0), session.currency)}"
            f" — {(li.get('description') or 'Item').strip()}"
            for li in session.lines
        )
        body = (
            "📝 *Review your invoice*\n\n"
            f"👤 {session.customer_name}\n"
            f"{items_block}\n"
            f"💰 *Total: {money}*"
            f"{phone_line}\n\n"
            "Send it now?"
        )
    else:
        desc = session.description or "Service"
        body = (
            "📝 *Review your invoice*\n\n"
            f"👤 {session.customer_name}\n"
            f"💰 {money} — {desc}"
            f"{phone_line}\n\n"
            "Send it now?"
        )
    sent = False
    try:
        send_buttons = getattr(client, "send_interactive_buttons", None)
        if callable(send_buttons):
            sent = bool(
                send_buttons(
                    phone,
                    body,
                    [
                        {"id": "onb_send", "title": "✅ Send"},
                        {"id": "onb_edit", "title": "✏️ Edit"},
                        {"id": "onb_cancel", "title": "❌ Cancel"},
                    ],
                )
            )
    except Exception:
        logger.exception("failed to send review buttons")
        sent = False
    if not sent:
        client.send_text(
            phone,
            body + "\n\nReply *send*, *edit*, or *cancel*.",
        )


def start_guided_invoice(
    client,
    phone: str,
    user_id: int,
    *,
    customer_name: str | None = None,
    customer_phone: str | None = None,
    amount: float | None = None,
    description: str | None = None,
    db: Any = None,
    currency: str = "NGN",
) -> OnboardingSession:
    """Start a slot-filling invoice session, skipping pre-filled steps.

    Used when we detected invoice intent but the user's message was
    missing data (e.g. "bill Joy" without an amount). We pre-fill what
    we already know and ask only for the next missing slot, in a
    natural, friendly tone.
    """
    session = OnboardingSession(user_id=user_id)
    session.currency = (currency or "NGN").upper()
    if customer_name and customer_name.lower() not in {"customer", ""}:
        session.customer_name = customer_name
    if customer_phone:
        session.customer_phone = customer_phone
    if amount and amount > 0:
        session.amount = float(amount)
    if description:
        session.description = description

    # Auto-complete phone from history if we already have a name.
    if session.customer_name and not session.customer_phone:
        existing = _lookup_customer_phone(db, user_id, session.customer_name)
        if existing:
            session.customer_phone = existing

    _save_session(phone, session)

    examples = _money_examples(session.currency)

    # Decide first prompt based on what's still missing.
    if not session.customer_name:
        session.step = "customer_name"
        client.send_text(
            phone,
            "👋 Sure — let's get that invoice out!\n\n"
            "👤 *Who are you billing?*\n\n"
            "_Just type their name (e.g. Joy, Mrs Bello, ABC Ltd)_\n\n"
            "_Type *cancel* to stop._",
        )
    elif not session.amount:
        session.step = "amount"
        client.send_text(
            phone,
            f"👍 Got it — invoice for *{session.customer_name}*.\n\n"
            "💰 *How much, and what is it for?*\n\n"
            "_Examples:_\n"
            f"• `{examples[0]}`\n"
            f"• `{examples[1]}`\n"
            f"• `{examples[2]}`\n\n"
            "_Type *cancel* to stop._",
        )
    elif not session.customer_phone:
        session.step = "phone"
        money = _money_label(session.amount, session.currency)
        desc = f" for *{session.description}*" if session.description else ""
        client.send_text(
            phone,
            f"✅ *{session.customer_name}* — {money}{desc}.\n\n"
            "📱 *What's their phone number?*\n\n"
            "_So they get the invoice on WhatsApp._\n"
            "_Type *skip* to send without one._",
        )
    else:
        # All slots already filled — jump straight to review.
        session.step = "review"
        _save_session(phone, session)
        _send_review(client, phone, session)

    # Persist the step chosen above (customer_name / amount / phone). The earlier
    # _save_session ran BEFORE these mutations, so without this the step was lost
    # and the next reply was handled at the wrong slot — causing a double
    # "how much?" prompt and breaking the review buttons.
    _save_session(phone, session)
    return session


def clear_onboarding(phone: str) -> None:
    """Remove onboarding session from Redis and the in-memory cache."""
    _sessions.pop(phone, None)
    r = _redis()
    if r is not None:
        try:
            r.delete(_redis_key(phone))
        except Exception:
            logger.exception("Failed to clear onboarding session for %s", phone)


def send_onboarding_prompt(client, phone: str, name: str) -> None:
    """Send the initial onboarding message that starts the guided flow."""
    msg = (
        f"🎉 Let's create your *first invoice*, {name}!\n\n"
        "You can do it two ways:\n\n"
        "⚡ *Quick way — just copy, edit & send:*\n"
        "_invoice Chidi 08012345678, 5000 wig_\n\n"
        "📝 *Or step by step — just answer:*\n"
        "👤 *What's your customer's name?*\n"
        "_(e.g. Joy, Ade, Mrs Bello)_\n\n"
        "━━━━━━━━━━━━━━━━━━━━━\n"
        "💡 Type *skip* to explore on your own"
    )
    client.send_text(phone, msg)


def handle_onboarding_reply(
    session: OnboardingSession,
    client,
    phone: str,
    text: str,
    db: Any = None,
) -> dict[str, Any] | None:
    """Process user reply in the onboarding flow.

    Returns invoice data dict when all steps are complete (and the
    user has confirmed at the review step). Returns None otherwise.
    The caller is responsible for actually creating the invoice.
    """
    text = text.strip()
    text_lower = text.lower()

    # Allow user to skip/cancel at any point
    if text_lower in {"cancel", "stop"} or (
        session.step != "phone" and text_lower in {"skip", "no", "later", "not now"}
    ):
        clear_onboarding(phone)
        client.send_text(
            phone,
            "👍 No problem! You can create an invoice anytime by typing:\n\n"
            "`Invoice Joy 08012345678, 5000 wig`\n\n"
            "Type *help* for the full guide.",
        )
        return None

    session.touch()

    if session.step == "customer_name":
        result = _handle_customer_name(session, client, phone, text, db=db)
    elif session.step == "amount":
        result = _handle_amount(session, client, phone, text)
    elif session.step == "phone":
        result = _handle_phone(session, client, phone, text)
    elif session.step == "review":
        result = _handle_review(session, client, phone, text)
    else:
        return None

    # Persist any step/slot mutations made by the handler so the next
    # message (which may be served by a different gunicorn worker)
    # picks up the latest state. The completion path clears the
    # session itself, so only re-save while still mid-flow.
    if result is None:
        _save_session(phone, session)
    return result


def _handle_customer_name(
    session: OnboardingSession,
    client,
    phone: str,
    text: str,
    db: Any = None,
) -> dict[str, Any] | None:
    """Step 1: Capture customer name (with phone auto-complete from history)."""
    # Basic validation — name should be at least 2 chars
    name = text.strip()
    if len(name) < 2:
        client.send_text(
            phone, "Please enter a valid customer name (at least 2 characters).\n\n" "👤 *What's your customer's name?*"
        )
        return None

    # Don't accept numbers-only as names
    if name.replace(" ", "").isdigit():
        client.send_text(
            phone,
            "That looks like a number, not a name 😊\n\n"
            "👤 *What's your customer's name?*\n"
            "_e.g. Joy, Ade, Mrs Bello_",
        )
        return None

    session.customer_name = name
    session.step = "amount"

    # Customer auto-complete: if we already invoiced this customer,
    # remember their phone so we can skip the phone step.
    if not session.customer_phone:
        existing = _lookup_customer_phone(db, session.user_id, name)
        if existing:
            session.customer_phone = existing

    examples = _money_examples(session.currency)
    autoph = f"\n_(I'll send it to {session.customer_phone} — saved from before.)_" if session.customer_phone else ""
    client.send_text(
        phone,
        f"✅ Customer: *{name}*{autoph}\n\n"
        "💰 *How much and what is it for?*\n\n"
        "_Type the amount and item, e.g:_\n"
        f"• `{examples[0]}`\n"
        f"• `{examples[1]}`\n"
        f"• `{examples[2]}`",
    )
    return None


def _handle_amount(
    session: OnboardingSession,
    client,
    phone: str,
    text: str,
) -> dict[str, Any] | None:
    """Step 2: Capture amount and optional description.

    Supports both single-item ("5000 wig") and multi-item input
    ("5000 shoe, 4000 belt, 6000 cap"). Multi-item parsing is
    delegated to the NLP service so we share one parser everywhere.
    """
    import re
    from decimal import Decimal

    text = text.strip()

    # Multi-item detection: try the NLP line-item parser first when
    # the input looks like it might contain more than one amount.
    # We only use its output if it returns 2+ lines — otherwise we
    # fall back to the simpler single-item regex below to preserve
    # existing behaviour (and friendlier validation messages).
    multi_lines: list[dict[str, Any]] = []
    is_usd = (session.currency or "NGN").upper() == "USD"
    try:
        from app.bot.nlp_service import NLPService

        nlp = NLPService()
        candidate = nlp._extract_line_items(
            text,
            phone_variants=set(),
            is_usd=is_usd,
        )
        # Drop quantity-only items (no unit price) — those need
        # inventory lookup that the guided flow doesn't support yet.
        candidate = [c for c in candidate if c.get("unit_price") is not None]
        if len(candidate) >= 2:
            multi_lines = candidate
    except Exception:
        logger.exception("multi-item parse failed; falling back to single-item")

    if multi_lines:
        total = sum(Decimal(str(li["unit_price"])) * int(li.get("quantity") or 1) for li in multi_lines)
        min_amt, min_label = _money_min(session.currency)
        if float(total) < min_amt:
            examples = _money_examples(session.currency)
            client.send_text(
                phone, f"⚠️ Total seems too low. Minimum is {min_label}.\n\n" f"💰 *Try again:* _e.g. `{examples[0]}`_"
            )
            return None
        session.amount = float(total)
        session.lines = [
            {
                "description": str(li.get("description") or "Item"),
                "quantity": int(li.get("quantity") or 1),
                "unit_price": float(li["unit_price"]),
            }
            for li in multi_lines
        ]
        session.description = ", ".join(li["description"] for li in session.lines)

        if session.customer_phone:
            session.step = "review"
            _send_review(client, phone, session)
            return None

        session.step = "phone"
        client.send_text(
            phone,
            f"✅ {len(session.lines)} items totalling "
            f"*{_money_label(session.amount, session.currency)}*\n\n"
            "📱 *What's your customer's phone number?*\n\n"
            "_Type their number so they get the invoice on WhatsApp._\n"
            "_Or type *skip* to create without a phone number._",
        )
        return None

    # Single-item path (original behaviour)
    amount_match = re.match(
        r"^[\₦N]?\s*([\d,]+(?:\.\d{1,2})?)\s*(.*)?$",
        text,
        re.IGNORECASE,
    )

    if not amount_match:
        examples = _money_examples(session.currency)
        client.send_text(
            phone,
            "I couldn't find an amount in that.\n\n"
            "💰 *Please type the amount and what it's for:*\n"
            f"_e.g. `{examples[0]}` or `{examples[1]}`_",
        )
        return None

    amount_str = amount_match.group(1).replace(",", "")
    try:
        amount = float(amount_str)
    except ValueError:
        examples = _money_examples(session.currency)
        client.send_text(
            phone,
            "That doesn't look like a valid amount.\n\n" f"💰 *Try again:* _e.g. `{examples[0]}` or `{examples[2]}`_",
        )
        return None

    min_amt, min_label = _money_min(session.currency)
    if amount < min_amt:
        examples = _money_examples(session.currency)
        client.send_text(
            phone, f"⚠️ Amount seems too low. Minimum is {min_label}.\n\n" f"💰 *Try again:* _e.g. `{examples[0]}`_"
        )
        return None

    description = (amount_match.group(2) or "").strip()
    session.amount = amount
    session.description = description or "Service"
    session.lines = []  # single-item path — clear any prior multi-line state

    # If we already auto-completed the phone in the name step, jump
    # straight to the review screen — no need to ask again.
    if session.customer_phone:
        session.step = "review"
        _send_review(client, phone, session)
        return None

    session.step = "phone"

    desc_text = f" for *{session.description}*" if description else ""
    money = _money_label(amount, session.currency)

    client.send_text(
        phone,
        f"✅ Amount: *{money}*{desc_text}\n\n"
        "📱 *What's your customer's phone number?*\n\n"
        "_Type their number so they get the invoice on WhatsApp._\n"
        "_Or type *skip* to create without a phone number._",
    )
    return None


def _handle_phone(
    session: OnboardingSession,
    client,
    phone: str,
    text: str,
) -> dict[str, Any] | None:
    """Step 3: Capture customer phone (optional) and complete."""
    import re

    text_lower = text.lower().strip()

    customer_phone = ""
    if text_lower not in {"skip", "no", "none", "no phone", "n/a", "-"}:
        # Try to extract a phone number
        digits = re.sub(r"[^\d+]", "", text)
        if len(digits) >= 10:
            customer_phone = digits
        else:
            client.send_text(
                phone,
                "That doesn't look like a valid phone number.\n\n"
                "📱 *Enter a phone number* (e.g. 08012345678)\n"
                "_or type *skip* to continue without one_",
            )
            return None

    session.customer_phone = customer_phone

    # Move to review step instead of completing immediately. The user
    # gets a one-tap chance to confirm or fix the details before we
    # actually create + send the invoice.
    session.step = "review"
    _send_review(client, phone, session)
    return None


def _handle_review(
    session: OnboardingSession,
    client,
    phone: str,
    text: str,
) -> dict[str, Any] | None:
    """Step 4: Confirm-before-send. Returns invoice data on confirmation."""
    text_lower = text.strip().lower()
    confirm_words = {
        "send",
        "yes",
        "y",
        "ok",
        "okay",
        "confirm",
        "go",
        "go ahead",
        "✅",
        "send it",
        "onb_send",
    }
    edit_words = {"edit", "change", "fix", "modify", "✏️", "onb_edit"}
    cancel_words = {"cancel", "no", "stop", "abort", "❌", "onb_cancel"}

    if text_lower in cancel_words:
        clear_onboarding(phone)
        client.send_text(phone, "❌ Invoice cancelled. Type *invoice* to start again.")
        return None

    if text_lower in edit_words:
        # Restart at the customer-name step but keep what we know so the
        # user can quickly correct one field; cleanest UX is to walk
        # through again from the top.
        session.customer_name = ""
        session.amount = 0
        session.description = ""
        session.customer_phone = ""
        session.lines = []
        session.step = "customer_name"
        client.send_text(
            phone,
            "✏️ No problem — let's redo it.\n\n"
            "👤 *Who are you billing?*\n"
            "_Type the customer's name (or *cancel* to stop)._",
        )
        return None

    if text_lower in confirm_words:
        # Build invoice data in the format _create_invoice expects.
        if session.lines:
            lines = [
                {
                    "description": li.get("description") or "Service",
                    "quantity": int(li.get("quantity") or 1),
                    "unit_price": li.get("unit_price"),
                }
                for li in session.lines
            ]
        else:
            lines = [
                {
                    "description": session.description or "Service",
                    "quantity": 1,
                    "unit_price": session.amount,
                }
            ]
        invoice_data: dict[str, Any] = {
            "customer_name": session.customer_name,
            "customer_phone": session.customer_phone,
            "amount": session.amount,
            "lines": lines,
            "currency": (session.currency or "NGN").upper(),
        }
        clear_onboarding(phone)
        return invoice_data

    # Anything else — re-show the review with a gentle nudge.
    client.send_text(
        phone,
        "👆 Tap *✅ Send*, *✏️ Edit*, or *❌ Cancel* (or reply *send* / *edit* / *cancel*).",
    )
    return None
