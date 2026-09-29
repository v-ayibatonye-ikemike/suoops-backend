"""
Messaging and WhatsApp Tasks.

Celery tasks for WhatsApp processing, reminders, OCR, and payment sync.
"""

from __future__ import annotations

import asyncio
import gc
import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from celery import Task

from app.core.config import settings
from app.db.session import session_scope
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _is_valid_phone(phone: str | None) -> bool:
    """Return True if phone looks like real digits (not an OAuth placeholder)."""
    if not phone:
        return False
    digits = phone.lstrip("+")
    return digits.isdigit() and len(digits) >= 10


@celery_app.task(
    bind=True,
    name="whatsapp.process_inbound",
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_jitter=True,
    retry_kwargs={"max_retries": 5},
    soft_time_limit=120,
    time_limit=150,
)
def process_whatsapp_inbound(self: Task, payload: dict[str, Any]) -> None:
    """Process inbound WhatsApp message.

    Heavy NLP / adapter imports are done lazily to keep baseline worker RSS low.
    """
    from app.bot.nlp_service import NLPService
    from app.bot.whatsapp_adapter import WhatsAppHandler
    from app.core.whatsapp import get_whatsapp_client

    with session_scope() as db:
        handler = WhatsAppHandler(
            client=get_whatsapp_client(),
            nlp=NLPService(),
            db=db,
        )
        asyncio.run(handler.handle_incoming(payload))

    gc.collect()


@celery_app.task(
    name="storefront.notify_back_in_stock",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=60,
    time_limit=90,
)
def notify_back_in_stock(product_id: int) -> dict[str, Any]:
    """Text customers who asked to be told when a sold-out product returns.

    Triggered when a product's stock rises above zero. Each waitlist entry is
    messaged once (marked ``notified``) and capped by the WhatsApp budget.
    """
    from app.bot.whatsapp_client import WhatsAppClient
    from app.models.inventory_models import Product
    from app.models.models import StorefrontStockNotification, User
    from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

    stats = {"sent": 0, "skipped": 0}
    with session_scope() as db:
        product = db.query(Product).filter(Product.id == product_id).one_or_none()
        if not product or (product.track_stock and product.quantity_in_stock <= 0):
            return {"success": True, **stats}

        waiters = (
            db.query(StorefrontStockNotification)
            .filter(
                StorefrontStockNotification.product_id == product_id,
                StorefrontStockNotification.notified.is_(False),
            )
            .all()
        )
        if not waiters:
            return {"success": True, **stats}

        owner = db.query(User).filter(User.id == product.user_id).one_or_none()
        store_name = (owner.business_name or owner.name) if owner else "the store"
        link = (
            f"{settings.FRONTEND_URL}/store/{owner.storefront_slug}?p={product.id}"
            if owner and owner.storefront_slug
            else None
        )
        msg = f"✅ *Back in stock!*\n\n" f"*{product.name}* is available again at {store_name}." + (
            f"\n\n👉 Order now: {link}" if link else ""
        )

        client = WhatsAppClient(settings.WHATSAPP_API_KEY)
        for w in waiters:
            if not _is_valid_phone(w.phone):
                w.notified = True
                stats["skipped"] += 1
                continue
            if not can_send_whatsapp(priority=False):
                break
            try:
                if client.send_text(w.phone, msg):
                    record_whatsapp_send(priority=False)
                    stats["sent"] += 1
                w.notified = True
            except Exception as exc:  # noqa: BLE001
                logger.warning("Back-in-stock notify failed for %s: %s", w.phone, exc)
        db.commit()

    logger.info("Back-in-stock notify for product %s: %s", product_id, stats)
    return {"success": True, **stats}


@celery_app.task(
    name="maintenance.send_overdue_reminders",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def send_overdue_reminders() -> dict[str, Any]:
    """Send escalating reminders to BUSINESS OWNERS about their overdue invoices.

    Escalation tiers (by days overdue):
      owner_light    — 1-3 days  : gentle nudge
      owner_action   — 4-7 days  : action required
      owner_urgent   — 8-14 days : suggest calling customer
      owner_critical — 14+ days  : final warning / write-off hint

    Each tier is sent only once per invoice (tracked in InvoiceReminderLog).
    Runs daily at 09:00 WAT (08:00 UTC).
    """
    from sqlalchemy.orm import joinedload

    from app.bot.conversation_window import is_window_open
    from app.models.models import Invoice, InvoiceReminderLog, User

    sent = 0
    email_sent = 0
    skipped = 0
    failed = 0
    skipped_window = 0

    try:
        with session_scope() as db:
            today = date.today()
            now_dt = datetime.combine(today, datetime.min.time())

            from sqlalchemy import or_

            overdue_invoices = (
                db.query(Invoice)
                .options(joinedload(Invoice.customer))
                .filter(
                    Invoice.status == "pending",
                    Invoice.invoice_type == "revenue",
                    Invoice.due_date != None,  # noqa: E711
                    Invoice.due_date < now_dt,
                    # Never nag owners about unpaid/abandoned storefront orders —
                    # those are online-pay carts, not invoices to chase.
                    or_(
                        Invoice.channel.is_(None),
                        Invoice.channel != "storefront",
                    ),
                )
                .all()
            )

            if not overdue_invoices:
                logger.info("No overdue invoices found")
                return {"success": True, "sent": 0, "total_overdue": 0}

            # Group by issuer
            by_issuer: dict[int, list[Invoice]] = {}
            for inv in overdue_invoices:
                by_issuer.setdefault(inv.issuer_id, []).append(inv)

            logger.info(
                "Found %d overdue invoices for %d users",
                len(overdue_invoices),
                len(by_issuer),
            )

            from app.core.whatsapp import get_whatsapp_client

            client = get_whatsapp_client()

            for issuer_id, invoices in by_issuer.items():
                user = db.query(User).filter(User.id == issuer_id).first()
                if not user:
                    continue
                has_phone = _is_valid_phone(user.phone)

                # Skip users with no reachable channel at all
                if not has_phone and not user.email:
                    continue

                # Classify invoices by escalation tier
                tiers: dict[str, list[Invoice]] = {
                    "owner_light": [],
                    "owner_action": [],
                    "owner_urgent": [],
                    "owner_critical": [],
                }
                for inv in invoices:
                    if not inv.due_date:
                        continue
                    days = (today - inv.due_date.date()).days
                    if days >= 14:
                        tier = "owner_critical"
                    elif days >= 8:
                        tier = "owner_urgent"
                    elif days >= 4:
                        tier = "owner_action"
                    else:
                        tier = "owner_light"

                    # Check if this tier was already sent for this invoice
                    # (any channel — prevents duplicate emails when WA fails)
                    already = (
                        db.query(InvoiceReminderLog)
                        .filter(
                            InvoiceReminderLog.invoice_id == inv.id,
                            InvoiceReminderLog.reminder_type == tier,
                        )
                        .first()
                    )
                    if already:
                        skipped += 1
                        continue
                    tiers[tier].append(inv)

                # Build a single consolidated message per user covering the highest tier
                message = _build_owner_escalation_message(tiers, today)
                if not message:
                    continue

                try:
                    wa_delivered = False
                    from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

                    # 1) Try template first (works outside 24h window)
                    overdue_tpl = settings.WHATSAPP_TEMPLATE_OVERDUE_REPORT
                    if overdue_tpl and has_phone and can_send_whatsapp(priority=True):
                        total_inv = sum(len(v) for v in tiers.values())
                        total_amt = sum(inv.amount for vs in tiers.values() for inv in vs)
                        critical_cnt = len(tiers["owner_critical"])
                        urgent_cnt = len(tiers["owner_urgent"])
                        tpl_lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                        wa_delivered = client.send_template(
                            user.phone,
                            overdue_tpl,
                            tpl_lang,
                            components=[
                                {
                                    "type": "body",
                                    "parameters": [
                                        {"type": "text", "text": str(total_inv)},
                                        {"type": "text", "text": f"₦{total_amt:,.0f}"},
                                        {"type": "text", "text": str(critical_cnt)},
                                        {"type": "text", "text": str(urgent_cnt)},
                                    ],
                                }
                            ],
                        )
                        if wa_delivered:
                            record_whatsapp_send(priority=True)
                            sent += 1

                    # 2) Fallback to plain text (only within 24h window)
                    if (
                        not wa_delivered
                        and has_phone
                        and is_window_open(user.phone)
                        and can_send_whatsapp(priority=True)
                    ):
                        wa_delivered = client.send_text(user.phone, message)
                        if wa_delivered:
                            record_whatsapp_send(priority=True)
                            sent += 1
                        else:
                            logger.warning(
                                "Overdue reminder delivery failed for user %s (phone=%s…)",
                                issuer_id,
                                user.phone[:6] if user.phone else "none",
                            )

                    # 3) Email fallback: send if WhatsApp didn't deliver
                    if not wa_delivered and user.email:
                        email_ok = _send_owner_overdue_email(user.email, user.name, tiers, today)
                        if email_ok:
                            email_sent += 1
                        else:
                            failed += 1
                    elif not wa_delivered and not user.email:
                        skipped_window += 1

                    # Log all tiers we just notified about
                    if wa_delivered or (user.email and not wa_delivered):
                        channel = "whatsapp" if wa_delivered else "email"
                        recipient = user.phone if wa_delivered else user.email
                        for tier, tier_invoices in tiers.items():
                            for inv in tier_invoices:
                                db.add(
                                    InvoiceReminderLog(
                                        invoice_id=inv.id,
                                        reminder_type=tier,
                                        channel=channel,
                                        recipient=recipient,
                                    )
                                )
                        db.commit()
                except Exception as e:
                    logger.warning("Failed owner overdue reminder for user %s: %s", issuer_id, e)
                    failed += 1

        logger.info(
            "Owner overdue reminders: wa_sent=%d email_sent=%d skipped=%d failed=%d skipped_window=%d",
            sent,
            email_sent,
            skipped,
            failed,
            skipped_window,
        )
        return {
            "success": True,
            "sent": sent,
            "email_sent": email_sent,
            "skipped": skipped,
            "failed": failed,
            "skipped_window": skipped_window,
            "total_overdue": len(overdue_invoices),
        }

    except Exception as exc:
        logger.warning("Owner reminder task transient failure: %s", exc)
        raise


def _build_owner_escalation_message(tiers: dict[str, list[Any]], today: date) -> str | None:
    """Build a consolidated escalation message for the business owner."""
    total = sum(len(v) for v in tiers.values())
    if total == 0:
        return None

    total_owed = sum(inv.amount for invs in tiers.values() for inv in invs)
    parts: list[str] = []

    critical = tiers["owner_critical"]
    urgent = tiers["owner_urgent"]
    action = tiers["owner_action"]
    light = tiers["owner_light"]

    if critical:
        days_list = ", ".join(f"{(today - inv.due_date.date()).days}d" for inv in critical[:3])
        parts.append(
            f"🔴 *CRITICAL* — {len(critical)} invoice(s) 14+ days overdue ({days_list}).\n"
            "Consider calling these customers directly or reviewing your collection strategy."
        )

    if urgent:
        parts.append(
            f"🟠 *URGENT* — {len(urgent)} invoice(s) 8-14 days overdue.\n"
            "💡 Try calling your customers — a quick follow-up call recovers 70% of late payments."
        )

    if action:
        parts.append(
            f"🟡 *Action Required* — {len(action)} invoice(s) 4-7 days overdue.\n"
            "💡 Send a polite reminder from your dashboard to nudge them."
        )

    if light:
        parts.append(
            f"🟢 *Heads Up* — {len(light)} invoice(s) 1-3 days overdue.\n"
            "These are still fresh — customers may just need a gentle nudge."
        )

    header = f"⚠️ *Overdue Invoice Report*\n" f"You have {total} overdue invoice(s) totalling ₦{total_owed:,.0f}.\n\n"
    footer = "\n\n🔗 Review all invoices at suoops.com/dashboard"

    return header + "\n\n".join(parts) + footer


# ── Email fallback helpers ───────────────────────────────────────────


def _send_owner_overdue_email(to_email: str, name: str | None, tiers: dict[str, list[Any]], today: date) -> bool:
    """Send the overdue invoice report via email when WhatsApp is unavailable."""
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from jinja2 import Template

    total = sum(len(v) for v in tiers.values())
    total_owed = sum(inv.amount for invs in tiers.values() for inv in invs)
    display_name = (name or "").split()[0] if name else "there"

    # Build HTML body text
    lines: list[str] = []
    tier_labels = {
        "owner_critical": ("🔴 CRITICAL", "14+ days overdue"),
        "owner_urgent": ("🟠 URGENT", "8-14 days overdue"),
        "owner_action": ("🟡 Action Required", "4-7 days overdue"),
        "owner_light": ("🟢 Heads Up", "1-3 days overdue"),
    }
    for key, (label, desc) in tier_labels.items():
        invoices = tiers.get(key, [])
        if invoices:
            lines.append(f"<b>{label}</b> — {len(invoices)} invoice(s) {desc}.")

    body_html = "<br>".join(lines)
    headline = f"You have {total} overdue invoice(s) totalling ₦{total_owed:,.0f}"

    # Load template
    tpl_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "email", "engagement_tip.html")
    try:
        with open(tpl_path) as f:
            tpl = Template(f.read())
        html_body = tpl.render(
            headline=headline,
            name=display_name,
            body_text=body_html,
            tip_text="A quick follow-up call recovers 70% of late payments.",
            cta_url="https://suoops.com/dashboard",
            cta_label="Review Invoices →",
        )
    except Exception:
        html_body = f"<p>Hi {display_name},</p><p>{headline}.</p><p>{body_html}</p>"

    plain_body = (
        f"Hi {display_name},\n\n{headline}.\n\n"
        + "\n".join(f"- {line}" for line in lines)
        + "\n\nReview at https://suoops.com/dashboard"
    )

    subject = f"⚠️ {total} Overdue Invoice(s) — Action Needed"

    from app.utils.smtp import get_smtp_config

    smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

    if not smtp_user or not smtp_password:
        logger.warning("SMTP not configured, cannot send overdue email to %s", to_email)
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
        logger.info("Overdue email sent to %s", to_email)
        return True
    except Exception as e:
        logger.warning("Overdue email failed for %s: %s", to_email, e)
        return False


def _send_mark_paid_email(
    to_email: str,
    name: str | None,
    pending_count: int,
    pending_total: float,
    days_oldest: int,
    oldest_invoices: list,
    today: date,
) -> bool:
    """Send mark-as-paid nudge via email when WhatsApp is unavailable."""
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from jinja2 import Template

    display_name = (name or "").split()[0] if name else "there"

    inv_lines = []
    for inv in oldest_invoices:
        cust_name = inv.customer.name if inv.customer else "Unknown"
        if inv.due_date:
            age = (today - inv.due_date.date()).days
            age_str = f"{age}d overdue"
        else:
            age_str = f"{(today - inv.created_at.date()).days}d old"
        inv_lines.append(f"• {cust_name} — ₦{inv.amount:,.0f} ({age_str})")

    body_html = (
        f"You have <b>{pending_count}</b> pending invoices totalling "
        f"<b>₦{pending_total:,.0f}</b> — the oldest is {days_oldest} days overdue.<br><br>"
        + "<br>".join(inv_lines)
        + "<br><br>If any of these were paid offline, mark them as paid to keep your records accurate."
    )
    headline = f"{pending_count} invoices still marked as pending"

    tpl_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "email", "engagement_tip.html")
    try:
        with open(tpl_path) as f:
            tpl = Template(f.read())
        html_body = tpl.render(
            headline=headline,
            name=display_name,
            body_text=body_html,
            tip_text="Keeping invoices accurate helps you track real revenue.",
            cta_url="https://suoops.com/dashboard",
            cta_label="Mark Invoices as Paid →",
        )
    except Exception:
        html_body = f"<p>Hi {display_name},</p><p>{headline}.</p><p>{body_html}</p>"

    plain_body = (
        f"Hi {display_name},\n\n"
        f"You have {pending_count} pending invoices totalling ₦{pending_total:,.0f}.\n\n"
        + "\n".join(inv_lines)
        + "\n\nIf any were paid offline, mark them as paid at https://suoops.com/dashboard"
    )

    subject = f"📋 {pending_count} Invoices Still Pending — Were They Paid?"

    from app.utils.smtp import get_smtp_config

    smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

    if not smtp_user or not smtp_password:
        logger.warning("SMTP not configured, cannot send mark-paid email to %s", to_email)
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
        logger.info("Mark-paid email sent to %s", to_email)
        return True
    except Exception as e:
        logger.warning("Mark-paid email failed for %s: %s", to_email, e)
        return False


# ── Customer-Facing Payment Reminders ────────────────────────────────


@celery_app.task(
    name="reminders.send_customer_payment_reminders",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def send_customer_payment_reminders() -> dict[str, Any]:
    """Send payment reminders directly to CUSTOMERS via WhatsApp and email.

    Reminder tiers (relative to due_date):
      customer_pre_due    — 3 days before due : gentle heads-up
      customer_due_today  — on the due date   : due today notice
      customer_overdue_1d — 1 day past due    : first nudge
      customer_overdue_7d — 7 days past due   : firmer follow-up
      customer_overdue_14d— 14+ days past due : final escalation (CC's owner)

    Each tier is sent only once per invoice per channel (tracked in
    InvoiceReminderLog).  Runs daily at 10:00 WAT (09:00 UTC).
    """
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload

    from app.models.models import Invoice, InvoiceReminderLog

    stats = {"whatsapp_sent": 0, "email_sent": 0, "skipped": 0, "failed": 0, "wa_skipped_window": 0}

    try:
        with session_scope() as db:
            today = date.today()
            now_dt = datetime.combine(today, datetime.min.time())

            # Window: 3 days before today → any overdue
            window_start = now_dt + timedelta(days=3)

            candidates = (
                db.query(Invoice)
                .options(
                    joinedload(Invoice.customer),
                    joinedload(Invoice.issuer),
                )
                .filter(
                    Invoice.status == "pending",
                    Invoice.invoice_type == "revenue",
                    Invoice.due_date != None,  # noqa: E711
                    Invoice.due_date <= window_start,
                    # Never remind/escalate on unpaid/abandoned storefront carts —
                    # those are online-pay drop-offs, not receivables to chase.
                    # This keeps reminders consistent with the cash dashboard,
                    # which excludes them too (exclude_abandoned_storefront()).
                    or_(
                        Invoice.channel.is_(None),
                        Invoice.channel != "storefront",
                    ),
                )
                .all()
            )

            if not candidates:
                logger.info("No invoices due or overdue for customer reminders")
                return {"success": True, **stats}

            logger.info("Customer reminders: %d candidate invoices", len(candidates))

            for inv in candidates:
                customer = inv.customer
                issuer = inv.issuer
                if not customer or not issuer:
                    continue

                # Determine which tier this invoice falls into
                days_until_due = (inv.due_date.date() - today).days
                tier = _classify_customer_tier(days_until_due)
                if not tier:
                    continue

                customer_phone = customer.phone
                customer_email = customer.email
                business_name = issuer.business_name or issuer.name

                # --- WhatsApp ---
                wa_delivered = False
                email_handled = False
                if customer_phone and _is_valid_phone(customer_phone):
                    already = (
                        db.query(InvoiceReminderLog)
                        .filter(
                            InvoiceReminderLog.invoice_id == inv.id,
                            InvoiceReminderLog.reminder_type == tier,
                            InvoiceReminderLog.channel == "whatsapp",
                        )
                        .first()
                    )
                    if already:
                        stats["skipped"] += 1
                        wa_delivered = True  # Already sent via WA before
                    else:
                        ok = _send_customer_whatsapp_reminder(inv, customer, issuer, tier, business_name)
                        if ok:
                            db.add(
                                InvoiceReminderLog(
                                    invoice_id=inv.id,
                                    reminder_type=tier,
                                    channel="whatsapp",
                                    recipient=customer_phone,
                                )
                            )
                            stats["whatsapp_sent"] += 1
                            wa_delivered = True
                        else:
                            stats["wa_skipped_window"] += 1
                            # WhatsApp failed (likely outside 24h window or
                            # no template) — try email as fallback if available
                            if customer_email and not (
                                db.query(InvoiceReminderLog)
                                .filter(
                                    InvoiceReminderLog.invoice_id == inv.id,
                                    InvoiceReminderLog.reminder_type == tier,
                                    InvoiceReminderLog.channel == "email",
                                )
                                .first()
                            ):
                                email_ok = _send_customer_email_reminder(inv, customer, issuer, tier, business_name)
                                # Mark handled so the email-only block below
                                # doesn't re-send (the log added here is still
                                # pending/unflushed and its dedup query would
                                # miss it, causing a duplicate-row IntegrityError).
                                email_handled = True
                                if email_ok:
                                    db.add(
                                        InvoiceReminderLog(
                                            invoice_id=inv.id,
                                            reminder_type=tier,
                                            channel="email",
                                            recipient=customer_email,
                                        )
                                    )
                                    stats["email_sent"] += 1
                                else:
                                    stats["failed"] += 1

                # --- Email (only for email-only customers, skip if WA delivered) ---
                if customer_email and not wa_delivered and not email_handled:
                    already = (
                        db.query(InvoiceReminderLog)
                        .filter(
                            InvoiceReminderLog.invoice_id == inv.id,
                            InvoiceReminderLog.reminder_type == tier,
                            InvoiceReminderLog.channel == "email",
                        )
                        .first()
                    )
                    if already:
                        stats["skipped"] += 1
                    else:
                        ok = _send_customer_email_reminder(inv, customer, issuer, tier, business_name)
                        if ok:
                            db.add(
                                InvoiceReminderLog(
                                    invoice_id=inv.id,
                                    reminder_type=tier,
                                    channel="email",
                                    recipient=customer_email,
                                )
                            )
                            stats["email_sent"] += 1
                        else:
                            stats["failed"] += 1

                # For 14d+ overdue, also ping the owner about this specific invoice
                if tier == "customer_overdue_14d" and issuer.phone:
                    _notify_owner_escalation(inv, issuer, customer, business_name)

            db.commit()

        logger.info("Customer payment reminders: %s", stats)
        return {"success": True, **stats}

    except Exception as exc:
        logger.warning("Customer reminder task transient failure: %s", exc)
        raise


def _classify_customer_tier(days_until_due: int) -> str | None:
    """Map days-until-due to customer reminder tier.

    Uses ranges instead of exact days so reminders are never missed
    if the Celery beat task skips a day or the worker is temporarily down.
    Each tier is sent only once per invoice thanks to InvoiceReminderLog.

    Returns None if the invoice doesn't match any tier window.
    """
    if 1 <= days_until_due <= 3:
        return "customer_pre_due"
    elif days_until_due == 0:
        return "customer_due_today"
    elif -3 <= days_until_due <= -1:
        return "customer_overdue_1d"
    elif -13 <= days_until_due <= -4:
        return "customer_overdue_7d"
    elif days_until_due <= -14:
        return "customer_overdue_14d"
    return None


def _send_customer_whatsapp_reminder(
    inv: Any,
    customer: Any,
    issuer: Any,
    tier: str,
    business_name: str,
) -> bool:
    """Send a WhatsApp payment reminder to a customer.

    Prefers the ``payment_reminder`` template (deliverable outside the 24-hour
    window) and falls back to free-form text when the template is not configured.
    Checks the 24h conversation window before attempting plain text.
    Budget-gated as priority sends (payment collection = revenue-generating).
    """
    try:
        from app.bot.conversation_window import is_window_open
        from app.core.whatsapp import get_whatsapp_client
        from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

        # Payment reminders are priority (help users collect money)
        if not can_send_whatsapp(priority=True):
            return False

        client = get_whatsapp_client()

        # Prefer template (works outside 24h window)
        template_name = settings.WHATSAPP_TEMPLATE_PAYMENT_REMINDER
        if template_name:
            customer_name = customer.name or "Customer"
            amount_str = f"₦{inv.amount:,.0f}"
            payment_link = f"{settings.FRONTEND_URL}/pay/{inv.invoice_id}"

            days_until_due = (inv.due_date.date() - date.today()).days
            # Template text: "⏰ Overdue: {{4}} days" — send just the number
            if days_until_due >= 0:
                days_info = str(days_until_due)
            else:
                days_info = str(abs(days_until_due))

            from app.utils.invoice_delivery import template_bank_params

            bank_name, account_number, _ = template_bank_params(
                issuer, online_only=bool(getattr(issuer, "online_payments_active", False))
            )

            components = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": customer_name},
                        {"type": "text", "text": inv.invoice_id},
                        {"type": "text", "text": amount_str},
                        {"type": "text", "text": days_info},
                        {"type": "text", "text": bank_name},
                        {"type": "text", "text": account_number},
                        {"type": "text", "text": payment_link},
                    ],
                }
            ]
            lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
            ok = client.send_template(customer.phone, template_name, lang, components)
            if ok:
                record_whatsapp_send(priority=True)
            return ok

        # Fallback: free-form text (only works within 24h window)
        if not is_window_open(customer.phone):
            logger.debug(
                "Skipping WhatsApp reminder for customer %s — outside 24h window",
                customer.phone[:6] if customer.phone else "none",
            )
            return False
        message = _format_customer_reminder(inv, tier, business_name)
        ok = client.send_text(customer.phone, message)
        if ok:
            record_whatsapp_send(priority=True)
        return ok
    except Exception as e:
        logger.warning(
            "WhatsApp reminder failed for invoice %s to %s: %s",
            inv.invoice_id,
            customer.phone,
            e,
        )
        return False


def _send_customer_email_reminder(
    inv: Any,
    customer: Any,
    issuer: Any,
    tier: str,
    business_name: str,
) -> bool:
    """Send an email payment reminder to a customer."""
    try:
        from app.services.notification.service import NotificationService

        svc = NotificationService()
        subject = _email_subject_for_tier(inv, tier, business_name)
        body = _format_customer_reminder(inv, tier, business_name)
        return asyncio.run(svc.send_email(customer.email, subject, body))
    except Exception as e:
        logger.warning(
            "Email reminder failed for invoice %s to %s: %s",
            inv.invoice_id,
            customer.email,
            e,
        )
        return False


def _notify_owner_escalation(inv: Any, issuer: Any, customer: Any, business_name: str) -> None:
    """Alert the business owner about a severely overdue invoice."""
    try:
        from app.bot.conversation_window import is_window_open
        from app.core.whatsapp import get_whatsapp_client

        client = get_whatsapp_client()
        customer_name = customer.name or "a customer"

        msg = (
            f"🔴 *Escalation Alert*\n\n"
            f"Invoice {inv.invoice_id} for {customer_name} "
            f"(₦{inv.amount:,.0f}) is now 14+ days overdue.\n\n"
            "We've sent a final reminder to the customer. "
            "Consider calling them directly or reviewing your collection options.\n\n"
            "🔗 suoops.com/dashboard"
        )

        delivered = False
        if is_window_open(issuer.phone):
            delivered = client.send_text(issuer.phone, msg)

        # Email fallback
        if not delivered and issuer.email:
            import os
            import smtplib
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText

            from jinja2 import Template

            display_name = (issuer.name or "").split()[0] if issuer.name else "there"
            headline = f"Escalation: Invoice {inv.invoice_id} is 14+ days overdue"
            body_html = (
                f"Invoice <b>{inv.invoice_id}</b> for <b>{customer_name}</b> "
                f"(₦{inv.amount:,.0f}) is now 14+ days overdue.<br><br>"
                "We've sent a final reminder to the customer. "
                "Consider calling them directly or reviewing your collection options."
            )
            tpl_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "..", "templates", "email", "engagement_tip.html"
            )
            try:
                with open(tpl_path) as f:
                    tpl = Template(f.read())
                html_body = tpl.render(
                    headline=headline,
                    name=display_name,
                    body_text=body_html,
                    tip_text="A quick phone call often recovers severely overdue payments.",
                    cta_url="https://suoops.com/dashboard",
                    cta_label="View Invoice →",
                )
            except Exception:
                html_body = f"<p>Hi {display_name},</p><p>{headline}.</p><p>{body_html}</p>"

            plain_body = (
                f"Hi {display_name},\n\n{headline}.\n\n"
                f"Invoice {inv.invoice_id} for {customer_name} (₦{inv.amount:,.0f}) "
                "is severely overdue. Consider calling the customer directly.\n\n"
                "Review at https://suoops.com/dashboard"
            )

            from app.utils.smtp import get_smtp_config

            smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

            if smtp_user and smtp_password:
                email_msg = MIMEMultipart("alternative")
                email_msg["From"] = from_email
                email_msg["To"] = issuer.email
                email_msg["Subject"] = f"🔴 {headline}"
                email_msg.attach(MIMEText(plain_body, "plain"))
                email_msg.attach(MIMEText(html_body, "html"))
                try:
                    with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                        server.starttls()
                        server.login(smtp_user, smtp_password)
                        server.send_message(email_msg)
                    logger.info("Escalation email sent to %s for invoice %s", issuer.email, inv.invoice_id)
                except Exception as e:
                    logger.warning("Escalation email failed for %s: %s", issuer.email, e)
    except Exception as e:
        logger.warning("Owner escalation alert failed for invoice %s: %s", inv.invoice_id, e)


# ── Mark-as-Paid Nudge ──────────────────────────────────────────


@celery_app.task(
    name="reminders.send_mark_paid_nudges",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def send_mark_paid_nudges() -> dict[str, Any]:
    """Nudge business owners to mark invoices as paid if they've been settled.

    Many invoices stay "pending" because the customer pays via bank transfer
    or cash, but the business owner forgets to mark them as paid in SuoOps.
    This task sends a weekly nudge per business owner listing their oldest
    pending invoices and encouraging them to update status.

    Tracked via Redis key to avoid spamming — one nudge per owner per 7 days.
    Runs daily at 12:00 WAT (11:00 UTC).
    """
    from sqlalchemy import func as sqlfunc
    from sqlalchemy import or_
    from sqlalchemy.orm import joinedload

    from app.bot.conversation_window import is_window_open
    from app.db.redis_client import get_redis_client
    from app.models.models import Invoice, User

    sent = 0
    skipped_cooldown = 0
    skipped_window = 0
    failed = 0

    try:
        redis = get_redis_client()
        with session_scope() as db:
            today = date.today()
            # Find business owners with 2+ pending revenue invoices older than 5 days
            cutoff = datetime.combine(today - timedelta(days=5), datetime.min.time())

            owners_with_pending = (
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.count(Invoice.id).label("pending_count"),
                    sqlfunc.sum(Invoice.amount).label("pending_total"),
                    sqlfunc.min(Invoice.created_at).label("oldest"),
                )
                .filter(
                    Invoice.status == "pending",
                    Invoice.invoice_type == "revenue",
                    Invoice.created_at < cutoff,
                    # Exclude unpaid/abandoned storefront carts — online-pay
                    # drop-offs, not invoices the owner needs to mark paid.
                    or_(
                        Invoice.channel.is_(None),
                        Invoice.channel != "storefront",
                    ),
                )
                .group_by(Invoice.issuer_id)
                .having(sqlfunc.count(Invoice.id) >= 2)
                .all()
            )

            if not owners_with_pending:
                logger.info("No owners with stale pending invoices for mark-paid nudge")
                return {"success": True, "sent": 0}

            from app.core.whatsapp import get_whatsapp_client

            client = get_whatsapp_client()

            for row in owners_with_pending:
                issuer_id = row.issuer_id
                pending_count = row.pending_count
                pending_total = float(row.pending_total or 0)
                oldest_date = row.oldest

                # Check 7-day cooldown via Redis
                cooldown_key = f"nudge:mark_paid:{issuer_id}"
                if redis and redis.get(cooldown_key):
                    skipped_cooldown += 1
                    continue

                user = db.query(User).filter(User.id == issuer_id).first()
                if not user:
                    continue
                has_phone = _is_valid_phone(user.phone)
                if not has_phone and not user.email:
                    continue

                # Get the 3 oldest pending invoices for specificity
                oldest_invoices = (
                    db.query(Invoice)
                    .options(joinedload(Invoice.customer))
                    .filter(
                        Invoice.issuer_id == issuer_id,
                        Invoice.status == "pending",
                        Invoice.invoice_type == "revenue",
                        Invoice.created_at < cutoff,
                        or_(
                            Invoice.channel.is_(None),
                            Invoice.channel != "storefront",
                        ),
                    )
                    .order_by(Invoice.created_at.asc())
                    .limit(3)
                    .all()
                )

                # Build the nudge message
                days_oldest = (today - oldest_date.date()).days if oldest_date else 0
                owner_name = (user.name or "").split()[0] if user.name else ""
                greeting = f"Hi {owner_name}! 👋\n\n" if owner_name else "Hi there! 👋\n\n"

                msg = (
                    f"{greeting}"
                    f"You have *{pending_count} pending invoice(s)* "
                    f"totalling *₦{pending_total:,.0f}* — "
                    f"the oldest is {days_oldest} days old.\n\n"
                )

                if oldest_invoices:
                    msg += "Here are the oldest ones:\n"
                    for inv in oldest_invoices:
                        cust_name = inv.customer.name if inv.customer else "Unknown"
                        age = (today - inv.created_at.date()).days
                        msg += f"• {inv.invoice_id} — {cust_name} — ₦{inv.amount:,.0f} ({age}d ago)\n"
                    msg += "\n"

                msg += (
                    "💡 *If any of these have been paid* (bank transfer, cash, POS), "
                    "please mark them as paid in your dashboard so your records stay accurate.\n\n"
                    "🔗 suoops.com/dashboard"
                )

                try:
                    delivered = False

                    # 1) Email first — the primary, free channel (owners have an
                    #    email on file, so this keeps nudges off paid WhatsApp).
                    if user.email:
                        delivered = _send_mark_paid_email(
                            user.email,
                            user.name,
                            pending_count,
                            pending_total,
                            days_oldest,
                            oldest_invoices,
                            today,
                        )
                        if delivered:
                            sent += 1

                    # 2) WhatsApp fallback (no email, or email failed): template
                    #    first (works outside the 24h window), then plain text.
                    nudge_tpl = settings.WHATSAPP_TEMPLATE_MARK_PAID_NUDGE
                    if not delivered and nudge_tpl and has_phone:
                        tpl_lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                        delivered = client.send_template(
                            user.phone,
                            nudge_tpl,
                            tpl_lang,
                            components=[
                                {
                                    "type": "body",
                                    "parameters": [
                                        {"type": "text", "text": str(pending_count)},
                                        {"type": "text", "text": f"₦{pending_total:,.0f}"},
                                        {"type": "text", "text": str(days_oldest)},
                                    ],
                                }
                            ],
                        )
                        if delivered:
                            sent += 1

                    if not delivered and has_phone and is_window_open(user.phone):
                        delivered = client.send_text(user.phone, msg)
                        if delivered:
                            sent += 1

                    if not delivered:
                        if user.email or has_phone:
                            failed += 1
                        else:
                            skipped_window += 1
                        continue  # nothing delivered — don't start the cooldown

                    # Set 7-day cooldown after a successful send
                    if redis:
                        redis.set(cooldown_key, "1", ex=7 * 86400)
                except Exception as e:
                    logger.warning("Mark-paid nudge failed for user %s: %s", issuer_id, e)
                    failed += 1

        logger.info(
            "Mark-paid nudges: sent=%d skipped_cooldown=%d skipped_window=%d failed=%d",
            sent,
            skipped_cooldown,
            skipped_window,
            failed,
        )
        return {
            "success": True,
            "sent": sent,
            "skipped_cooldown": skipped_cooldown,
            "skipped_window": skipped_window,
            "failed": failed,
        }

    except Exception as exc:
        logger.warning("Mark-paid nudge task failed: %s", exc)
        raise


def _format_customer_reminder(inv: Any, tier: str, business_name: str) -> str:
    """Format a customer-facing payment reminder message."""
    payment_link = f"{settings.FRONTEND_URL}/pay/{inv.invoice_id}"
    amount_str = f"₦{inv.amount:,.0f}"
    due_str = inv.due_date.strftime("%d %b %Y") if inv.due_date else "N/A"

    if tier == "customer_pre_due":
        return (
            f"👋 Hi{_name_greeting(inv.customer)},\n\n"
            f"Friendly reminder: your invoice {inv.invoice_id} for {amount_str} "
            f"from *{business_name}* is due on *{due_str}* (in 3 days).\n\n"
            f"💳 Pay now: {payment_link}\n\n"
            "Thank you for your prompt attention!"
        )
    elif tier == "customer_due_today":
        return (
            f"⏰ Hi{_name_greeting(inv.customer)},\n\n"
            f"Your invoice {inv.invoice_id} for {amount_str} "
            f"from *{business_name}* is *due today*.\n\n"
            f"💳 Pay now: {payment_link}\n\n"
            "Please make your payment to avoid it becoming overdue."
        )
    elif tier == "customer_overdue_1d":
        return (
            f"📌 Hi{_name_greeting(inv.customer)},\n\n"
            f"Your invoice {inv.invoice_id} for {amount_str} "
            f"from *{business_name}* was due yesterday and is now overdue.\n\n"
            f"💳 Pay now: {payment_link}\n\n"
            "If you've already paid, please disregard this message."
        )
    elif tier == "customer_overdue_7d":
        return (
            f"⚠️ Hi{_name_greeting(inv.customer)},\n\n"
            f"This is a follow-up regarding invoice {inv.invoice_id} for "
            f"{amount_str} from *{business_name}*, which is now 7 days overdue.\n\n"
            f"💳 Pay now: {payment_link}\n\n"
            "Please settle this at your earliest convenience. "
            "If you're experiencing any issues with payment, please reach out to "
            f"{business_name} directly."
        )
    else:  # customer_overdue_14d
        return (
            f"🔴 Hi{_name_greeting(inv.customer)},\n\n"
            f"*Final reminder:* Invoice {inv.invoice_id} for {amount_str} "
            f"from *{business_name}* is now over 14 days past due.\n\n"
            f"💳 Pay now: {payment_link}\n\n"
            "Please arrange payment immediately. If you have questions or "
            f"need to discuss payment terms, please contact {business_name} directly."
        )


def _email_subject_for_tier(inv: Any, tier: str, business_name: str) -> str:
    """Return an email subject line appropriate for the reminder tier."""
    ref = inv.invoice_id
    if tier == "customer_pre_due":
        return f"Reminder: Invoice {ref} from {business_name} due in 3 days"
    elif tier == "customer_due_today":
        return f"Invoice {ref} from {business_name} is due today"
    elif tier == "customer_overdue_1d":
        return f"Overdue: Invoice {ref} from {business_name}"
    elif tier == "customer_overdue_7d":
        return f"Follow-up: Invoice {ref} from {business_name} — 7 days overdue"
    else:
        return f"Final Notice: Invoice {ref} from {business_name} — 14+ days overdue"


def _name_greeting(customer: Any) -> str:
    """Return ' Name' if customer has a name, else empty string."""
    name = getattr(customer, "name", None)
    if name and name.strip():
        return f" {name.split()[0]}"
    return ""


@celery_app.task(
    bind=True,
    name="payments.sync_provider_status",
    autoretry_for=(Exception,),
    retry_backoff=10,
    retry_jitter=True,
    retry_kwargs={"max_retries": 4},
)
def sync_provider_status(self: Task, provider: str, reference: str) -> dict[str, Any]:
    """Sync payment provider status with retries on transient errors.

    Calls Paystack verify endpoint and updates the local invoice status
    to match what the payment provider reports.
    """
    import requests as http_requests

    from app.models.models import Invoice
    from app.services.paystack_http import paystack_requests_proxies

    logger.info("Syncing provider status | provider=%s reference=%s", provider, reference)

    if provider != "paystack":
        logger.warning("Unsupported provider: %s", provider)
        return {"success": False, "error": f"Unsupported provider: {provider}"}

    paystack_key = settings.PAYSTACK_SECRET
    if not paystack_key:
        logger.error("PAYSTACK_SECRET not configured, cannot verify")
        return {"success": False, "error": "Paystack key not configured"}

    try:
        resp = http_requests.get(
            f"https://api.paystack.co/transaction/verify/{reference}",
            headers={"Authorization": f"Bearer {paystack_key}"},
            timeout=15,
            proxies=paystack_requests_proxies(),
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        logger.warning("Paystack verify call failed for %s: %s", reference, e)
        raise  # triggers Celery retry

    tx_data = data.get("data", {})
    tx_status = tx_data.get("status", "unknown")  # success / failed / abandoned
    logger.info(
        "Paystack status for %s: %s (gateway_response=%s)",
        reference,
        tx_status,
        tx_data.get("gateway_response"),
    )

    # Map Paystack status to local invoice status
    status_map = {
        "success": "paid",
        "failed": "failed",
        "abandoned": "pending",
        "reversed": "failed",
    }
    new_status = status_map.get(tx_status)
    if not new_status:
        logger.warning("Unknown Paystack status '%s' for reference %s", tx_status, reference)
        return {"success": False, "error": f"Unknown provider status: {tx_status}"}

    with session_scope() as db:
        invoice = db.query(Invoice).filter(Invoice.invoice_id == reference).first()
        if not invoice:
            logger.warning("No invoice found for reference %s", reference)
            return {"success": False, "error": "Invoice not found"}

        old_status = invoice.status
        if old_status == new_status:
            logger.info("Invoice %s already %s, no update needed", invoice.invoice_id, new_status)
            return {"success": True, "status": new_status, "changed": False}

        invoice.status = new_status
        if new_status == "paid":
            invoice.status_updated_at = datetime.now(timezone.utc)
        db.commit()

        logger.info(
            "Invoice %s status synced: %s → %s (ref=%s)",
            invoice.invoice_id,
            old_status,
            new_status,
            reference,
        )

    return {"success": True, "status": new_status, "changed": True, "old_status": old_status}


@celery_app.task(
    bind=True,
    name="ocr.parse_image",
    autoretry_for=(Exception,),
    retry_backoff=15,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def ocr_parse_image(self: Task, image_bytes_b64: str, context: str | None = None) -> dict[str, Any]:
    """Run OCR parse with retries (handles rate limits/timeouts)."""
    import base64

    from app.services.ocr_service import OCRService

    raw = base64.b64decode(image_bytes_b64)
    service = OCRService()
    result = asyncio.run(service.parse_receipt(raw, context))

    if not result.get("success"):
        if "timeout" in str(result.get("error", "")).lower():
            raise Exception(result["error"])

    return result


# ── Daily Business Summary ───────────────────────────────────────────


@celery_app.task(
    name="summary.send_daily_summaries",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_kwargs={"max_retries": 2},
)
def send_daily_summaries() -> dict[str, Any]:
    """Send daily business summary to all active users via WhatsApp.

    Runs every evening (18:00 UTC / 19:00 WAT).  Skips users with zero
    activity for the day so it never feels like spam.
    """
    from sqlalchemy import func as sqlfunc
    from sqlalchemy import or_

    from app.models.models import Invoice, SubscriptionPlan, User

    sent = 0
    failed = 0
    email_sent = 0

    try:
        with session_scope() as db:
            now_utc = datetime.now(timezone.utc)
            start_of_day = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)

            # Debug: count PRO / pro_override users first
            pro_count = (
                db.query(sqlfunc.count(User.id))
                .filter(
                    (User.plan == SubscriptionPlan.PRO) | (User.pro_override.is_(True)),
                )
                .scalar()
            )
            with_phone = (
                db.query(sqlfunc.count(User.id))
                .filter(
                    User.phone != None,  # noqa: E711
                    (User.plan == SubscriptionPlan.PRO) | (User.pro_override.is_(True)),
                )
                .scalar()
            )
            logger.info(
                "Daily summary debug: pro/override_users=%d with_phone=%d",
                pro_count,
                with_phone,
            )

            # Get PRO / pro_override users who have a phone OR email
            # (email-only users still deserve their daily summary)
            active_users = (
                db.query(User)
                .filter(
                    (User.plan == SubscriptionPlan.PRO) | (User.pro_override.is_(True)),
                    (User.phone != None) | (User.email != None),  # noqa: E711
                )
                .outerjoin(Invoice, Invoice.issuer_id == User.id)
                .group_by(User.id)
                .all()
            )
            logger.info("Daily summary: %d users after join", len(active_users))

            from app.bot.conversation_window import is_window_open
            from app.core.whatsapp import get_whatsapp_client

            client = get_whatsapp_client()
            summary_template = getattr(settings, "WHATSAPP_TEMPLATE_DAILY_SUMMARY", None)
            template_lang = getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en")

            if not summary_template:
                logger.warning(
                    "WHATSAPP_TEMPLATE_DAILY_SUMMARY not configured — "
                    "daily summaries will only reach users who messaged the bot today. "
                    "Set up a WhatsApp message template in Meta Business Manager "
                    "and add it to your env vars for full coverage."
                )

            skipped_window = 0

            for user in active_users:
                try:
                    has_phone = _is_valid_phone(user.phone)

                    # Revenue collected today
                    revenue_today = (
                        db.query(sqlfunc.coalesce(sqlfunc.sum(Invoice.amount), 0))
                        .filter(
                            Invoice.issuer_id == user.id,
                            Invoice.invoice_type == "revenue",
                            Invoice.status == "paid",
                            Invoice.paid_at >= start_of_day,
                        )
                        .scalar()
                    )

                    # Expenses recorded today
                    expenses_today = (
                        db.query(sqlfunc.coalesce(sqlfunc.sum(Invoice.amount), 0))
                        .filter(
                            Invoice.issuer_id == user.id,
                            Invoice.invoice_type == "expense",
                            Invoice.created_at >= start_of_day,
                        )
                        .scalar()
                    )

                    # Total outstanding
                    outstanding = (
                        db.query(sqlfunc.coalesce(sqlfunc.sum(Invoice.amount), 0))
                        .filter(
                            Invoice.issuer_id == user.id,
                            Invoice.invoice_type == "revenue",
                            Invoice.status.in_(["pending", "awaiting_confirmation"]),
                            # Abandoned/unpaid storefront carts aren't money owed —
                            # exclude them so this matches the web dashboard's
                            # Outstanding figure (calculate_cash_position).
                            or_(
                                Invoice.channel.is_(None),
                                Invoice.channel != "storefront",
                                Invoice.status != "pending",
                            ),
                        )
                        .scalar()
                    )

                    # Overdue count
                    overdue_count = (
                        db.query(sqlfunc.count(Invoice.id))
                        .filter(
                            Invoice.issuer_id == user.id,
                            Invoice.invoice_type == "revenue",
                            Invoice.status == "pending",
                            Invoice.due_date != None,  # noqa: E711
                            Invoice.due_date < start_of_day,
                            # Same storefront-cart exclusion as Outstanding above.
                            or_(
                                Invoice.channel.is_(None),
                                Invoice.channel != "storefront",
                            ),
                        )
                        .scalar()
                    ) or 0

                    rev = float(revenue_today)
                    exp = float(expenses_today)
                    net = rev - exp
                    out = float(outstanding)

                    logger.info(
                        "Daily summary user %s: rev=%.0f exp=%.0f outstanding=%.0f overdue=%d",
                        user.id,
                        rev,
                        exp,
                        out,
                        overdue_count,
                    )

                    # PRO users always get a daily summary, even on quiet days

                    message = _format_daily_summary(revenue_today, expenses_today, outstanding, overdue_count)

                    # Email is the primary channel (free). WhatsApp is only used
                    # for users with no email on file — this keeps the daily
                    # summary off paid WhatsApp templates for (almost) everyone.
                    if user.email:
                        email_ok = _send_daily_summary_email(
                            to_email=user.email,
                            name=user.name or user.business_name,
                            revenue=rev,
                            expenses=exp,
                            net=net,
                            outstanding=out,
                            overdue_count=overdue_count,
                        )
                        if email_ok:
                            email_sent += 1
                            continue
                        # Email failed — fall through to the WhatsApp fallback.

                    # ── WhatsApp fallback (no email, or email delivery failed) ──
                    wa_success = False

                    if has_phone:
                        # Template first (works outside 24h window)
                        if summary_template:
                            wa_success = client.send_template(
                                user.phone,
                                summary_template,
                                template_lang,
                                components=[
                                    {
                                        "type": "body",
                                        "parameters": [
                                            {"type": "text", "text": f"₦{rev:,.0f}"},
                                            {"type": "text", "text": f"₦{exp:,.0f}"},
                                            {"type": "text", "text": f"₦{net:,.0f}"},
                                            {"type": "text", "text": f"₦{out:,.0f}"},
                                            {"type": "text", "text": str(overdue_count)},
                                        ],
                                    }
                                ],
                            )
                            if not wa_success:
                                logger.warning(
                                    "Template delivery failed for user %s, trying plain text",
                                    user.id,
                                )

                        # Plain text only works within the 24-hour window
                        if not wa_success:
                            if is_window_open(user.phone):
                                wa_success = client.send_text(user.phone, message)
                            else:
                                skipped_window += 1
                                logger.debug(
                                    "Daily summary outside 24h window for user %s",
                                    user.id,
                                )

                    if wa_success:
                        sent += 1
                        # Follow up with the visual cash snapshot. Images, like
                        # plain text, are only deliverable inside the 24h window.
                        try:
                            _send_daily_cash_image(db, client, user, is_window_open)
                        except Exception as img_err:  # noqa: BLE001
                            logger.warning("Daily cash image failed for user %s: %s", user.id, img_err)
                        continue

                    # Neither channel succeeded
                    failed += 1
                    logger.warning(
                        "Daily summary delivery failed for user %s " "(phone=%s… email=%s)",
                        user.id,
                        user.phone[:6] if user.phone else "none",
                        "yes" if user.email else "no",
                    )

                except Exception as e:
                    logger.warning("Failed daily summary for user %s: %s", user.id, e)
                    failed += 1

        logger.info(
            "Daily summaries: wa_sent=%d email_sent=%d failed=%d skipped_24h_window=%d",
            sent,
            email_sent,
            failed,
            skipped_window,
        )
        return {
            "success": True,
            "sent": sent,
            "email_sent": email_sent,
            "failed": failed,
            "skipped_window": skipped_window,
        }

    except Exception as exc:
        logger.error("Daily summary task failed: %s", exc)
        raise


def _send_daily_cash_image(db, client, user, window_check) -> None:
    """Send the visual cash snapshot as a daily-summary follow-up.

    Only attempted inside the 24-hour customer-service window (free-form images,
    like plain text, cannot be delivered outside it). Skips fully empty snapshots
    to avoid noise.
    """
    if not user.phone or not window_check(user.phone):
        return

    from app.services.analytics_service import calculate_cash_position
    from app.services.cash_dashboard_image import build_cash_snapshot_png
    from app.utils.currency_fmt import get_user_currency

    cash = calculate_cash_position(db, user.id)
    if not any(
        cash.get(k)
        for k in (
            "cash_collected_today",
            "cash_collected_this_week",
            "total_outstanding",
            "total_overdue",
            "expected_inflow_7_days",
            "expenses_today",
        )
    ):
        return

    currency = get_user_currency(db, user.id)
    business = user.business_name or user.name or "Your Business"
    png = build_cash_snapshot_png(cash, str(business), currency)
    media_id = client.upload_media(png, "image/png", "cash-snapshot.png")
    if media_id:
        client.send_image(user.phone, media_id, caption="💰 Your cash snapshot")


def _format_daily_summary(revenue: Any, expenses: Any, outstanding: Any, overdue_count: int) -> str:
    """Format the daily WhatsApp summary message."""
    rev = float(revenue)
    exp = float(expenses)
    net = rev - exp
    out = float(outstanding)

    msg = "📊 *Today's Business Summary*\n\n"

    if rev == 0 and exp == 0 and out == 0 and overdue_count == 0:
        msg += "✨ All clear today — no new transactions or outstanding invoices.\n"
        msg += "💡 Create an invoice to start tracking your cash flow!\n"
    else:
        if rev > 0:
            msg += f"💰 Cash In: ₦{rev:,.0f}\n"
        if exp > 0:
            msg += f"💸 Expenses: ₦{exp:,.0f}\n"
        if rev > 0 or exp > 0:
            emoji = "📈" if net >= 0 else "📉"
            msg += f"{emoji} Net: ₦{net:,.0f}\n"

        msg += "\n"

        if out > 0:
            msg += f"⏳ Outstanding: ₦{out:,.0f}\n"
        if overdue_count > 0:
            s = "s" if overdue_count != 1 else ""
            msg += (
                f"⚠️ Overdue: {overdue_count} invoice{s}\n" "💡 Send reminders from your dashboard to collect faster!\n"
            )

    msg += "\n🔗 suoops.com/dashboard"
    return msg


def _send_daily_summary_email(
    to_email: str,
    name: str | None,
    revenue: float,
    expenses: float,
    net: float,
    outstanding: float,
    overdue_count: int,
) -> bool:
    """Send the daily business summary via email when WhatsApp is unavailable."""
    import os
    import smtplib
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText

    from jinja2 import Template

    display_name = (name or "").split()[0] if name else "there"

    # Build HTML body
    lines: list[str] = []
    if revenue > 0:
        lines.append(f"💰 Cash In: <b>₦{revenue:,.0f}</b>")
    if expenses > 0:
        lines.append(f"💸 Expenses: <b>₦{expenses:,.0f}</b>")
    if revenue > 0 or expenses > 0:
        emoji = "📈" if net >= 0 else "📉"
        lines.append(f"{emoji} Net: <b>₦{net:,.0f}</b>")
    if outstanding > 0:
        lines.append(f"⏳ Outstanding: <b>₦{outstanding:,.0f}</b>")
    if overdue_count > 0:
        s = "s" if overdue_count != 1 else ""
        lines.append(f"⚠️ Overdue: <b>{overdue_count}</b> invoice{s}")

    if not lines:
        body_html = "✨ All clear today — no new transactions or outstanding invoices."
    else:
        body_html = "<br>".join(lines)

    headline = "Your Daily Business Summary"

    tpl_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "email", "engagement_tip.html")
    try:
        with open(tpl_path) as f:
            tpl = Template(f.read())
        html_body = tpl.render(
            headline=headline,
            name=display_name,
            body_text=body_html,
            tip_text="Check your dashboard for the full picture.",
            cta_url="https://suoops.com/dashboard",
            cta_label="View Dashboard →",
        )
    except Exception:
        html_body = f"<p>Hi {display_name},</p><p>{headline}</p><p>{body_html}</p>"

    plain_lines = [line.replace("<b>", "").replace("</b>", "").replace("<br>", "\n") for line in lines]
    plain_body = (
        f"Hi {display_name},\n\n{headline}\n\n"
        + "\n".join(plain_lines)
        + "\n\nView dashboard: https://suoops.com/dashboard"
    )

    subject = "📊 Your Daily Business Summary — SuoOps"

    from app.utils.smtp import get_smtp_config

    smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

    if not smtp_user or not smtp_password:
        logger.warning("SMTP not configured, cannot send daily summary email to %s", to_email)
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
        logger.info("Daily summary email sent to %s", to_email)
        return True
    except Exception as e:
        logger.warning("Daily summary email failed for %s: %s", to_email, e)
        return False
