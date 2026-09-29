"""
User Engagement Email Tasks.

Celery tasks for lifecycle email notifications:
- Activation: Push new users to create their first invoice (Day 0, 1, 3)
- Monetization: Nudge to keep the prepaid wallet topped up once value is felt
  (3+ invoices, wallet low, wallet empty). Every feature is free — users only pay
  commission (manual 0.5%, storefront 3%). No plans/subscriptions.
- Education: Tips every 2 days for active users
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func

from app.core.config import settings
from app.db.session import session_scope
from app.utils.smtp import send_smtp_email as _send_smtp_email
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Jinja2 template setup
_template_dir = Path(__file__).parent.parent.parent.parent / "templates" / "email"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)

# ── Email type constants ─────────────────────────────────────────────
# Activation sequence (one-time sends)
EMAIL_WELCOME_FIRST_INVOICE = "activation_day0"
EMAIL_SEND_ONE_INVOICE = "activation_day1"
EMAIL_DAILY_HABIT = "activation_day3"
EMAIL_NUDGE_DAY7 = "activation_day7"
EMAIL_NUDGE_DAY14 = "activation_day14"

# Monetization (one-time sends based on behavior)
EMAIL_3_INVOICES_SENT = "monetization_3_invoices"
EMAIL_80PCT_LIMIT = "monetization_80pct_limit"
EMAIL_LIMIT_REACHED = "monetization_limit_reached"

# Phone verification nudge (email-only, users without verified phone)
EMAIL_PHONE_NUDGE_DAY5 = "phone_nudge_day5"
EMAIL_PHONE_NUDGE_DAY10 = "phone_nudge_day10"

# Education tips (rotating, tracked by index)
EMAIL_TIP_PREFIX = "tip_"

# Tips pool — rotated every 2 days
TIPS = [
    {
        "subject": "Get paid faster with payment verification",
        "headline": "Tip: Get Paid Faster",
        "body": "When a customer says they've paid, verify it instantly on SuoOps. "
        "Mark invoices as paid and your records stay accurate — no more guessing.",
        "tip": "Go to any pending invoice → Mark as Paid. Your cash position updates automatically.",
        "cta_url": "https://suoops.com/dashboard",
        "cta_label": "Check Your Invoices →",
    },
    {
        "subject": "Your professionalism score matters",
        "headline": "Tip: Look Professional",
        "body": "Businesses that add their logo and bank details to invoices get paid 40% faster. "
        "Your professionalism score shows how complete your setup is.",
        "tip": "Upload your logo in Settings → Business Profile to boost your score.",
        "cta_url": "https://suoops.com/dashboard/settings",
        "cta_label": "Update Your Profile →",
    },
    {
        "subject": "Send invoices via WhatsApp — it's faster",
        "headline": "Tip: WhatsApp Delivery",
        "body": "You can create and send invoices right from WhatsApp. "
        "Just message your SuoOps number with the customer name, item, and amount.",
        "tip": 'Try it: Send "Invoice Amina ₦5000 for consulting" to your SuoOps WhatsApp.',
        "cta_url": None,
        "cta_label": None,
    },
    {
        "subject": "Track your expenses alongside revenue",
        "headline": "Tip: Track Expenses Too",
        "body": "SuoOps isn't just for invoices. Record your business expenses so you can see "
        "your true profit — not just revenue.",
        "tip": "Go to Dashboard → Expenses → Add Expense to start tracking.",
        "cta_url": "https://suoops.com/dashboard/expenses",
        "cta_label": "Add an Expense →",
    },
    {
        "subject": "Beware of fake payment alerts",
        "headline": "Tip: Spot Fake Alerts",
        "body": "Fake bank alerts are everywhere. Always verify payments through your actual bank app "
        "before releasing goods. SuoOps helps you track what's truly paid vs. pending.",
        "tip": "Never rely on screenshots alone. Check your bank, then mark it paid on SuoOps.",
        "cta_url": "https://suoops.com/dashboard",
        "cta_label": "Review Pending Invoices →",
    },
]


def _was_sent(db, user_id: int, email_type: str) -> bool:
    """Check if this email type was already sent to this user."""
    from app.models.models import UserEmailLog

    return (
        db.query(UserEmailLog.id).filter(UserEmailLog.user_id == user_id, UserEmailLog.email_type == email_type).first()
        is not None
    )


def _record_sent(db, user_id: int, email_type: str) -> None:
    """Record that this email type was sent to this user."""
    from app.models.models import UserEmailLog

    db.add(UserEmailLog(user_id=user_id, email_type=email_type))
    db.flush()


def _get_user_name(user) -> str:
    """Get a display name from user, falling back to 'there'."""
    return user.name.split()[0] if user.name else "there"


# ── WhatsApp template helper ─────────────────────────────────────────


def _send_wa_template(
    phone: str | None,
    template_name: str | None,
    params: list[str],
    wa_type: str,
    db,
    user_id: int,
    priority: bool = False,
) -> bool:
    """Send a WhatsApp template if configured, not yet sent, and within budget.

    Tracks delivery via ``UserEmailLog`` using ``wa_`` prefixed types
    so templates are never sent twice to the same user.
    Returns True if the template was delivered successfully.
    """
    if not phone or not template_name:
        return False

    if _was_sent(db, user_id, wa_type):
        return False

    # Check daily WhatsApp budget before sending
    from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

    if not can_send_whatsapp(priority=priority):
        logger.debug("WhatsApp daily budget exceeded, skipping %s for user %s", wa_type, user_id)
        return False

    try:
        from app.core.whatsapp import get_whatsapp_client

        client = get_whatsapp_client()
        lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
        components = (
            [
                {
                    "type": "body",
                    "parameters": [{"type": "text", "text": p} for p in params],
                }
            ]
            if params
            else None
        )

        ok = client.send_template(phone, template_name, lang, components)
        if ok:
            _record_sent(db, user_id, wa_type)
            record_whatsapp_send(priority=priority)
        return ok
    except Exception as e:
        logger.warning(
            "WhatsApp template '%s' failed for user %s: %s",
            template_name,
            user_id,
            e,
        )
        return False


# ── Main scheduled task ──────────────────────────────────────────────


@celery_app.task(
    name="engagement.send_lifecycle_emails",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def send_engagement_emails() -> dict[str, Any]:
    """Send lifecycle engagement emails to users based on their behavior.

    Runs daily at 09:00 UTC (10:00 WAT).
    Processes users in batches of 50 to avoid blocking the worker.

    Segments:
    1. Activation — new users who haven't created an invoice yet (Day 0/1/3)
    2. Monetization — active users approaching or at invoice limits
    3. Tips — every-2-days education for active FREE-tier users
    """
    from app.models.models import Invoice, User

    now = datetime.now(timezone.utc)
    stats: dict[str, int] = {
        "activation_sent": 0,
        "monetization_sent": 0,
        "tips_sent": 0,
        "phone_nudge_sent": 0,
        "whatsapp_sent": 0,
        "skipped": 0,
        "failed": 0,
    }

    BATCH_SIZE = 50

    try:
        with session_scope() as db:
            total_users = (
                db.query(func.count(User.id))
                .filter(
                    User.email != None,  # noqa: E711
                )
                .scalar()
                or 0
            )

            # Pre-fetch invoice counts for all users in one query
            # (eliminates per-user invoice count query — saves 50K queries at scale)
            from app.models.models import Invoice

            invoice_count_map: dict[int, int] = {}
            for row in (
                db.query(Invoice.issuer_id, func.count(Invoice.id))
                .filter(Invoice.invoice_type == "revenue")
                .group_by(Invoice.issuer_id)
                .all()
            ):
                invoice_count_map[row[0]] = row[1]

            for offset in range(0, total_users, BATCH_SIZE):
                users = (
                    db.query(User)
                    .filter(User.email != None)  # noqa: E711
                    .order_by(User.id)
                    .offset(offset)
                    .limit(BATCH_SIZE)
                    .all()
                )

                for user in users:
                    try:
                        _process_user(db, user, now, stats, invoice_count_map)
                    except Exception as e:
                        logger.warning("Engagement email failed for user %s: %s", user.id, e)
                        stats["failed"] += 1

                db.commit()
                # Expire loaded objects to free memory
                db.expire_all()

        logger.info(
            "Engagement emails complete: activation=%d monetization=%d tips=%d "
            "phone_nudge=%d whatsapp=%d skipped=%d failed=%d",
            stats["activation_sent"],
            stats["monetization_sent"],
            stats["tips_sent"],
            stats["phone_nudge_sent"],
            stats["whatsapp_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Engagement email task failed: %s", exc)
        raise


def _process_user(
    db, user, now: datetime, stats: dict[str, int], invoice_count_map: dict[int, int] | None = None
) -> None:
    """Determine which email (if any) to send to a single user."""
    from app.models.models import Invoice

    name = _get_user_name(user)
    signup_age = (
        now - user.created_at.replace(tzinfo=timezone.utc) if user.created_at.tzinfo is None else now - user.created_at
    )

    # Use pre-fetched count if available, otherwise query (fallback)
    if invoice_count_map is not None:
        invoice_count = invoice_count_map.get(user.id, 0)
    else:
        invoice_count = (
            db.query(func.count(Invoice.id))
            .filter(Invoice.issuer_id == user.id, Invoice.invoice_type == "revenue")
            .scalar()
        ) or 0

    # ── 0. PHONE VERIFICATION NUDGE — DISABLED (phone-first signup) ──
    # With phone-first signup, users already have WhatsApp connected.
    # No need to nudge them to connect.

    # ── 1. ACTIVATION (users with 0 invoices) ──────────────────────
    #    Day 0: Welcome email
    #    Day 3: Pro pitch
    #    Day 7: "Your invoices are waiting" nudge
    #    Day 14: Final "last chance" nudge
    if invoice_count == 0 and signup_age.days <= 14:
        if signup_age.days == 1:
            stats["skipped"] += 1  # Day 1 removed — WhatsApp onboarding covers this
        elif signup_age.days == 0 and _was_sent(db, user.id, "instant_welcome"):
            stats["skipped"] += 1
        elif signup_age.days == 7:
            _send_zero_invoice_nudge(db, user, name, 7, stats)
        elif signup_age.days == 14:
            _send_zero_invoice_nudge(db, user, name, 14, stats)
        else:
            _send_activation(db, user, name, signup_age.days, stats)
        return

    # ── 2. FIRST INVOICE FOLLOW-UP (limited, deduplicated email) ───────
    if invoice_count >= 1 and not _was_sent(db, user.id, "wa_first_invoice"):
        if user.email and not _was_sent(db, user.id, "email_first_invoice"):
            try:
                tpl = _jinja_env.get_template("engagement_first_invoice.html")
                html = tpl.render(
                    name=name,
                    headline="Congrats on Your First Invoice! 🎊",
                    body_text=(
                        "You just created your first invoice on SuoOps — "
                        "welcome to stress-free invoicing! Did you know you can also:\n\n"
                        "📱 Track expenses on WhatsApp\n"
                        "📊 Get daily business summaries\n"
                        "📧 Send professional PDF invoices\n\n"
                        "Visit your dashboard to explore!"
                    ),
                )
                plain = (
                    f"Congrats {name}! You just created your first invoice on SuoOps. "
                    "Visit suoops.com/dashboard to explore more features."
                )
                if _send_smtp_email(user.email, "Congrats on your first invoice! 🎊", html, plain):
                    _record_sent(db, user.id, "email_first_invoice")
                    stats["emails_sent"] = stats.get("emails_sent", 0) + 1
            except Exception as e:
                logger.warning("First-invoice email failed for user %s: %s", user.id, e)

    # ── 3. MONETIZATION (keep the prepaid wallet topped up) ──────────
    if user.plan.value == "free" and invoice_count > 0:
        if _send_monetization(db, user, name, invoice_count, stats):
            return

    # ── 4. EDUCATION TIPS — DISABLED (cut email volume) ──────────
    # Tips are now delivered via WhatsApp morning insights only.
    if invoice_count > 0 and user.plan.value == "free":
        stats["skipped"] += 1
        return

    stats["skipped"] += 1


# ── Phone-verification nudge ─────────────────────────────────────────


def _send_phone_nudge(db, user, name: str, days_since_signup: int, stats: dict[str, int]) -> None:
    """Email users who haven't verified a phone number to connect WhatsApp.

    - Day 5:  First nudge ("Create invoices via WhatsApp")
    - Day 10: Second nudge ("You're missing out — WhatsApp invoicing is faster")

    Uses the existing ``whatsapp_bot_promotion.html`` template.
    Only sent via email (can't reach these users on WhatsApp by definition).
    """
    from datetime import datetime

    nudge_map: dict[int, tuple[str, str]] = {
        5: (
            EMAIL_PHONE_NUDGE_DAY5,
            "Create invoices via WhatsApp — connect your number!",
        ),
        10: (
            EMAIL_PHONE_NUDGE_DAY10,
            "You're missing out — WhatsApp invoicing is faster",
        ),
    }

    entry = nudge_map.get(days_since_signup)
    if not entry:
        return

    email_type, subject = entry

    if not user.email:
        return

    if _was_sent(db, user.id, email_type):
        return

    template = _jinja_env.get_template("whatsapp_bot_promotion.html")
    html = template.render(
        user_name=name,
        dashboard_url="https://suoops.com/dashboard/settings",
        help_url="https://support.suoops.com",
        unsubscribe_url="https://suoops.com/unsubscribe",
        current_year=datetime.now().year,
    )
    plain = (
        f"Hi {name},\n\n"
        "You signed up for SuoOps but haven't connected your WhatsApp yet.\n"
        "With the WhatsApp bot you can create invoices in seconds — just send a message!\n\n"
        "Steps:\n"
        "1. Log in at suoops.com → Settings\n"
        "2. Click 'Connect WhatsApp'\n"
        "3. Enter your phone number and verify with the OTP\n\n"
        "Our WhatsApp number: +234 818 376 3636\n\n"
        "Connect now: https://suoops.com/dashboard/settings\n\n"
        "— SuoOps"
    )

    if _send_smtp_email(user.email, subject, html, plain):
        _record_sent(db, user.id, email_type)
        stats["phone_nudge_sent"] += 1
        logger.info("Sent phone nudge '%s' to user %s", email_type, user.id)
    else:
        stats["failed"] += 1


def _send_zero_invoice_nudge(db, user, name: str, day: int, stats: dict[str, int]) -> None:
    """Send a re-engagement nudge to zero-invoice users on Day 7 or Day 14.

    Uses the existing limited, deduplicated email channel only.
    """
    nudge_map = {
        7: (
            EMAIL_NUDGE_DAY7,
            "Your free invoices are waiting!",
            (
                f"Hi {name},\n\n"
                "You signed up for SuoOps a week ago but haven't sent your first invoice yet.\n\n"
                "It takes 30 seconds — just text our WhatsApp bot:\n"
                '"Invoice Joy 5000 wig"\n\n'
                "...and your invoice goes out instantly with a payment link!\n\n"
                "Your 2 free invoices are ready to use.\n\n"
                "Create your first invoice: https://suoops.com/dashboard\n\n"
                "— The SuoOps Team"
            ),
            (
                f"👋 Hi {name}! You signed up a week ago but haven't sent your first invoice yet.\n\n"
                "It's super easy — just text me:\n"
                "`Invoice Joy 5000 wig`\n\n"
                "...and your customer gets a professional invoice instantly! 🧾\n\n"
                "You have 2 free invoices waiting. Try it now!"
            ),
        ),
        14: (
            EMAIL_NUDGE_DAY14,
            "Last chance — your free invoices expire soon",
            (
                f"Hi {name},\n\n"
                "It's been 2 weeks since you signed up for SuoOps.\n\n"
                "Your 2 free invoices are still waiting. Here's what you can do in 30 seconds:\n\n"
                "1. Open WhatsApp\n"
                '2. Text: "Invoice Joy 5000 wig"\n'
                "3. Done — your customer gets a professional invoice\n\n"
                "No forms, no complexity. Just text and send.\n\n"
                "Create your first invoice: https://suoops.com/dashboard\n\n"
                "— The SuoOps Team"
            ),
            (
                f"⏰ {name}, it's been 2 weeks! Your free invoices are still waiting.\n\n"
                "Just text me something like:\n"
                "`Invoice Ade 10000 for design work`\n\n"
                "Your customer gets a professional invoice with payment details instantly.\n\n"
                "Don't let your free invoices go to waste! 🚀"
            ),
        ),
    }

    entry = nudge_map.get(day)
    if not entry:
        stats["skipped"] += 1
        return

    email_type, subject, plain_email, _wa_message = entry

    if _was_sent(db, user.id, email_type):
        stats["skipped"] += 1
        return

    sent = False

    # Email
    if user.email:
        if _send_smtp_email(user.email, subject, None, plain_email):
            sent = True

    if sent:
        _record_sent(db, user.id, email_type)
        stats["activation_sent"] += 1
        logger.info("Sent day_%d nudge to user %s", day, user.id)
    else:
        stats["failed"] += 1


def _send_activation(db, user, name: str, days_since_signup: int, stats: dict[str, int]) -> None:
    """Send activation email based on days since signup.

    Day 0 — Welcome: feature showcase (invoicing, WhatsApp, reminders, receipts,
             expenses, customer DB) + CTA to create first invoice.
    Day 1 — Feature tour: deep dive into 5 key features users may not know about.
    Day 3 — Pricing intro: how the low 1% manual fee works + a wallet top-up nudge.
    """
    # Each day maps to: (email_type, subject, template_file, plain_text_fallback)
    email_map = {
        0: (
            EMAIL_WELCOME_FIRST_INVOICE,
            "Welcome to SuoOps — Here's Everything You Can Do 🎉",
            "activation_day0_welcome.html",
            (
                f"Hi {name},\n\n"
                "Welcome to SuoOps! You just made a smart move.\n\n"
                "Here's what you can do right now:\n"
                "📄 Send professional invoices in under 60 seconds\n"
                "💬 Create invoices by chatting on WhatsApp\n"
                "🔔 Auto payment reminders — no more chasing customers\n"
                "🧾 Receipts with QR verification for every payment\n"
                "💰 Track expenses to see your real profit\n"
                "👥 Customer database built automatically from invoices\n\n"
                "You have 2 free invoices to get started.\n\n"
                "Create your first invoice: https://suoops.com/dashboard/invoices/new\n\n"
                "— SuoOps"
            ),
        ),
        1: (
            EMAIL_SEND_ONE_INVOICE,
            "Did You Know SuoOps Can Do All This? 🔍",
            "activation_day1_features.html",
            (
                f"Hi {name},\n\n"
                "Most people sign up for invoicing — but SuoOps does a lot more:\n\n"
                "🔔 Automatic Payment Reminders — customers get nudged, you stay professional\n"
                "💸 Expense Tracking — see your real profit, not just revenue\n"
                "💬 WhatsApp Invoicing — say 'Invoice Ade ₦15,000 for website design' and it's done\n"
                "👥 Customer Database — purchase history built automatically from invoices\n"
                "🧾 Receipts with QR — anyone can scan to verify a payment is real\n\n"
                "All these features are available on your free plan.\n\n"
                "Explore your dashboard: https://suoops.com/dashboard\n\n"
                "— SuoOps"
            ),
        ),
        3: (
            EMAIL_DAILY_HABIT,
            "How SuoOps pricing works — fees as low as 0.5% ⭐",
            "activation_day3_pro.html",
            (
                f"Hi {name},\n\n"
                "Quick note on pricing — it's refreshingly simple:\n\n"
                "Every feature is free. You only pay when you invoice:\n"
                "✅ Storefront orders — 3% when your customer pays online (nothing upfront)\n"
                "✅ Manual invoices — just 0.5% (min ₦100, capped ₦400 under ₦500k) from your prepaid wallet\n\n"
                "So custom branding, tax reports, inventory, team, voice notes,\n"
                "daily summaries and insights are all included — no plan to buy.\n\n"
                "Top up your wallet anytime (₦1,250 / ₦5,000 / ₦20,000), or just share\n"
                "your storefront link and let customers pay you online.\n\n"
                "Your dashboard: https://suoops.com/dashboard\n\n"
                "— SuoOps"
            ),
        ),
    }

    entry = email_map.get(days_since_signup)
    if not entry:
        stats["skipped"] += 1
        return

    email_type, subject, template_file, plain = entry

    if _was_sent(db, user.id, email_type):
        stats["skipped"] += 1
        return

    template = _jinja_env.get_template(template_file)
    html = template.render(name=name)

    if _send_smtp_email(user.email, subject, html, plain):
        _record_sent(db, user.id, email_type)
        stats["activation_sent"] += 1
        logger.info("Sent activation email '%s' to user %s", email_type, user.id)
    else:
        stats["failed"] += 1


def _send_monetization(db, user, name: str, invoice_count: int, stats: dict[str, int]) -> bool:
    """Send monetization email if user hits a threshold. Returns True if sent."""
    wallet_kobo = getattr(user, "wallet_balance_kobo", 0) or 0
    wallet_naira = wallet_kobo // 100

    # Wallet empty
    if wallet_kobo <= 0 and not _was_sent(db, user.id, EMAIL_LIMIT_REACHED):
        subject = "Your invoice wallet is empty"
        headline = "Top up to keep invoicing"
        body = (
            f"You've sent {invoice_count} invoices — that's great progress! "
            "Your invoice wallet is now empty. Top up to keep creating manual "
            "invoices (just 0.5% each, min ₦100, capped ₦400 under ₦500k) — or share your storefront so "
            "customers order and pay you online, no wallet needed."
        )
        tip = "Top up ₦1,250, ₦5,000 or ₦20,000 in Settings → Billing."
        cta_url = "https://suoops.com/dashboard/billing/purchase"
        cta_label = "Top up wallet →"
        email_type = EMAIL_LIMIT_REACHED

    # Wallet low (under ₦50)
    elif 0 < wallet_naira < 50 and not _was_sent(db, user.id, EMAIL_80PCT_LIMIT):
        subject = f"Wallet low: ₦{wallet_naira} left"
        headline = "Almost out — top up soon"
        body = (
            f"You've sent {invoice_count} invoices so far — you're clearly getting value from SuoOps. "
            f"Your wallet is down to ₦{wallet_naira}. Top up so invoicing never pauses "
            "(0.5% per invoice, min ₦100, capped ₦400 under ₦500k)."
        )
        tip = "Top up before you run out so there's no interruption."
        cta_url = "https://suoops.com/dashboard/billing/purchase"
        cta_label = "Top up wallet →"
        email_type = EMAIL_80PCT_LIMIT

    # After 3 invoices sent (soft mention)
    elif invoice_count >= 3 and not _was_sent(db, user.id, EMAIL_3_INVOICES_SENT):
        subject = "You've sent 3 invoices — keep going!"
        headline = "You're on a Roll 🎉"
        body = (
            f"You've already sent {invoice_count} invoices through SuoOps. "
            "You're building a real record of your business — and every feature is "
            "included, so you only pay 0.5% per manual invoice."
        )
        tip = None
        cta_url = "https://suoops.com/dashboard"
        cta_label = "Back to Dashboard →"
        email_type = EMAIL_3_INVOICES_SENT

    else:
        return False

    template = _jinja_env.get_template("engagement_tip.html")
    html = template.render(
        name=name,
        headline=headline,
        body_text=body,
        tip_text=tip,
        cta_url=cta_url,
        cta_label=cta_label,
    )
    plain = f"Hi {name},\n\n{body}\n\n{'💡 ' + tip if tip else ''}\n\n{cta_url}\n\n— SuoOps"

    if _send_smtp_email(user.email, subject, html, plain):
        _record_sent(db, user.id, email_type)
        stats["monetization_sent"] += 1
        logger.info("Sent monetization email '%s' to user %s", email_type, user.id)
    else:
        stats["failed"] += 1

    # Email already delivered this nudge — no duplicate WhatsApp send. Owners
    # have an email on file; WhatsApp is reserved for customer-facing collection
    # (which drives payment) so we don't pay for redundant owner nudges.

    return True


def _send_tip(db, user, name: str, stats: dict[str, int]) -> None:
    """Send next unsent tip to a user (max one every 2 days)."""
    from app.models.models import UserEmailLog

    # Check when the last tip was sent
    last_tip = (
        db.query(UserEmailLog.sent_at)
        .filter(
            UserEmailLog.user_id == user.id,
            UserEmailLog.email_type.like(f"{EMAIL_TIP_PREFIX}%"),
        )
        .order_by(UserEmailLog.sent_at.desc())
        .first()
    )

    if last_tip:
        last_sent = last_tip[0]
        if last_sent.tzinfo is None:
            last_sent = last_sent.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - last_sent).days < 4:
            stats["skipped"] += 1
            return

    # Find next unsent tip
    for i, tip in enumerate(TIPS):
        tip_type = f"{EMAIL_TIP_PREFIX}{i}"
        if not _was_sent(db, user.id, tip_type):
            template = _jinja_env.get_template("engagement_tip.html")
            html = template.render(
                name=name,
                headline=tip["headline"],
                body_text=tip["body"],
                tip_text=tip["tip"],
                cta_url=tip.get("cta_url"),
                cta_label=tip.get("cta_label"),
            )
            plain = f"Hi {name},\n\n{tip['body']}\n\n💡 {tip['tip']}\n\n— SuoOps"

            if _send_smtp_email(user.email, tip["subject"], html, plain):
                _record_sent(db, user.id, tip_type)
                stats["tips_sent"] += 1
                logger.info("Sent tip_%d to user %s", i, user.id)
            else:
                stats["failed"] += 1
            return

    # All tips sent — nothing more to do
    stats["skipped"] += 1
