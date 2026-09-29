"""Automated feedback collection via WhatsApp and email.

Sends feedback requests to users who hit milestones:
- 10th invoice created
- 50th invoice created
- 100th invoice created
- First month active
- First invoice paid

Users reply via WhatsApp → saved as Testimonial → admin approves → landing page.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func

from app.core.config import settings
from app.workers.celery_app import celery_app
from app.workers.tasks.messaging_tasks import session_scope

logger = logging.getLogger(__name__)

_FEEDBACK_REDIS_PREFIX = "feedback:asked:"
_FEEDBACK_PENDING_PREFIX = "feedback:pending:"
_FEEDBACK_TTL = 86400 * 7  # 7 days to respond
_FEEDBACK_ASKED_TTL = 86400 * 90  # Don't re-ask for 90 days


def _is_valid_phone(phone: str | None) -> bool:
    if not phone:
        return False
    digits = phone.lstrip("+")
    return digits.isdigit() and len(digits) >= 10


def mark_feedback_pending(phone: str) -> None:
    """Mark that a user has been asked for feedback (they can reply within 7 days)."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        r.setex(f"{_FEEDBACK_PENDING_PREFIX}{phone}", _FEEDBACK_TTL, "1")
    except Exception:
        logger.debug("Failed to mark feedback pending for %s", phone)


def is_feedback_pending(phone: str) -> bool:
    """Check if a user has a pending feedback request."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        return bool(r.get(f"{_FEEDBACK_PENDING_PREFIX}{phone}"))
    except Exception:
        return False


def clear_feedback_pending(phone: str) -> None:
    """Clear the pending feedback flag after user responds."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        r.delete(f"{_FEEDBACK_PENDING_PREFIX}{phone}")
    except Exception:
        pass


def _was_recently_asked(user_id: int) -> bool:
    """Check if we already asked this user recently (90 days)."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        return bool(r.get(f"{_FEEDBACK_REDIS_PREFIX}{user_id}"))
    except Exception:
        return False


def _mark_asked(user_id: int) -> None:
    """Record that we asked this user for feedback."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        r.setex(f"{_FEEDBACK_REDIS_PREFIX}{user_id}", _FEEDBACK_ASKED_TTL, "1")
    except Exception:
        pass


MILESTONES = [1, 10, 20, 30]


@celery_app.task(
    name="feedback.collect_user_feedback",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def collect_user_feedback() -> dict[str, Any]:
    """Send feedback requests to users who hit invoicing milestones.

    Runs weekly. Identifies users who:
    1. Hit a milestone invoice count (10, 50, 100, 200)
    2. Have been active in the last 30 days
    3. Haven't been asked in the last 90 days
    4. Don't already have a testimonial

    Sends feedback request emails to eligible users.
    """
    from app.models.models import Invoice, Testimonial, User

    stats: dict[str, int] = {
        "email_sent": 0,
        "skipped": 0,
        "failed": 0,
    }

    try:
        with session_scope() as db:
            thirty_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=30)

            # Users with recent activity and no existing testimonial
            users_with_counts = (
                db.query(
                    User,
                    func.count(Invoice.id).label("invoice_count"),
                )
                .join(Invoice, Invoice.issuer_id == User.id)
                .outerjoin(Testimonial, Testimonial.user_id == User.id)
                .filter(
                    Invoice.invoice_type == "revenue",
                    Testimonial.id.is_(None),  # No testimonial yet
                )
                .group_by(User.id)
                .having(func.count(Invoice.id) >= MILESTONES[0])
                .all()
            )

            logger.info("Feedback collection: %d candidate users", len(users_with_counts))

            for user, invoice_count in users_with_counts:
                try:
                    # Check milestone
                    hit_milestone = any(invoice_count >= m for m in MILESTONES)
                    if not hit_milestone:
                        stats["skipped"] += 1
                        continue

                    # Recently asked?
                    if _was_recently_asked(user.id):
                        stats["skipped"] += 1
                        continue

                    # Recently active?
                    last_invoice = (
                        db.query(Invoice.created_at)
                        .filter(Invoice.issuer_id == user.id)
                        .order_by(Invoice.created_at.desc())
                        .first()
                    )
                    if not last_invoice or last_invoice.created_at < thirty_days_ago:
                        stats["skipped"] += 1
                        continue

                    name = (user.name or "").split()[0] if user.name else "there"
                    delivered = False

                    # ── Email DISABLED (WhatsApp feedback template handles this) ──
                    # WhatsApp feedback is sent via the WHATSAPP_TEMPLATE_FEEDBACK
                    # template in the same task (handled separately).

                    # ── WhatsApp ──
                    has_phone = _is_valid_phone(user.phone)
                    if has_phone:
                        try:
                            from app.core.whatsapp import get_whatsapp_client
                            from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

                            template_name = getattr(settings, "WHATSAPP_TEMPLATE_FEEDBACK", None)
                            if template_name and can_send_whatsapp(priority=False):
                                client = get_whatsapp_client()
                                lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                                components = [
                                    {
                                        "type": "body",
                                        "parameters": [
                                            {"type": "text", "text": name},
                                            {"type": "text", "text": str(invoice_count)},
                                        ],
                                    }
                                ]
                                if client.send_template(user.phone, template_name, lang, components):
                                    record_whatsapp_send(priority=False)
                                    from app.workers.tasks.feedback_tasks import mark_feedback_pending

                                    mark_feedback_pending(user.phone)
                                    stats["whatsapp_sent"] = stats.get("whatsapp_sent", 0) + 1
                                    delivered = True
                        except Exception as e:
                            logger.warning("Feedback WhatsApp failed for user %s: %s", user.id, e)

                    if delivered:
                        _mark_asked(user.id)
                    else:
                        stats["failed"] += 1

                except Exception as e:
                    logger.warning("Feedback failed for user %s: %s", user.id, e)
                    stats["failed"] += 1

            db.commit()

        logger.info("Feedback collection: %s", stats)
        return {"success": True, **stats}

    except Exception as exc:
        logger.warning("Feedback collection task failure: %s", exc)
        raise


def _send_feedback_email(email: str, name: str, invoice_count: int, token: str) -> bool:
    """Send a feedback request email with a link to the feedback form."""
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    feedback_url = f"{settings.FRONTEND_URL}/feedback?token={token}"

    subject = f"🎉 {name}, you've sent {invoice_count} invoices! How's SuoOps working for you?"
    html = f"""
    <div style="font-family: sans-serif; max-width: 500px; margin: 0 auto; \
padding: 20px;">
        <h2 style="color: #0B3318;">Hey {name}! 🎉</h2>
        <p>You've created <strong>{invoice_count} invoices</strong> with SuoOps — \
that's amazing!</p>
        <p>We'd love to hear how SuoOps is helping your business. A quick sentence \
or two would mean the world to us.</p>
        <p style="text-align: center; margin: 24px 0;">
            <a href="{feedback_url}" style="display: inline-block; \
background-color: #0B6B3A; color: white; padding: 14px 28px; border-radius: 8px; \
text-decoration: none; font-weight: 600; font-size: 15px;">Share Your Feedback</a>
        </p>
        <p style="color: #666; font-size: 13px;">Takes less than 30 seconds. Your \
feedback may be featured on our website.</p>
        <p style="color: #666; font-size: 13px; margin-top: 24px;">— The SuoOps \
Team</p>
    </div>
    """
    plain = (
        f"Hey {name}! 🎉\n\n"
        f"You've created {invoice_count} invoices with SuoOps — that's amazing!\n\n"
        "We'd love to hear how SuoOps is helping your business.\n\n"
        f"Share your feedback here: {feedback_url}\n\n"
        "Takes less than 30 seconds.\n\n"
        "— The SuoOps Team"
    )

    from app.utils.smtp import get_smtp_config

    smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

    if not smtp_user or not smtp_password:
        logger.warning("SMTP not configured, skipping feedback email to %s", email)
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = from_email
        msg["To"] = email
        msg["Reply-To"] = "feedback@suoops.com"
        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        return True
    except Exception as e:
        logger.warning("Feedback email failed to %s: %s", email, e)
        return False
