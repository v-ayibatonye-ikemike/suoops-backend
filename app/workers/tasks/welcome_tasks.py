"""
Instant Welcome Message Task.

Fires immediately when a new user completes signup.
Sends a short, warm welcome by email.

This is distinct from the Day 0/1/3 activation sequence which runs
on the daily Beat schedule. The instant welcome arrives within seconds
of signup — no waiting until the next morning.
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.db.session import session_scope
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

WELCOME_BROADCAST_LOG_TYPE = "welcome_broadcast_v1"

# Jinja2 template setup
_template_dir = Path(__file__).parent.parent.parent.parent / "templates" / "email"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)


@celery_app.task(
    name="welcome.send_instant_welcome",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 3},
    soft_time_limit=60,
    time_limit=90,
)
def send_instant_welcome(user_id: int, *, broadcast: bool = False) -> dict:
    """Send an instant welcome message right after signup.

    Called asynchronously from ``AuthService.complete_signup()``
    so the API response is not blocked.

    Args:
        user_id: The newly created user's ID.

    Returns:
        dict with keys: email_sent, whatsapp_sent
    """
    from app.models.models import User, UserEmailLog

    result = {"email_sent": False, "whatsapp_sent": False}

    with session_scope() as db:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user:
            logger.warning("Instant welcome: user %s not found", user_id)
            return result

        name = user.name.split()[0] if user.name else "there"

        log_type = WELCOME_BROADCAST_LOG_TYPE if broadcast else "instant_welcome"

        # ── De-dup: don't re-send if task retries after success ──────
        already = (
            db.query(UserEmailLog.id)
            .filter(
                UserEmailLog.user_id == user_id,
                UserEmailLog.email_type == log_type,
            )
            .first()
        )
        if already:
            logger.info("Instant welcome already sent to user %s", user_id)
            return result

        # ── 1. Email ─────────────────────────────────────────────────
        if user.email:
            try:
                template = _jinja_env.get_template("instant_welcome.html")
                html = template.render(
                    name=name,
                    dashboard_url="https://suoops.com/dashboard",
                    whatsapp_number="+234 810 686 5807",
                    whatsapp_url="https://wa.me/2348106865807?text=Hi",
                )
                plain = (
                    f"Hi {name},\n\n"
                    "I created SuoOps because African business owners should not need one "
                    "app to sell, another to collect payments, another to arrange delivery, "
                    "and spreadsheets to understand their business.\n\n"
                    "SuoOps is your commerce operating system: one place to sell, get paid, "
                    "fulfil orders, manage operations, and grow — built for how you already "
                    "do business on WhatsApp.\n\n"
                    "What you can do with SuoOps:\n"
                    "1. Sell online with a shareable storefront where customers browse, "
                    "order, pay, and choose delivery.\n"
                    "2. Sell with confidence using buyer protection, courier delivery, "
                    "order tracking, and secure settlement.\n"
                    "3. Create branded, QR-verifiable invoices from your dashboard "
                    "or by WhatsApp.\n"
                    "4. Manage inventory, customers, payments, expenses, team access, "
                    "insights, and Nigeria-focused tax reports.\n\n"
                    "The simplest way to start:\n"
                    "1. Complete your business profile and bank details:\n"
                    "https://suoops.com/dashboard/settings#profile\n"
                    "https://suoops.com/dashboard/settings#bank-details\n\n"
                    "2. Add your products or services and publish your storefront:\n"
                    "https://suoops.com/dashboard/inventory\n"
                    "https://suoops.com/dashboard/settings#online-payments\n\n"
                    "3. Share your store link — or create a direct invoice on WhatsApp:\n"
                    "https://wa.me/2348106865807?text=Hi\n\n"
                    "4. Manage orders, payments, stock, expenses, insights, and tax reports:\n"
                    "https://suoops.com/dashboard/invoices\n"
                    "https://suoops.com/dashboard/inventory\n"
                    "https://suoops.com/dashboard/expenses\n"
                    "https://suoops.com/dashboard/analytics\n"
                    "https://suoops.com/dashboard/tax\n\n"
                    "Open your dashboard:\n"
                    "https://suoops.com/dashboard\n\n"
                    "Send 'Hi' to +234 810 686 5807 to create invoices and run everyday tasks by chat.\n\n"
                    "Every feature is included. No plans or monthly fees — you only pay when you transact.\n\n"
                    "Welcome aboard. I am glad you are here.\n\n"
                    "Ayibatonye\n"
                    "Founder & CEO, SuoOps"
                )
                result["email_sent"] = _send_email(
                    user.email,
                    "Welcome to SuoOps — Your Business, in One Place",
                    html,
                    plain,
                )
            except Exception as e:
                logger.warning("Instant welcome email failed for user %s: %s", user_id, e)

        # ── Record so Daily activation skips duplicate welcome ───────
        if result["email_sent"] or result["whatsapp_sent"]:
            db.add(UserEmailLog(user_id=user_id, email_type=log_type))
            db.flush()

        logger.info(
            "Instant welcome for user %s: email=%s, wa=%s",
            user_id,
            result["email_sent"],
            result["whatsapp_sent"],
        )

    return result


@celery_app.task(
    name="welcome.broadcast_welcome",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def broadcast_welcome() -> dict:
    """Queue the current welcome message once for every reachable account.

    Each per-user task uses a versioned broadcast dedup key and suppresses the
    signup-only onboarding demo and one-hour activation follow-up.
    """
    from app.models.models import User

    queued = 0
    with session_scope() as db:
        user_ids = [
            user_id
            for (user_id,) in (
                db.query(User.id).filter((User.email.isnot(None)) | (User.phone.isnot(None))).order_by(User.id).all()
            )
        ]

    for user_id in user_ids:
        send_instant_welcome.apply_async(
            args=[user_id],
            kwargs={"broadcast": True},
        )
        queued += 1

    logger.info("Welcome broadcast queued for %d accounts", queued)
    return {"success": True, "queued": queued}


def _send_email(to_email: str, subject: str, html_body: str, plain_body: str) -> bool:
    """Send an email via Brevo SMTP. Returns True on success."""
    from app.utils.smtp import get_smtp_config

    smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

    if not smtp_user or not smtp_password:
        logger.warning("SMTP not configured, skipping email to %s", to_email)
        return False

    msg = MIMEMultipart("alternative")
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception as e:
        logger.warning("Instant welcome SMTP failed to %s: %s", to_email, e)
        return False


# ─────────────────────────────────────────────────────────────────────
# 1-hour activation follow-up
# ─────────────────────────────────────────────────────────────────────
# Scheduled with countdown=3600 from send_instant_welcome. If the user
# still has 0 invoices, send a brief, action-focused WhatsApp nudge
# with a ready-to-copy example they can paste straight back.

FOLLOWUP_LOG_TYPE = "activation_1h_followup"


@celery_app.task(
    name="welcome.send_activation_followup",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=30,
    time_limit=45,
)
def send_activation_followup(user_id: int) -> dict:
    """One-hour follow-up for users who didn't create an invoice after signup."""
    # Activation guidance is surfaced in-app; promotional WhatsApp is disabled.
    return {"sent": False, "reason": "in_app_only"}

    from sqlalchemy import func

    from app.models.models import Invoice, User, UserEmailLog

    result = {"sent": False, "reason": ""}

    with session_scope() as db:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user or not user.phone:
            result["reason"] = "user_not_found"
            return result

        # Already created an invoice — no nudge needed
        has_invoice = db.query(func.count(Invoice.id)).filter(Invoice.issuer_id == user_id).scalar()
        if has_invoice:
            result["reason"] = "already_activated"
            return result

        # Already sent this follow-up (idempotent)
        already = (
            db.query(UserEmailLog.id)
            .filter(
                UserEmailLog.user_id == user_id,
                UserEmailLog.email_type == FOLLOWUP_LOG_TYPE,
            )
            .first()
        )
        if already:
            result["reason"] = "already_sent"
            return result

        name = (user.name or "").split()[0] or "there"

        msg = (
            f"Hey {name} 👋\n\n"
            f"You're one message away from your first invoice!\n\n"
            f"Just copy and paste this _(edit the details)_:\n\n"
            f"*invoice Chidi 08012345678, 5000 wig*\n\n"
            f"That's it — I'll create a professional PDF and send it "
            f"to your customer instantly.\n\n"
            f"Try it now 👇"
        )

        try:
            from app.core.whatsapp import get_whatsapp_client
            from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

            if not can_send_whatsapp(priority=False):
                result["reason"] = "daily_budget_exhausted"
                return result

            client = get_whatsapp_client()
            if client.send_text(user.phone, msg):
                record_whatsapp_send(priority=False)
                db.add(UserEmailLog(user_id=user_id, email_type=FOLLOWUP_LOG_TYPE))
                db.commit()
                result["sent"] = True
                logger.info("Sent 1-hour follow-up to user %s", user_id)
            else:
                # Outside 24h window — use template fallback
                from app.core.config import settings as _settings

                tpl = _settings.WHATSAPP_TEMPLATE_WIN_BACK
                if tpl:
                    lang = _settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                    components = [{"type": "body", "parameters": [{"type": "text", "text": name}]}]
                    if client.send_template(user.phone, tpl, lang, components):
                        record_whatsapp_send(priority=False)
                        db.add(UserEmailLog(user_id=user_id, email_type=FOLLOWUP_LOG_TYPE))
                        db.commit()
                        result["sent"] = True
        except Exception as e:
            logger.warning("1-hour follow-up failed for user %s: %s", user_id, e)
            result["reason"] = str(e)

    return result


# ─────────────────────────────────────────────────────────────────────
# First-paid-invoice referral nudge
# ─────────────────────────────────────────────────────────────────────
# Trigger: dispatched from InvoiceStatusMixin.update_status when an invoice
# transitions to "paid". We only actually message the user when this is
# their FIRST paid invoice — that's the moment of peak motivation
# ("SuoOps got me paid!") so the referral ask lands well.
def _valid_wa_phone(phone: str | None) -> bool:
    digits = (phone or "").strip().lstrip("+")
    return digits.isdigit() and len(digits) >= 10


def _professionalism_score_message(db, user_id: int, first_name: str, *, paid: bool) -> str:
    """Build the WhatsApp professionalism-score nudge."""
    from app.services.analytics_service import calculate_professionalism_score

    score = calculate_professionalism_score(db, user_id)
    pct = int(score.get("score", 0) or 0)
    level = score.get("level", "")
    tips = [t for t in (score.get("tips") or []) if t][:3]

    header = f"🎉 *You just got paid, {first_name}!*" if paid else f"🧾 *Nice — invoice created, {first_name}!*"
    lines = [
        header,
        "",
        f"📊 Your *professionalism score* is *{pct}%*" + (f" ({level})" if level else "") + ".",
        "A complete profile builds trust — businesses that look professional " "get paid faster.",
    ]
    if pct < 100 and tips:
        lines.append("")
        lines.append("*Quick wins:*")
        lines.extend(f"• {t}" for t in tips)
    lines.append("")
    lines.append("👉 Finish setup: suoops.com/dashboard/settings")
    return "\n".join(lines)


def _score_sent_today(db, user_id: int) -> bool:
    from datetime import datetime, timezone

    from app.models.models import UserEmailLog

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    return (
        db.query(UserEmailLog.id)
        .filter(
            UserEmailLog.user_id == user_id,
            UserEmailLog.email_type == f"wa_proscore_{today}",
        )
        .first()
        is not None
    )


def _record_score_today(db, user_id: int) -> None:
    from datetime import datetime, timezone

    from app.models.models import UserEmailLog

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    db.add(UserEmailLog(user_id=user_id, email_type=f"wa_proscore_{today}"))
    db.flush()


@celery_app.task(
    name="engagement.send_daily_professionalism_score",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=30,
    time_limit=45,
)
def send_daily_professionalism_score(user_id: int) -> dict:
    """WhatsApp professionalism-score nudge, at most once per calendar day.

    Dispatched whenever a business creates a revenue invoice. Deduped to once a
    day per user and capped by the daily WhatsApp marketing budget.
    """
    # Profile guidance is surfaced in-app; promotional WhatsApp is disabled.
    return {"sent": False, "skipped_reason": "in_app_only"}

    from app.models.models import User

    result: dict = {"sent": False, "skipped_reason": None}
    with session_scope() as db:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user or not _valid_wa_phone(user.phone):
            result["skipped_reason"] = "no_user_or_phone"
            return result
        if _score_sent_today(db, user.id):
            result["skipped_reason"] = "already_today"
            return result

        from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

        if not can_send_whatsapp():
            result["skipped_reason"] = "budget"
            return result

        try:
            from app.core.whatsapp import get_whatsapp_client

            first_name = (user.name or "there").split()[0]
            msg = _professionalism_score_message(db, user.id, first_name, paid=False)
            get_whatsapp_client().send_text(user.phone, msg)
            record_whatsapp_send()
            _record_score_today(db, user.id)
            result["sent"] = True
            logger.info("Daily professionalism nudge sent to user %s", user_id)
        except Exception as e:
            logger.warning("Daily professionalism nudge failed for user %s: %s", user_id, e)
            result["skipped_reason"] = f"error: {e}"
    return result


FIRST_PAID_REFERRAL_LOG_TYPE = "first_paid_referral_nudge"


@celery_app.task(
    name="referral.send_first_paid_nudge",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=30,
    time_limit=45,
)
def send_first_paid_referral_nudge(user_id: int) -> dict:
    """Send a one-time referral nudge after the user's first paid invoice.

    Idempotent — guarded by ``UserEmailLog`` so retries / repeated paid
    transitions don't spam the user.
    """
    # Referral/profile guidance is surfaced in-app; promotional WhatsApp is disabled.
    return {"sent": False, "skipped_reason": "in_app_only"}

    from sqlalchemy import func

    from app.models import models
    from app.models.models import User, UserEmailLog

    result = {"sent": False, "skipped_reason": None}

    with session_scope() as db:
        user = db.query(User).filter(User.id == user_id).one_or_none()
        if not user or not user.phone:
            result["skipped_reason"] = "no_user_or_phone"
            return result

        # Dedup: only ever send this nudge once per user
        already = (
            db.query(UserEmailLog.id)
            .filter(
                UserEmailLog.user_id == user_id,
                UserEmailLog.email_type == FIRST_PAID_REFERRAL_LOG_TYPE,
            )
            .first()
        )
        if already:
            result["skipped_reason"] = "already_sent"
            return result

        # Confirm this really is the first paid invoice — guards against
        # races where status.py dispatches before checking, or back-fills.
        paid_count = (
            db.query(func.count(models.Invoice.id))
            .filter(
                models.Invoice.issuer_id == user_id,
                models.Invoice.status == "paid",
            )
            .scalar()
            or 0
        )
        if paid_count > 1:
            result["skipped_reason"] = "not_first_paid"
            db.add(UserEmailLog(user_id=user_id, email_type=FIRST_PAID_REFERRAL_LOG_TYPE))
            db.flush()
            return result

        try:
            from app.core.whatsapp import get_whatsapp_client

            first_name = (user.name or "there").split()[0]
            msg = _professionalism_score_message(db, user_id, first_name, paid=True)

            get_whatsapp_client().send_text(user.phone, msg)

            # Also mark the daily key so the creation-triggered nudge doesn't
            # double up on the same day.
            _record_score_today(db, user_id)
            db.add(UserEmailLog(user_id=user_id, email_type=FIRST_PAID_REFERRAL_LOG_TYPE))
            db.flush()
            result["sent"] = True
            logger.info("First-paid professionalism nudge sent to user %s", user_id)
        except Exception as e:
            logger.warning("First-paid professionalism nudge failed for user %s: %s", user_id, e)
            result["skipped_reason"] = f"error: {e}"

    return result
