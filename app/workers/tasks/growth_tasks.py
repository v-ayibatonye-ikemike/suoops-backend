"""Growth-focused tasks — aggregate unpaid alerts, weekly free summary, payment upsell.

Three tasks that target the key funnel gaps:
- Aggregate unpaid notification: "You have ₦145,000 unpaid across 6 invoices"
- Weekly free-user summary: Same as daily Pro summary, but weekly for free users
- Payment-triggered upsell: After collecting ≥₦50K, nudge toward Pro
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func as sqlfunc
from sqlalchemy import or_

from app.core.config import settings
from app.db.session import session_scope
from app.utils.smtp import send_smtp_email as _send_smtp_email
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


def _is_valid_phone(phone: str | None) -> bool:
    if not phone:
        return False
    digits = phone.lstrip("+")
    return digits.isdigit() and len(digits) >= 10


# ═══════════════════════════════════════════════════════════════════════
# TASK 1: AGGREGATE UNPAID AMOUNT NOTIFICATION
# ═══════════════════════════════════════════════════════════════════════


@celery_app.task(
    name="growth.send_aggregate_unpaid_alerts",
    # NOTE: autoretry intentionally disabled. This task does bulk WhatsApp
    # sends; a retry after a partial run would re-deliver the same template
    # to users whose dedup row was not yet committed (we observed a 60s gap
    # duplicate exactly matching retry_backoff). The task is on a Mon+Thu
    # cron, so a single missed run is recoverable on the next schedule.
    soft_time_limit=300,
    time_limit=360,
)
def send_aggregate_unpaid_alerts() -> dict[str, Any]:
    """Notify business owners of their total unpaid amount across all invoices.

    Runs twice a week (Mon + Thu at 09:00 WAT).
    Only notifies users who have ≥₦5,000 unpaid across ≥2 invoices.
    Deduplicates: won't re-send if the same total was sent within 3 days.
    """
    from app.models.models import Invoice, User, UserEmailLog

    stats = {"whatsapp_sent": 0, "email_sent": 0, "skipped": 0, "failed": 0}

    try:
        with session_scope() as db:
            # Find users with pending revenue invoices
            unpaid_data = (
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.sum(Invoice.amount).label("total_unpaid"),
                    sqlfunc.count(Invoice.id).label("invoice_count"),
                )
                .filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.status.in_(["pending", "awaiting_confirmation"]),
                )
                .group_by(Invoice.issuer_id)
                .having(
                    sqlfunc.sum(Invoice.amount) >= 5000,
                    sqlfunc.count(Invoice.id) >= 2,
                )
                .all()
            )

            if not unpaid_data:
                logger.info("No users with significant unpaid invoices")
                return {"success": True, **stats}

            from app.bot.conversation_window import is_window_open
            from app.core.whatsapp import get_whatsapp_client

            client = get_whatsapp_client()

            # Check for recent sends (don't spam)
            three_days_ago = datetime.now(timezone.utc) - timedelta(days=3)

            # Pre-fetch all users in one query (avoid N+1)
            issuer_ids = [row.issuer_id for row in unpaid_data]
            users_by_id = {u.id: u for u in db.query(User).filter(User.id.in_(issuer_ids)).all()}

            for row in unpaid_data:
                user = users_by_id.get(row.issuer_id)
                if not user:
                    continue

                # Dedup: skip if we sent this type recently
                recent = (
                    db.query(UserEmailLog.id)
                    .filter(
                        UserEmailLog.user_id == user.id,
                        UserEmailLog.email_type == "aggregate_unpaid",
                        UserEmailLog.sent_at >= three_days_ago,
                    )
                    .first()
                )
                if recent:
                    stats["skipped"] += 1
                    continue

                total = float(row.total_unpaid)
                count = int(row.invoice_count)
                name = (user.name or "").split()[0] or "there"
                s = "s" if count != 1 else ""

                message = (
                    f"💰 *Payment Alert*\n\n"
                    f"Hi {name}, you have *₦{total:,.0f}* unpaid "
                    f"across *{count} invoice{s}*.\n\n"
                    f"💡 Send reminders to collect faster — "
                    f"or check your dashboard:\n"
                    f"🔗 suoops.com/dashboard\n\n"
                    f"_Tip: Businesses that follow up within 3 days "
                    f"collect 40% faster._"
                )

                # Claim the dedup slot BEFORE sending so a crash/retry mid-send
                # cannot cause a second delivery within the 3-day window.
                dedup_row = UserEmailLog(user_id=user.id, email_type="aggregate_unpaid")
                db.add(dedup_row)
                try:
                    db.commit()
                except Exception as commit_exc:  # noqa: BLE001
                    db.rollback()
                    logger.warning(
                        "aggregate_unpaid: dedup commit failed for user %s (%s); skipping",
                        user.id,
                        commit_exc,
                    )
                    stats["skipped"] += 1
                    continue

                sent = False
                has_phone = _is_valid_phone(user.phone)

                # Email first (free primary channel for owner alerts).
                if user.email:
                    subject = f"You have ₦{total:,.0f} unpaid across {count} invoice{s}"
                    plain = (
                        f"Hi {name},\n\n"
                        f"You have ₦{total:,.0f} unpaid across {count} invoice{s}.\n\n"
                        f"Send reminders to collect faster: https://suoops.com/dashboard\n\n"
                        f"— SuoOps"
                    )
                    if _send_smtp_email(user.email, subject, None, plain):
                        stats["email_sent"] += 1
                        sent = True

                # WhatsApp fallback (no email, or email failed) — budget-gated,
                # unpaid alerts are high value.
                if not sent and has_phone:
                    from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

                    if can_send_whatsapp(priority=True):
                        template_name = getattr(settings, "WHATSAPP_TEMPLATE_UNPAID_ALERT", None)
                        if template_name:
                            lang = settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                            ok = client.send_template(
                                user.phone,
                                template_name,
                                lang,
                                components=[
                                    {
                                        "type": "body",
                                        "parameters": [
                                            {"type": "text", "text": name},
                                            {"type": "text", "text": f"₦{total:,.0f}"},
                                            {"type": "text", "text": str(count)},
                                        ],
                                    }
                                ],
                            )
                            if ok:
                                record_whatsapp_send(priority=True)
                                stats["whatsapp_sent"] += 1
                                sent = True

                        if not sent and is_window_open(user.phone):
                            if client.send_text(user.phone, message):
                                record_whatsapp_send(priority=True)
                                stats["whatsapp_sent"] += 1
                                sent = True

                # Email fallback already attempted first above.
                if sent:
                    pass  # dedup row already committed above
                else:
                    # Send failed across all channels. Remove the dedup claim so
                    # the next scheduled run can try again.
                    try:
                        db.delete(dedup_row)
                        db.commit()
                    except Exception:  # noqa: BLE001
                        db.rollback()
                    stats["failed"] += 1

        logger.info(
            "Aggregate unpaid alerts: wa=%d email=%d skipped=%d failed=%d",
            stats["whatsapp_sent"],
            stats["email_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Aggregate unpaid alerts failed: %s", exc)
        raise


# ═══════════════════════════════════════════════════════════════════════
# TASK 2: WEEKLY FREE-USER SUMMARY
# ═══════════════════════════════════════════════════════════════════════


@celery_app.task(
    name="growth.send_weekly_free_summary",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def send_weekly_free_summary() -> dict[str, Any]:
    """Send weekly business summary to FREE users who have invoices.

    Runs every Monday at 09:00 WAT. Mirrors the daily Pro summary but
    covers the past 7 days and targets free users only.
    """
    from app.models.models import Invoice, SubscriptionPlan, User

    stats = {"whatsapp_sent": 0, "email_sent": 0, "skipped": 0, "failed": 0}

    try:
        with session_scope() as db:
            now_utc = datetime.now(timezone.utc)
            week_ago = now_utc - timedelta(days=7)

            # Free users who have created at least 1 revenue invoice ever
            free_users_with_invoices = (
                db.query(User)
                .filter(
                    User.plan == SubscriptionPlan.FREE,
                    (User.phone != None) | (User.email != None),  # noqa: E711
                )
                .join(Invoice, Invoice.issuer_id == User.id)
                .filter(Invoice.invoice_type == "revenue")
                .group_by(User.id)
                .having(sqlfunc.count(Invoice.id) >= 1)
                .all()
            )

            if not free_users_with_invoices:
                logger.info("No free users with invoices for weekly summary")
                return {"success": True, **stats}

            from app.bot.conversation_window import is_window_open
            from app.core.whatsapp import get_whatsapp_client

            client = get_whatsapp_client()

            # Pre-fetch all summary stats in bulk (avoids 4 queries × N users)
            user_ids = [u.id for u in free_users_with_invoices]

            # Revenue collected this week per user
            revenue_map = dict(
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.coalesce(sqlfunc.sum(Invoice.amount), 0),
                )
                .filter(
                    Invoice.issuer_id.in_(user_ids),
                    Invoice.invoice_type == "revenue",
                    Invoice.status == "paid",
                    Invoice.paid_at >= week_ago,
                )
                .group_by(Invoice.issuer_id)
                .all()
            )

            # Expenses this week per user
            expense_map = dict(
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.coalesce(sqlfunc.sum(Invoice.amount), 0),
                )
                .filter(
                    Invoice.issuer_id.in_(user_ids),
                    Invoice.invoice_type == "expense",
                    Invoice.created_at >= week_ago,
                )
                .group_by(Invoice.issuer_id)
                .all()
            )

            # Total outstanding per user
            outstanding_map = dict(
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.coalesce(sqlfunc.sum(Invoice.amount), 0),
                )
                .filter(
                    Invoice.issuer_id.in_(user_ids),
                    Invoice.invoice_type == "revenue",
                    Invoice.status.in_(["pending", "awaiting_confirmation"]),
                    # Abandoned storefront carts aren't money owed — keep this
                    # consistent with the dashboard + daily summary.
                    or_(
                        Invoice.channel.is_(None),
                        Invoice.channel != "storefront",
                        Invoice.status != "pending",
                    ),
                )
                .group_by(Invoice.issuer_id)
                .all()
            )

            # Overdue count per user
            overdue_map = dict(
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.count(Invoice.id),
                )
                .filter(
                    Invoice.issuer_id.in_(user_ids),
                    Invoice.invoice_type == "revenue",
                    Invoice.status == "pending",
                    Invoice.due_date != None,  # noqa: E711
                    Invoice.due_date < now_utc,
                    or_(
                        Invoice.channel.is_(None),
                        Invoice.channel != "storefront",
                    ),
                )
                .group_by(Invoice.issuer_id)
                .all()
            )

            for user in free_users_with_invoices:
                try:
                    has_phone = _is_valid_phone(user.phone)
                    name = (user.name or "").split()[0] or "there"

                    revenue_week = float(revenue_map.get(user.id, 0))
                    expenses_week = float(expense_map.get(user.id, 0))
                    outstanding = float(outstanding_map.get(user.id, 0))
                    overdue_count = int(overdue_map.get(user.id, 0))

                    # Skip if zero activity AND zero outstanding
                    if revenue_week == 0 and expenses_week == 0 and outstanding == 0:
                        stats["skipped"] += 1
                        continue

                    net = revenue_week - expenses_week

                    message = "📊 *Your Weekly Summary*\n\n"
                    message += f"Hi {name}, here's your week:\n\n"

                    if revenue_week > 0:
                        message += f"💰 Collected: ₦{revenue_week:,.0f}\n"
                    if expenses_week > 0:
                        message += f"💸 Spent: ₦{expenses_week:,.0f}\n"
                    if revenue_week > 0 or expenses_week > 0:
                        emoji = "📈" if net >= 0 else "📉"
                        message += f"{emoji} Net: ₦{net:,.0f}\n"

                    message += "\n"

                    if outstanding > 0:
                        message += f"⏳ Still unpaid: ₦{outstanding:,.0f}\n"
                    if overdue_count > 0:
                        s = "s" if overdue_count != 1 else ""
                        message += f"⚠️ Overdue: {overdue_count} invoice{s}\n"

                    message += (
                        "\n🔗 suoops.com/dashboard\n\n"
                        "_Every feature is free — tax reports, inventory, daily "
                        "summaries & more. Fees as low as 0.5%; on your storefront "
                        "customers pay the 3%._"
                    )

                    sent = False

                    # Try WhatsApp (weekly summary = low priority, prefer email)
                    if has_phone and user.email:
                        # If user has email, prefer email to save WhatsApp budget
                        pass
                    elif has_phone and is_window_open(user.phone):
                        from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

                        if can_send_whatsapp():
                            if client.send_text(user.phone, message):
                                record_whatsapp_send()
                                stats["whatsapp_sent"] += 1
                                sent = True

                    # Email fallback
                    if not sent and user.email:
                        subject = "Your Weekly Business Summary"
                        if _send_smtp_email(user.email, subject, None, message.replace("*", "")):
                            stats["email_sent"] += 1
                            sent = True

                    if not sent:
                        stats["failed"] += 1

                except Exception as e:
                    logger.warning("Weekly summary failed for user %s: %s", user.id, e)
                    stats["failed"] += 1

        logger.info(
            "Weekly free summary: wa=%d email=%d skipped=%d failed=%d",
            stats["whatsapp_sent"],
            stats["email_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Weekly free summary failed: %s", exc)
        raise


# ═══════════════════════════════════════════════════════════════════════
# TASK 3: PAYMENT-TRIGGERED UPSELL
# ═══════════════════════════════════════════════════════════════════════


@celery_app.task(
    name="growth.send_payment_upsells",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def send_payment_upsells() -> dict[str, Any]:
    """Send outcome-based upsell to free users who have collected money.

    Triggers when a free user has:
    - Received ≥2 payments, OR
    - Collected ≥₦50,000 total

    Sent once per user (tracked via UserEmailLog).
    Runs daily at 15:00 WAT.
    """
    from app.models.models import Invoice, SubscriptionPlan, User, UserEmailLog

    stats = {"whatsapp_sent": 0, "email_sent": 0, "skipped": 0, "failed": 0}

    try:
        with session_scope() as db:
            # Find free users who have collected payments
            paid_data = (
                db.query(
                    Invoice.issuer_id,
                    sqlfunc.sum(Invoice.amount).label("total_collected"),
                    sqlfunc.count(Invoice.id).label("paid_count"),
                )
                .filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.status == "paid",
                )
                .group_by(Invoice.issuer_id)
                .having((sqlfunc.count(Invoice.id) >= 2) | (sqlfunc.sum(Invoice.amount) >= 50000))
                .all()
            )

            if not paid_data:
                logger.info("No users qualify for payment upsell")
                return {"success": True, **stats}

            for row in paid_data:
                user = db.query(User).filter(User.id == row.issuer_id).first()
                if not user:
                    continue

                # Only target free users
                if user.plan != SubscriptionPlan.FREE:
                    stats["skipped"] += 1
                    continue

                # Dedup: only send once
                already = (
                    db.query(UserEmailLog.id)
                    .filter(
                        UserEmailLog.user_id == user.id,
                        UserEmailLog.email_type == "payment_upsell",
                    )
                    .first()
                )
                if already:
                    stats["skipped"] += 1
                    continue

                total = float(row.total_collected)
                count = int(row.paid_count)
                name = (user.name or "").split()[0] or "there"

                sent = False

                # Promotional upsells use the existing deduplicated email only.
                if user.email:
                    subject = f"You've collected ₦{total:,.0f} through SuoOps 🎉"
                    plain = (
                        f"Hi {name},\n\n"
                        f"You've received {count} payment{'s' if count != 1 else ''} "
                        f"totalling ₦{total:,.0f} through SuoOps.\n\n"
                        f"Every feature is free — tax reports, daily summaries, "
                        f"customer insights & more. Fees as low as 0.5% — and on your "
                        f"storefront, customers pay the 3%, so you keep your full price.\n\n"
                        f"Keep it flowing: share your storefront or top up your wallet at "
                        f"https://suoops.com/dashboard/billing/purchase\n\n"
                        f"— SuoOps"
                    )
                    if _send_smtp_email(user.email, subject, None, plain):
                        stats["email_sent"] += 1
                        sent = True

                if sent:
                    db.add(UserEmailLog(user_id=user.id, email_type="payment_upsell"))
                    db.commit()  # Commit immediately so retries won't re-send
                else:
                    stats["failed"] += 1

        logger.info(
            "Payment upsells: wa=%d email=%d skipped=%d failed=%d",
            stats["whatsapp_sent"],
            stats["email_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Payment upsell task failed: %s", exc)
        raise


# ═══════════════════════════════════════════════════════════════════════
# TASK 4: STOREFRONT COMPLETION / ENABLE NUDGES
# ═══════════════════════════════════════════════════════════════════════

# One dedup key per milestone → each owner gets each nudge at most once.
STOREFRONT_NUDGE_TYPES = {
    "enable": "nudge_enable_storefront",
    "payments": "nudge_enable_payments",
    "profile": "nudge_complete_storefront",
}


def _storefront_nudge_for(db, user):
    """The single highest-priority storefront nudge this owner needs.

    Returns ``(type_key, whatsapp_msg, email_subject, email_plain)`` or ``None``
    when the store is already complete & live. Priority: open the store → turn on
    online payments → finish a thin profile (matches the live-search gate).
    """
    from app.models.inventory_models import Product

    name = (user.name or "").split()[0] or "there"
    dash = "https://suoops.com/dashboard/settings"

    if not user.storefront_enabled:
        return (
            "enable",
            f"🛍️ *Open your free shop*\n\nHi {name}, turn on your SuoOps storefront so "
            f"customers can browse your products and order — no website needed.\n👉 {dash}\n\n"
            f"_It's free._",
            "Open your free SuoOps shop",
            f"Hi {name},\n\nTurn on your SuoOps storefront so customers can browse and order — "
            f"no website needed. It's free.\n\n{dash}\n\n— SuoOps",
        )

    if not user.paystack_subaccount_active:
        return (
            "payments",
            f"💳 *Get paid online*\n\nHi {name}, turn on online payments so customers can pay "
            f"you by card or transfer right on your store — money lands in your bank.\n👉 {dash}",
            "Turn on online payments for your shop",
            f"Hi {name},\n\nTurn on online payments so customers can pay you by card or transfer "
            f"on your store — money lands in your bank.\n\n{dash}\n\n— SuoOps",
        )

    # Enabled + payments on — is it complete enough to show up in search?
    missing = []
    if not user.logo_url:
        missing.append("a logo")
    if not (user.storefront_description or "").strip():
        missing.append("a short description")
    if not user.storefront_state:
        missing.append("your location")
    listable = (
        db.query(Product.id)
        .filter(
            Product.user_id == user.id,
            Product.is_active.is_(True),
            Product.description.isnot(None),
            Product.description != "",
            Product.image_url.isnot(None),
            Product.image_url != "",
        )
        .first()
    )
    if not listable:
        missing.append("a product with a photo & description")
    if not missing:
        return None

    missing_str = ", ".join(missing)
    return (
        "profile",
        f"✨ *Finish your shop to show up in search*\n\nHi {name}, add {missing_str} so your "
        f"store appears when customers search SuoOps.\n👉 {dash}",
        "Finish your shop to appear in search",
        f"Hi {name},\n\nAdd {missing_str} so your store appears when customers search SuoOps.\n\n"
        f"{dash}\n\n— SuoOps",
    )


@celery_app.task(
    name="growth.send_storefront_completion_nudges",
    soft_time_limit=300,
    time_limit=360,
)
def send_storefront_completion_nudges() -> dict[str, Any]:
    """Weekly nudge to owners to open a storefront, turn on online payments, or
    finish a thin storefront so it appears in search.

    Only targets engaged owners (have a product OR an invoice), reachable
    (WhatsApp-verified or email), older than 24h. One message per user per
    milestone (UserEmailLog dedup); WhatsApp only inside the 24h window and
    within budget, otherwise email.
    """
    from app.models.inventory_models import Product
    from app.models.models import Invoice, User, UserEmailLog

    stats = {"whatsapp_sent": 0, "email_sent": 0, "skipped": 0, "failed": 0}
    try:
        with session_scope() as db:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            product_owners = db.query(Product.user_id).distinct().subquery()
            invoice_owners = db.query(Invoice.issuer_id).distinct().subquery()
            candidates = (
                db.query(User)
                .filter(
                    User.created_at <= cutoff,
                    User.email.isnot(None),
                    or_(
                        User.id.in_(db.query(product_owners)),
                        User.id.in_(db.query(invoice_owners)),
                    ),
                )
                .all()
            )
            if not candidates:
                return {"success": True, **stats}

            for user in candidates:
                nudge = _storefront_nudge_for(db, user)
                if not nudge:
                    continue
                type_key, _wa_msg, subject, plain = nudge
                email_type = STOREFRONT_NUDGE_TYPES[type_key]

                # Dedup: at most once per user per milestone (ever).
                already = (
                    db.query(UserEmailLog.id)
                    .filter(
                        UserEmailLog.user_id == user.id,
                        UserEmailLog.email_type == email_type,
                    )
                    .first()
                )
                if already:
                    stats["skipped"] += 1
                    continue

                # Claim the dedup slot BEFORE sending (crash/retry-safe).
                dedup_row = UserEmailLog(user_id=user.id, email_type=email_type)
                db.add(dedup_row)
                try:
                    db.commit()
                except Exception:  # noqa: BLE001
                    db.rollback()
                    stats["skipped"] += 1
                    continue

                sent = False
                if user.email:
                    if _send_smtp_email(user.email, subject, None, plain):
                        stats["email_sent"] += 1
                        sent = True

                if not sent:
                    # Free the dedup claim so a later run can retry.
                    try:
                        db.delete(dedup_row)
                        db.commit()
                    except Exception:  # noqa: BLE001
                        db.rollback()
                    stats["failed"] += 1

        logger.info(
            "Storefront completion nudges: wa=%d email=%d skipped=%d failed=%d",
            stats["whatsapp_sent"],
            stats["email_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Storefront completion nudges failed: %s", exc)
        raise
