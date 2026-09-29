"""Inline edit by reply.

When a user replies to a bot message with an edit command like
"make it 5000" or "change amount to 8,000", we update their most
recent *pending* invoice in place.  This is intentionally
conservative — we only allow edits while the invoice is still
``pending`` and was created within the last 30 minutes, and we only
support amount tweaks for now (the most common slip).
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from app.models import models
from app.utils.currency_fmt import fmt_money

logger = logging.getLogger(__name__)

EDIT_WINDOW_MINUTES = 30

# Matches: "make it 5000", "change amount to 5,000", "amount 5000",
# "5k instead", "actually 5000", "set amount to 5000".
_AMOUNT_EDIT_RE = re.compile(
    r"(?:make it|change (?:the )?amount to|amount(?: is)?|set amount to|actually|"
    r"correction|edit(?: amount)? to)\s*"
    r"(?:₦|n|\$|£|€)?\s*"
    r"(\d{1,3}(?:[,\s]?\d{3})*(?:\.\d+)?|\d+)\s*([km]?)\b",
    re.IGNORECASE,
)


def _parse_amount_edit(text: str) -> float | None:
    """Try to extract a new amount from an edit-style reply.
    Returns the number as a float, or None if no edit intent detected.
    """
    if not text:
        return None
    m = _AMOUNT_EDIT_RE.search(text)
    if not m:
        return None
    raw, suffix = m.group(1), (m.group(2) or "").lower()
    try:
        value = float(raw.replace(",", "").replace(" ", ""))
    except ValueError:
        return None
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    if value <= 0:
        return None
    return value


def try_handle_inline_edit(
    db,
    client,
    sender: str,
    text: str,
    *,
    issuer_id: int | None,
) -> bool:
    """Best-effort inline edit. Returns True if we handled the message
    (caller should stop processing), False otherwise.
    """
    if not issuer_id or not text:
        return False
    new_amount = _parse_amount_edit(text)
    if new_amount is None:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=EDIT_WINDOW_MINUTES)
    invoice = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.issuer_id == issuer_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "pending",
            models.Invoice.created_at >= cutoff,
        )
        .order_by(models.Invoice.created_at.desc())
        .first()
    )
    if not invoice:
        # Nothing recent to edit; let the normal flow handle the text.
        return False

    old_amount = float(invoice.amount or 0)
    if abs(old_amount - new_amount) < 0.01:
        client.send_text(
            sender,
            f"That's already the amount on {invoice.invoice_id}. No change made.",
        )
        return True

    try:
        invoice.amount = new_amount
        # Keep totals consistent if the invoice tracks them separately.
        for attr in ("total", "subtotal", "amount_due"):
            if hasattr(invoice, attr) and getattr(invoice, attr) is not None:
                try:
                    setattr(invoice, attr, new_amount)
                except Exception:  # noqa: BLE001
                    pass
        db.add(invoice)
        db.commit()
        db.refresh(invoice)
    except Exception:  # noqa: BLE001
        logger.exception("inline edit failed for invoice %s", invoice.invoice_id)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return False

    currency = getattr(invoice, "currency", None) or "NGN"
    try:
        old_str = fmt_money(old_amount, currency, convert=False)
        new_str = fmt_money(new_amount, currency, convert=False)
    except Exception:  # noqa: BLE001
        old_str, new_str = f"{old_amount:,.0f}", f"{new_amount:,.0f}"

    client.send_text(
        sender,
        (
            f"✏️ Updated *{invoice.invoice_id}*\n"
            f"Amount: {old_str} → *{new_str}*\n\n"
            "Reply *undo* within 5 min to revert."
        ),
    )
    return True


__all__ = ["try_handle_inline_edit"]
