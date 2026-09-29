"""
One-time feature announcement broadcast.

Announces the storefront + online-payments launch (flat 3%, no plans) to
existing users via email (always) and WhatsApp. The WhatsApp side uses a
dedicated approved template (``WHATSAPP_TEMPLATE_FEATURE_ANNOUNCEMENT`` —
one param: {{1}} first name; the announcement copy is fixed in the template
itself). If that env var is unset, WhatsApp is skipped and email still goes out.

Idempotent per user via ``UserEmailLog``, so it can be run repeatedly
(e.g. across several days to respect the WhatsApp budget) without
double-sending. Bump the ``_v1`` suffix on the tracking types to run a
brand-new announcement in the future.

Trigger from the admin panel: POST /admin/tasks/feature_announcement/trigger
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings
from app.db.session import session_scope
from app.utils.smtp import send_smtp_email
from app.workers.celery_app import celery_app
from app.workers.tasks.engagement_tasks import (
    _get_user_name,
    _record_sent,
    _was_sent,
)

logger = logging.getLogger(__name__)

_template_dir = Path(__file__).parent.parent.parent.parent / "templates" / "email"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)

# Bump these suffixes to broadcast a future announcement.
ANNOUNCE_EMAIL_TYPE = "announce_online_storefront_v1"
ANNOUNCE_WA_TYPE = "wa_announce_online_storefront_v1"

_SUBJECT = "New: get paid online + your own storefront 🛍️"
_HEADLINE = "Two big new ways to get paid 🎉"
_CTA_URL = "https://suoops.com/dashboard/settings#online-payments"
_CTA_LABEL = "Turn it on →"

# Reference copy for the announcement template's FIXED body in Meta. Only the
# first name {{1}} is a variable — Meta rejects a template that is mostly
# variables or that ends with one. Kept here for documentation; NOT sent as a
# param (the wording lives inside the approved template).
_WA_BODY = (
    "🎉 New: get paid online + your own storefront. Customers can now pay you by "
    "card or transfer — it auto-confirms and settles to your bank the next business "
    "day (flat 3%, no monthly fee), and you get a shareable link for all your products. "
    "Turn it on in Settings → Business: suoops.com/dashboard/settings"
)


def _plain_text(name: str) -> str:
    return (
        f"Hi {name},\n\n"
        "We just launched two things that make SuoOps even more useful — "
        "no plans to buy.\n\n"
        "💳 Get paid online — customers pay you by card or bank transfer. "
        "Payment auto-confirms and the money settles to your bank the next "
        "business day via Paystack. The customer covers the flat 3% at checkout "
        "— you keep your full price, no monthly fee. "
        "Your manual invoices are unchanged.\n\n"
        "🛍️ Your own storefront — a shareable page of all your products. Post "
        "the link on WhatsApp, Instagram or your bio and customers order and pay online.\n\n"
        f"Turn these on anytime: {_CTA_URL}\n\n"
        "— SuoOps"
    )


def _announce_to_user(db, user, stats: dict[str, int]) -> None:
    """Send the announcement to one user (email + best-effort WhatsApp)."""
    # Users already accepting online payments know about it — don't re-announce.
    if getattr(user, "paystack_subaccount_active", False):
        stats["skipped"] += 1
        return

    name = _get_user_name(user)

    if user.email and not _was_sent(db, user.id, ANNOUNCE_EMAIL_TYPE):
        html = _jinja_env.get_template("feature_announcement.html").render(
            name=name,
            headline=_HEADLINE,
            cta_url=_CTA_URL,
            cta_label=_CTA_LABEL,
        )
        if send_smtp_email(user.email, _SUBJECT, html, _plain_text(name)):
            _record_sent(db, user.id, ANNOUNCE_EMAIL_TYPE)
            stats["email_sent"] += 1
        else:
            stats["failed"] += 1

    # WhatsApp: dedicated approved announcement template.
    _send_announcement_wa(db, user, name, stats)


def _send_announcement_wa(db, user, name: str, stats: dict[str, int]) -> None:
    """Send the WhatsApp announcement via the dedicated approved template.

    Deliberately BYPASSES the daily marketing budget (this is a one-off,
    admin-triggered broadcast) and records a clear reason for every non-send so
    the run log is diagnosable: wa_no_phone / wa_no_template / wa_already /
    wa_failed.
    """
    template = getattr(settings, "WHATSAPP_TEMPLATE_FEATURE_ANNOUNCEMENT", None)
    # Skip placeholder / non-phone values. OAuth signups without a real number
    # store things like "oauth_google_xxx" in the phone column, which Meta
    # rejects with #131009 (malformed). Require a plausible E.164-ish number.
    phone = (user.phone or "").strip()
    digits = phone.lstrip("+")
    if not digits.isdigit() or len(digits) < 10:
        stats["wa_no_phone"] += 1
        return
    if not template:
        stats["wa_no_template"] += 1
        return
    if _was_sent(db, user.id, ANNOUNCE_WA_TYPE):
        stats["wa_already"] += 1
        return
    try:
        from app.core.whatsapp import get_whatsapp_client

        client = get_whatsapp_client()
        lang = getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en") or "en"
        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": name},
                ],
            }
        ]
        ok = client.send_template(user.phone, template, lang, components)
        if ok:
            _record_sent(db, user.id, ANNOUNCE_WA_TYPE)
            stats["whatsapp_sent"] += 1
        else:
            stats["wa_failed"] += 1
    except Exception as e:
        logger.warning("Announcement WhatsApp failed for user %s: %s", user.id, e)
        stats["wa_failed"] += 1


@celery_app.task(
    name="announcement.send_feature_announcement",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=600,
    time_limit=660,
)
def send_feature_announcement(limit: int | None = None) -> dict[str, Any]:
    """Broadcast the storefront + online-payments announcement to existing users.

    Args:
        limit: Optional cap on how many users to process this run (useful for a
               test send). ``None`` processes everyone with an email address.

    Idempotent — users who already received it (or already use online payments)
    are skipped.
    """
    from app.models.models import User

    stats: dict[str, int] = {
        "email_sent": 0,
        "whatsapp_sent": 0,
        "skipped": 0,
        "failed": 0,
        # WhatsApp diagnostics (why a message wasn't sent)
        "wa_no_phone": 0,
        "wa_no_template": 0,
        "wa_already": 0,
        "wa_failed": 0,
    }

    BATCH_SIZE = 50
    processed = 0

    try:
        with session_scope() as db:
            offset = 0
            while True:
                users = (
                    db.query(User)
                    .filter(User.email != None)  # noqa: E711
                    .order_by(User.id)
                    .offset(offset)
                    .limit(BATCH_SIZE)
                    .all()
                )
                if not users:
                    break

                for user in users:
                    try:
                        _announce_to_user(db, user, stats)
                    except Exception as e:
                        logger.warning("Announcement failed for user %s: %s", user.id, e)
                        stats["failed"] += 1
                    processed += 1
                    if limit and processed >= limit:
                        break

                db.commit()
                db.expire_all()

                if limit and processed >= limit:
                    break
                offset += BATCH_SIZE

        logger.info(
            "Feature announcement complete: email=%d whatsapp=%d skipped=%d failed=%d "
            "| wa_no_phone=%d wa_no_template=%d wa_already=%d wa_failed=%d",
            stats["email_sent"],
            stats["whatsapp_sent"],
            stats["skipped"],
            stats["failed"],
            stats["wa_no_phone"],
            stats["wa_no_template"],
            stats["wa_already"],
            stats["wa_failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Feature announcement task failed: %s", exc)
        raise
