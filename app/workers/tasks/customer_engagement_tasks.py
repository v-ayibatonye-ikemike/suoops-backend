"""
Customer Engagement Tasks.

Two customer-facing automated messages:

1. **Dormant Customer Nudge** — sent TO THE CUSTOMER after 21+ days of no
   activity with a business. Encourages them to come back.

2. **Post-Payment Referral Ask** — sent TO THE CUSTOMER after a successful
   payment, asking them to recommend the business to friends.

Both run as daily Celery tasks and track sends via InvoiceReminderLog to
prevent spam/duplicates.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func

from app.core.config import settings
from app.db.session import session_scope
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# ── Jinja2 ────────────────────────────────────────────────────────────
_template_dir = Path(__file__).parent.parent.parent.parent / "templates" / "email"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)

# ── Constants ─────────────────────────────────────────────────────────
DORMANT_DAYS = 21  # 3 weeks without a purchase
REFERRAL_WINDOW_HOURS = 48  # Check invoices paid in the last 48h


# ── Helpers ───────────────────────────────────────────────────────────


def _is_valid_phone(phone: str | None) -> bool:
    if not phone:
        return False
    digits = phone.lstrip("+")
    return digits.isdigit() and len(digits) >= 10


def _already_sent(db, invoice_id: int, reminder_type: str, channel: str) -> bool:
    """Check if this reminder was already sent for this invoice."""
    from app.models.models import InvoiceReminderLog

    return (
        db.query(InvoiceReminderLog.id)
        .filter(
            InvoiceReminderLog.invoice_id == invoice_id,
            InvoiceReminderLog.reminder_type == reminder_type,
            InvoiceReminderLog.channel == channel,
        )
        .first()
        is not None
    )


def _record_send(db, invoice_id: int, reminder_type: str, channel: str, recipient: str) -> None:
    """Record that a reminder was sent."""
    from app.models.models import InvoiceReminderLog

    db.add(
        InvoiceReminderLog(
            invoice_id=invoice_id,
            reminder_type=reminder_type,
            channel=channel,
            recipient=recipient,
        )
    )
    db.flush()


# ═══════════════════════════════════════════════════════════════════════
# TASK 1: DORMANT CUSTOMER NUDGE (21 days)
# ═══════════════════════════════════════════════════════════════════════


@celery_app.task(
    name="customer_engagement.send_dormant_customer_nudges",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def send_dormant_customer_nudges() -> dict[str, Any]:
    """Nudge customers who haven't purchased from a business in 21+ days.

    For each (customer, business) pair where the last invoice was 21+ days ago,
    send a friendly "we miss you" message TO THE CUSTOMER mentioning the
    business name.

    Sent via email + WhatsApp (if customer has phone and opted in).
    Tracked via InvoiceReminderLog with type 'customer_dormant_21d' to ensure
    one nudge per dormant period. Resets if a new invoice is created.
    """

    from app.models.models import Customer, Invoice, User

    stats = {"email_sent": 0, "whatsapp_sent": 0, "skipped": 0, "failed": 0}

    try:
        with session_scope() as db:
            today = date.today()
            cutoff = today - timedelta(days=max(DORMANT_DAYS, 30))

            # Find each (customer_id, issuer_id) pair where:
            # - last invoice was created 21+ days ago
            # - the customer has at least 1 paid invoice (they're a real customer)
            subq = (
                db.query(
                    Invoice.customer_id,
                    Invoice.issuer_id,
                    func.max(Invoice.created_at).label("last_invoice_date"),
                    func.max(Invoice.id).label("last_invoice_pk"),
                )
                .filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.status == "paid",
                )
                .group_by(Invoice.customer_id, Invoice.issuer_id)
                .having(func.max(Invoice.created_at) < datetime.combine(cutoff, datetime.min.time()))
                .subquery()
            )

            results = (
                db.query(
                    Customer,
                    User,
                    subq.c.last_invoice_pk,
                )
                .join(Customer, Customer.id == subq.c.customer_id)
                .join(User, User.id == subq.c.issuer_id)
                .all()
            )

            if not results:
                logger.info("No dormant customers found (cutoff=%s)", cutoff)
                return {"success": True, **stats}

            logger.info("Dormant customer nudges: %d candidates", len(results))

            for customer, business, last_invoice_pk in results:
                try:
                    # Skip if no contact info
                    if not customer.email and not _is_valid_phone(customer.phone):
                        stats["skipped"] += 1
                        continue

                    # Check if we already sent a dormant nudge for this invoice (any channel)
                    if _already_sent(db, last_invoice_pk, "customer_dormant_21d", "email") or _already_sent(
                        db, last_invoice_pk, "customer_dormant_21d", "whatsapp"
                    ):
                        stats["skipped"] += 1
                        continue

                    customer_name = (customer.name or "").split()[0] if customer.name else "there"
                    business_name = business.business_name or business.name or "us"

                    delivered = False

                    # ── Email DISABLED (WhatsApp-only to cut email volume) ──

                    # ── WhatsApp ──
                    if _is_valid_phone(customer.phone) and customer.whatsapp_opted_in:
                        try:
                            template_name = getattr(settings, "WHATSAPP_TEMPLATE_DORMANT_CUSTOMER", None)
                            if template_name:
                                from app.core.whatsapp import get_whatsapp_client
                                from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

                                if can_send_whatsapp(priority=False):
                                    client = get_whatsapp_client()
                                    components = [
                                        {
                                            "type": "body",
                                            "parameters": [
                                                {"type": "text", "text": customer_name},
                                                {"type": "text", "text": business_name},
                                            ],
                                        }
                                    ]
                                    lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                                    if client.send_template(customer.phone, template_name, lang, components):
                                        record_whatsapp_send(priority=False)
                                        _record_send(
                                            db, last_invoice_pk, "customer_dormant_21d", "whatsapp", customer.phone
                                        )
                                        stats["whatsapp_sent"] += 1
                                        delivered = True
                        except Exception as e:
                            logger.warning("Dormant nudge WA failed for customer %s: %s", customer.id, e)

                    if not delivered:
                        stats["failed"] += 1

                except Exception as e:
                    logger.warning("Dormant nudge failed for customer %s: %s", customer.id, e)
                    stats["failed"] += 1

            db.commit()

        logger.info(
            "Dormant nudges complete: email=%d wa=%d skipped=%d failed=%d",
            stats["email_sent"],
            stats["whatsapp_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Dormant customer nudge task failed: %s", exc)
        raise


# ═══════════════════════════════════════════════════════════════════════
# TASK 2: POST-PAYMENT REFERRAL ASK
# ═══════════════════════════════════════════════════════════════════════


@celery_app.task(
    name="customer_engagement.send_post_payment_referrals",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def send_post_payment_referrals() -> dict[str, Any]:
    """Ask customers to recommend the business after a successful payment.

    Checks invoices paid in the last 48 hours. For each, sends the customer
    a friendly "loved the service? tell your friends!" message mentioning
    the business name.

    Sent via email + WhatsApp. Tracked via InvoiceReminderLog with type
    'post_payment_referral' — one ask per paid invoice.
    """
    from sqlalchemy.orm import joinedload

    from app.models.models import Invoice

    stats = {"email_sent": 0, "whatsapp_sent": 0, "skipped": 0, "failed": 0}

    try:
        with session_scope() as db:
            now = datetime.now(timezone.utc)
            window_start = now - timedelta(hours=REFERRAL_WINDOW_HOURS)

            # Find invoices paid recently
            recent_paid = (
                db.query(Invoice)
                .options(
                    joinedload(Invoice.customer),
                    joinedload(Invoice.issuer),
                )
                .filter(
                    Invoice.status == "paid",
                    Invoice.invoice_type == "revenue",
                    Invoice.paid_at.isnot(None),
                    Invoice.paid_at >= window_start,
                )
                .all()
            )

            if not recent_paid:
                logger.info("No recently paid invoices for referral asks")
                return {"success": True, **stats}

            logger.info("Post-payment referral: %d paid invoices in window", len(recent_paid))

            for inv in recent_paid:
                try:
                    customer = inv.customer
                    issuer = inv.issuer
                    if not customer or not issuer:
                        stats["skipped"] += 1
                        continue

                    # Skip if no contact info
                    if not customer.email and not _is_valid_phone(customer.phone):
                        stats["skipped"] += 1
                        continue

                    # Check if already sent for this invoice. Referral asks go
                    # out on WhatsApp (email is disabled below), and the send is
                    # recorded on the "whatsapp" channel — so dedup on that
                    # channel, otherwise the same customer is re-asked every run.
                    if _already_sent(db, inv.id, "post_payment_referral", "whatsapp"):
                        stats["skipped"] += 1
                        continue

                    customer_name = (customer.name or "").split()[0] if customer.name else "there"
                    business_name = issuer.business_name or issuer.name or "us"
                    business_phone = issuer.phone or ""

                    delivered = False

                    # ── Email DISABLED (WhatsApp-only to cut email volume) ──

                    # ── WhatsApp ──
                    if _is_valid_phone(customer.phone) and customer.whatsapp_opted_in:
                        try:
                            template_name = getattr(settings, "WHATSAPP_TEMPLATE_REFERRAL_ASK", None)
                            if template_name:
                                from app.core.whatsapp import get_whatsapp_client

                                client = get_whatsapp_client()
                                components = [
                                    {
                                        "type": "body",
                                        "parameters": [
                                            {"type": "text", "text": customer_name},
                                            {"type": "text", "text": business_name},
                                            {"type": "text", "text": business_phone},
                                        ],
                                    }
                                ]
                                lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                                if client.send_template(customer.phone, template_name, lang, components):
                                    _record_send(db, inv.id, "post_payment_referral", "whatsapp", customer.phone)
                                    stats["whatsapp_sent"] += 1
                                    delivered = True
                        except Exception as e:
                            logger.warning("Referral WA failed for invoice %s: %s", inv.invoice_id, e)

                    if not delivered:
                        stats["failed"] += 1

                except Exception as e:
                    logger.warning("Referral ask failed for invoice %s: %s", inv.invoice_id, e)
                    stats["failed"] += 1

            db.commit()

        logger.info(
            "Referral asks complete: email=%d wa=%d skipped=%d failed=%d",
            stats["email_sent"],
            stats["whatsapp_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Post-payment referral task failed: %s", exc)
        raise
