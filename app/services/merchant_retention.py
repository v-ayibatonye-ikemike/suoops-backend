from __future__ import annotations

import datetime as dt
import logging
from dataclasses import asdict, dataclass
from decimal import Decimal

import jwt
from sqlalchemy import and_, case, func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.security import ALGORITHM
from app.models.inventory_models import Product, StockMovement, StockMovementType, storefront_product_conditions
from app.models.models import EmailSuppression, Invoice, User, UserEmailLog
from app.models.retention_models import (
    MerchantLifecycle,
    RetentionContact,
    RetentionEnrollment,
    RetentionExperiment,
)
from app.models.team_models import TeamMember
from app.services.business_health import business_health_score
from app.services.shipping import shipbubble
from app.utils.smtp import get_retention_smtp_configs, send_retention_email

logger = logging.getLogger(__name__)
UTC = dt.timezone.utc


def outreach_configured() -> bool:
    return settings.RETENTION_OUTREACH_ENABLED and any(outreach_channels().values())


def outreach_channels() -> dict[str, bool]:
    return {
        "email": bool(get_retention_smtp_configs()),
        "whatsapp": bool(
            settings.WHATSAPP_TEMPLATE_UNPAID_ALERT and settings.WHATSAPP_API_KEY and settings.WHATSAPP_PHONE_NUMBER_ID
        ),
    }


def outreach_enabled(db: Session) -> bool:
    control = db.query(RetentionExperiment).filter_by(id=1).populate_existing().first()
    return bool(outreach_configured() and control and control.enabled)


def utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def whatsapp_event_time(value: object) -> dt.datetime | None:
    try:
        if not isinstance(value, (str, int)):
            raise ValueError("missing timestamp")
        return dt.datetime.fromtimestamp(int(value), UTC)
    except (ValueError, OverflowError, OSError):
        logger.warning("Retention attribution skipped: invalid WhatsApp event timestamp")
        return None


def business_records():
    """Exclude cancelled records and unconverted storefront carts, not real expenses."""
    return and_(
        Invoice.status != "cancelled",
        or_(Invoice.channel.is_(None), Invoice.channel != "storefront", Invoice.status != "pending"),
    )


def meaningful_stock_records():
    return and_(
        StockMovement.quantity != 0,
        or_(StockMovement.movement_type == StockMovementType.PURCHASE, StockMovement.created_by != "system"),
    )


def merchant_query(db: Session):
    excluded = [email.strip().lower() for email in (settings.METRICS_EXCLUDED_EMAILS or "").split(",") if email.strip()]
    return db.query(User).filter(
        User.flagged_for_review.is_(False),
        ~User.id.in_(db.query(TeamMember.user_id)),
        or_(User.email.is_(None), ~func.lower(User.email).in_(excluded)),
    )


@dataclass(frozen=True)
class RetentionFacts:
    user_id: int
    name: str
    signup_at: dt.datetime
    first_invoice_at: dt.datetime | None
    first_payment_at: dt.datetime | None
    last_activity_at: dt.datetime | None
    invoices: int
    paid_invoices: int
    recent_14: int
    previous_28: int
    recent_30: int
    outstanding_count: int
    outstanding_by_currency: dict[str, Decimal]
    low_stock_count: int
    days_inactive: int
    declining: bool
    health_score: int
    storefront_delivery_ready: bool = False
    has_physical_products: bool = False


def retention_facts(db: Session, user: User, now: dt.datetime) -> RetentionFacts:
    revenue = Invoice.invoice_type == "revenue"
    paid = and_(revenue, Invoice.status == "paid", Invoice.paid_at <= now)
    # Payment confirmation is meaningful activity; logins and page views are not.
    activity = case(
        (and_(Invoice.paid_at > Invoice.created_at, Invoice.paid_at <= now), Invoice.paid_at),
        else_=Invoice.created_at,
    )
    outstanding = and_(revenue, Invoice.status == "pending")
    row = (
        db.query(
            func.min(case((revenue, Invoice.created_at))).label("first_invoice"),
            func.min(case((paid, Invoice.paid_at))).label("first_payment"),
            func.max(activity).label("last_activity"),
            func.count(case((revenue, 1))).label("invoices"),
            func.count(case((paid, 1))).label("paid"),
            func.count(case((and_(revenue, Invoice.created_at >= now - dt.timedelta(days=14)), 1))).label("recent"),
            func.count(
                case(
                    (
                        and_(
                            revenue,
                            Invoice.created_at >= now - dt.timedelta(days=42),
                            Invoice.created_at < now - dt.timedelta(days=14),
                        ),
                        1,
                    )
                )
            ).label("previous"),
            func.count(case((and_(revenue, Invoice.created_at >= now - dt.timedelta(days=30)), 1))).label("month"),
            func.count(case((outstanding, 1))).label("outstanding_count"),
        )
        .filter(
            Invoice.issuer_id == user.id,
            business_records(),
            Invoice.created_at <= now,
        )
        .one()
    )
    outstanding_by_currency = {
        currency: Decimal(amount)
        for currency, amount in db.query(Invoice.currency, func.sum(Invoice.amount))
        .filter(
            Invoice.issuer_id == user.id,
            business_records(),
            outstanding,
            Invoice.created_at <= now,
        )
        .group_by(Invoice.currency)
        .all()
    }
    low_stock = (
        db.query(func.count(Product.id))
        .filter(
            Product.user_id == user.id,
            Product.is_active.is_(True),
            Product.track_stock.is_(True),
            Product.quantity_in_stock <= Product.reorder_level,
        )
        .scalar()
        or 0
    )
    last = utc(row.last_activity) if row.last_activity else None
    stock_activity = (
        db.query(func.max(StockMovement.created_at))
        .filter(
            StockMovement.user_id == user.id,
            StockMovement.created_at <= now,
            meaningful_stock_records(),
        )
        .scalar()
    )
    if stock_activity and (last is None or utc(stock_activity) > last):
        last = utc(stock_activity)
    days = max(0, (now - (last or utc(user.created_at))).days)
    collection = row.paid / row.invoices * 100 if row.invoices else 0
    storefront_delivery_ready = bool(
        settings.SHIPBUBBLE_CHECKOUT_ENABLED
        and shipbubble.enabled()
        and user.storefront_enabled
        and user.store_status == "active"
        and user.storefront_slug
        and user.paystack_subaccount_active
        and user.paystack_subaccount_code
        and user.phone_verified
        and user.storefront_city
        and user.storefront_state
        and db.query(Product.id).filter(
            Product.user_id == user.id,
            *storefront_product_conditions(),
            Product.fulfilment_type == "physical",
            Product.selling_price > 0,
            or_(Product.track_stock.is_(False), Product.quantity_in_stock > 0),
        ).first()
    )
    return RetentionFacts(
        user.id,
        user.business_name or user.name,
        utc(user.created_at),
        utc(row.first_invoice) if row.first_invoice else None,
        utc(row.first_payment) if row.first_payment else None,
        last,
        row.invoices,
        row.paid,
        row.recent,
        row.previous,
        row.month,
        row.outstanding_count,
        outstanding_by_currency,
        low_stock,
        days,
        row.previous >= 6 and row.recent < row.previous / 4,
        business_health_score(days if last else None, row.invoices, collection, row.month, row.invoices),
        storefront_delivery_ready,
        bool(db.query(Product.id).filter(
            Product.user_id == user.id,
            Product.is_active.is_(True),
            Product.fulfilment_type == "physical",
        ).first()),
    )


def refresh_lifecycle(db: Session, facts: RetentionFacts, now: dt.datetime) -> MerchantLifecycle:
    db.query(User.id).filter(User.id == facts.user_id).with_for_update().one()
    profile = db.query(MerchantLifecycle).filter_by(user_id=facts.user_id).populate_existing().first()
    if profile is None:
        profile = MerchantLifecycle(user_id=facts.user_id, state="new", state_since=now)
        db.add(profile)
    returned = (
        profile.state in {"at_risk", "dormant"}
        and facts.last_activity_at is not None
        and facts.last_activity_at > utc(profile.last_activity_at or profile.state_since)
        and facts.days_inactive < 14
    )
    if returned:
        profile.reactivated_at = facts.last_activity_at
    if facts.days_inactive >= 30:
        state = "dormant"
    elif returned or (profile.reactivated_at and now - utc(profile.reactivated_at) < dt.timedelta(days=7)):
        state = "reactivated"
    elif facts.invoices == 0 and now - facts.signup_at < dt.timedelta(days=7):
        state = "new"
    elif facts.days_inactive >= 14 or facts.declining or facts.invoices == 0:
        state = "at_risk"
    elif facts.invoices == 1 and not facts.first_payment_at:
        state = "activated"
    elif facts.health_score >= 60 and facts.recent_14 >= 3 and facts.previous_28 >= 3:
        state = "healthy"
    else:
        state = "engaged"
    if profile.state != state:
        profile.state, profile.state_since = state, now
    profile.last_activity_at = facts.last_activity_at
    db.flush()
    return profile


@dataclass(frozen=True)
class RetentionRecommendation:
    trigger: str
    title: str
    reason: str
    action_url: str
    dedupe_key: str


def recommendation(
    facts: RetentionFacts,
    profile: MerchantLifecycle,
    now: dt.datetime,
) -> RetentionRecommendation | None:
    anchor = (facts.last_activity_at or facts.signup_at).isoformat()
    if profile.state == "healthy":
        return None
    if profile.state == "reactivated":
        return RetentionRecommendation(
            "reactivated",
            "Review your business priorities",
            "Your business has recorded activity again. Review collections and stock before your next sale.",
            "/dashboard",
            f"reactivated:{utc(profile.reactivated_at).isoformat()}",
        )
    if facts.invoices == 0 and now - facts.signup_at >= dt.timedelta(days=1):
        return RetentionRecommendation(
            "activation",
            "Create your first invoice",
            "No invoice is recorded yet. Sold something through WhatsApp, Instagram or your shop? "
            "Record the sale in SuoOps and confirm payment only when received. "
            "Keep invoices and expense records ready for tax review, rather than reconstructing them later. "
            "Ask SuoOps can guide you through your first invoice for review.",
            "/dashboard?assistant=invoice",
            "activation",
        )
    if (
        facts.first_invoice_at
        and not facts.first_payment_at
        and (dt.timedelta(days=3) <= now - facts.first_invoice_at < dt.timedelta(days=14))
        and facts.outstanding_count
    ):
        trigger = "first_payment"
    elif facts.days_inactive >= 30:
        trigger = "winback_30"
    elif facts.days_inactive >= 21:
        trigger = "reactivation_21"
    elif facts.days_inactive >= 14:
        trigger = "reactivation_14"
    elif facts.declining:
        trigger = "declining"
    elif facts.outstanding_count:
        trigger = "collections"
    else:
        return None
    if facts.outstanding_count:
        title = "Review payment reminders"
        amounts = " + ".join(
            f"{currency} {amount:,.2f}" for currency, amount in sorted(facts.outstanding_by_currency.items())
        )
        reason = (
            f"You have {facts.outstanding_count} unpaid invoices worth {amounts}. "
            "Review them and prepare customer reminders in Collections."
        )
        url = "/dashboard/collections"
    elif facts.low_stock_count:
        title = "Review restock suggestions"
        reason = f"{facts.low_stock_count} tracked products are at or below their reorder level. Review your stock."
        url = "/dashboard/inventory"
    elif facts.invoices and facts.has_physical_products and facts.days_inactive >= 14:
        if facts.storefront_delivery_ready:
            title = "Share your storefront"
            reason = (
                "Your storefront has physical goods available. Share its link: the buyer chooses an available "
                "courier and pays for the order and delivery at checkout. For pickup delivery, pack the order, "
                "tap Book courier pickup, add a package photo and save. SuoOps requests the booking; "
                "wait for confirmation, then the courier handles pickup and delivery. For drop-off delivery, "
                "take the parcel to the designated station instead. "
                "Delivery options depend on the address and courier availability."
            )
        else:
            title = "Discover your SuoOps storefront"
            reason = (
                "You have physical products in SuoOps. A storefront lets customers browse and order from one link. "
                "Review your listings and complete any missing storefront and online-payment setup first. "
                "Where courier delivery is available at checkout, buyers can choose and pay for it. "
                "For pickup, you pack the order, add a package photo and request the booking; wait for confirmation. "
                "Drop-off options require taking the parcel to the courier station instead."
            )
        url = "/dashboard/settings#storefront"
    elif facts.invoices:
        title = "Keep each sale recorded"
        reason = (
            f"You recorded {facts.recent_30} invoices in the last 30 days. "
            "Have you also recorded sales made outside SuoOps? Invoice each sale, confirm payment only "
            "when received, and keep expense records ready for tax review. Start with your next sale."
        )
        url = "/dashboard?assistant=invoice"
    else:
        # No financial or usage evidence for a useful win-back; do not manufacture one.
        return None
    return RetentionRecommendation(trigger, title, reason, url, f"{trigger}:{anchor}")


def snapshot(db: Session, user: User, now: dt.datetime) -> dict:
    facts = retention_facts(db, user, now)
    profile = refresh_lifecycle(db, facts, now)
    action = recommendation(facts, profile, now)
    return {
        "user_id": user.id,
        "business_name": facts.name,
        "state": profile.state,
        "state_since": profile.state_since,
        "health_score": facts.health_score,
        "days_inactive": facts.days_inactive,
        "whatsapp_opted_in": profile.whatsapp_opted_in and profile.consented_phone == user.phone,
        "phone_verified": user.phone_verified,
        "recommendation": asdict(action) if action else None,
        "email_enabled": bool(user.email and not profile.email_opted_out),
        "email_available": bool(user.email),
        "channel": retention_channel(db, user, profile, facts),
        "facts": asdict(facts),
    }


def set_consent(db: Session, user_id: int, enabled: bool, source: str, now: dt.datetime) -> None:
    user = db.get(User, user_id)
    if user is None or (enabled and (not user.phone_verified or not user.phone)):
        raise ValueError("Verify your own WhatsApp phone number before enabling business nudges.")
    profile = refresh_lifecycle(db, retention_facts(db, user, now), now)
    profile.whatsapp_opted_in = enabled
    profile.consent_at, profile.consent_source = now, source
    profile.consented_phone = user.phone if enabled else None
    if not enabled and source == "whatsapp":
        profile.email_opted_out = True
    db.commit()


def set_email_consent(db: Session, user_id: int, enabled: bool, now: dt.datetime) -> None:
    user = db.get(User, user_id)
    if user is None or (enabled and not user.email):
        raise ValueError("Add an email address before enabling business emails.")
    profile = refresh_lifecycle(db, retention_facts(db, user, now), now)
    profile.email_opted_out = not enabled
    db.commit()


def email_unsubscribe_token(user: User, now: dt.datetime) -> str:
    return jwt.encode(
        {
            "sub": str(user.id), "email": (user.email or "").strip().lower(),
            "type": "retention_unsubscribe", "aud": "retention-unsubscribe",
            "exp": now + dt.timedelta(days=365),
        },
        settings.JWT_SECRET, algorithm=ALGORITHM,
    )


def unsubscribe_email(db: Session, token: str, now: dt.datetime) -> None:
    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET, algorithms=[ALGORITHM], audience="retention-unsubscribe",
            options={"require": ["sub", "email", "type", "exp", "aud"]},
        )
        if payload["type"] != "retention_unsubscribe":
            raise ValueError("Wrong token purpose")
        user_id = int(payload["sub"])
    except (jwt.InvalidTokenError, ValueError, TypeError) as exc:
        raise ValueError("This unsubscribe link is invalid or expired. Use your account settings.") from exc
    user = db.get(User, user_id)
    if user is None or (user.email or "").strip().lower() != payload["email"]:
        raise ValueError("This link no longer matches the account email. Use your account settings.")
    set_email_consent(db, user_id, False, now)


def retention_channel(db: Session, user: User, profile: MerchantLifecycle, facts: RetentionFacts) -> str | None:
    channels = outreach_channels()
    if (
        channels["email"] and user.email and not profile.email_opted_out
        and not db.query(EmailSuppression.id).filter(
            EmailSuppression.email == user.email.strip().lower()
        ).first()
    ):
        return "email"
    if (
        channels["whatsapp"] and user.phone and user.phone_verified and profile.whatsapp_opted_in
        and profile.consented_phone == user.phone and facts.outstanding_count
        and set(facts.outstanding_by_currency) == {"NGN"}
    ):
        return "whatsapp"
    return None


def pilot_protected(db: Session, user_id: int, now: dt.datetime | None = None) -> bool:
    """Suppress legacy campaigns while ongoing retention manages outreach, plus historical opt-outs."""
    profile = db.get(MerchantLifecycle, user_id)
    if profile and (profile.email_opted_out or (profile.consent_at and not profile.whatsapp_opted_in)):
        return True
    if settings.RETENTION_OUTREACH_ENABLED:
        return True
    current = now or dt.datetime.now(UTC)
    if db.query(RetentionContact.id).filter(
        RetentionContact.user_id == user_id, RetentionContact.created_at > current - dt.timedelta(days=7)
    ).first():
        return True
    return (
        db.query(RetentionEnrollment.user_id)
        .join(
            RetentionExperiment,
            RetentionExperiment.id == RetentionEnrollment.experiment_id,
        )
        .filter(
            RetentionEnrollment.user_id == user_id,
            or_(RetentionExperiment.started_at.is_(None), RetentionExperiment.ends_at > current),
        )
        .first()
        is not None
    )


def set_outreach_enabled(db: Session, enabled: bool, now: dt.datetime, admin_id: int) -> RetentionExperiment:
    control = db.query(RetentionExperiment).filter_by(id=1).with_for_update().first()
    if enabled and not outreach_configured():
        raise ValueError("The outreach kill switch and an existing email or WhatsApp sender must be configured first.")
    if control is None:
        control = RetentionExperiment(id=1, created_at=now, created_by=admin_id, enabled=False)
        db.add(control)
    if enabled and control.started_at is None:
        control.started_at = now
    control.enabled = enabled
    db.commit()
    return control


def send_retention_contact(db: Session, user_id: int, now: dt.datetime) -> str:
    from app.core.whatsapp import get_whatsapp_client
    from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

    if not settings.RETENTION_OUTREACH_ENABLED:
        return "disabled"
    # Serialize per merchant. Persist the reservation BEFORE the external side effect;
    # an ambiguous timeout/crash is never automatically retried into a duplicate send.
    user = merchant_query(db).filter(User.id == user_id).with_for_update().first()
    if not user or not outreach_enabled(db):
        return "ineligible"
    facts = retention_facts(db, user, now)
    profile = refresh_lifecycle(db, facts, now)
    action = recommendation(facts, profile, now)
    channel = retention_channel(db, user, profile, facts)
    if not action or not channel:
        return "ineligible"
    if not outreach_configured():
        logger.warning("Retention outreach skipped: WhatsApp template or sender is not configured")
        return "unconfigured"
    recent = (
        db.query(RetentionContact)
        .filter(RetentionContact.user_id == user.id)
        .order_by(
            RetentionContact.created_at.desc(),
        )
        .first()
    )
    if recent and utc(recent.created_at) > now - dt.timedelta(days=7):
        return "cooldown"
    if db.query(UserEmailLog.id).filter(
        UserEmailLog.user_id == user.id, UserEmailLog.sent_at > now - dt.timedelta(days=7)
    ).first():
        return "cooldown"
    if db.query(RetentionContact.id).filter_by(user_id=user.id, dedupe_key=action.dedupe_key).first():
        return "duplicate"
    if channel == "whatsapp" and not can_send_whatsapp(priority=False):
        return "budget"
    contact = RetentionContact(
        user_id=user.id,
        experiment_id=None,
        channel=channel,
        trigger=action.trigger,
        dedupe_key=action.dedupe_key,
        state=profile.state,
        status="reserved",
        created_at=now,
    )
    db.add(contact)
    db.commit()
    # Recheck consent/kill switches after committing the reservation.
    db.refresh(profile)
    db.refresh(user)
    if (
        not outreach_enabled(db)
        or retention_channel(db, user, profile, facts) != channel
    ):
        contact.status = "cancelled"
        db.commit()
        return "cancelled"
    name = " ".join(facts.name.split())[:80]
    try:
        if channel == "email":
            if not user.email:
                raise ValueError("Retention email recipient disappeared after eligibility check")
            token = email_unsubscribe_token(user, now)
            unsubscribe_url = f"https://api.suoops.com/ai/retention/unsubscribe?token={token}"
            body = (
                f"Hi {name},\n\n{action.reason}\n\n"
                f"{action.title}: https://suoops.com{action.action_url}\n\n"
                "SuoOps business updates for your existing account. "
                "No customer messages are sent without your review.\n"
                f"Stop these emails: {unsubscribe_url}\n\nSuoOps"
            )
            accepted = send_retention_email(user.email, action.title, body, unsubscribe_url)
            contact.status = "accepted" if accepted else "failed"
            if not accepted:
                contact.error = "Email was not accepted by the configured sender."
        else:
            if not user.phone:
                raise ValueError("Retention WhatsApp recipient disappeared after eligibility check")
            components = [{"type": "body", "parameters": [
                {"type": "text", "text": name},
                {"type": "text", "text": f"₦{facts.outstanding_by_currency['NGN']:,.0f}"},
                {"type": "text", "text": str(facts.outstanding_count)},
            ]}]
            contact.message_id = get_whatsapp_client().send_template_with_id(
                user.phone, settings.WHATSAPP_TEMPLATE_UNPAID_ALERT,
                settings.WHATSAPP_TEMPLATE_LANGUAGE or "en", components,
            )
            contact.status = "accepted" if contact.message_id else "unknown"
            if not contact.message_id:
                contact.error = "No WhatsApp message ID; do not automatically resend."
    except Exception:
        contact.status, contact.error = "unknown", "Provider outcome unknown; do not automatically resend."
        db.commit()
        logger.exception("Retention provider failed for contact %s", contact.id)
        raise
    if contact.status == "accepted":
        db.commit()
        if channel == "whatsapp":
            record_whatsapp_send(priority=False)
    elif contact.error:
        logger.warning("Retention contact %s: %s", contact.id, contact.error)
    db.commit()
    return contact.status


def record_response(db: Session, user_id: int, now: dt.datetime, context_id: str | None) -> None:
    query = db.query(RetentionContact).filter(
        RetentionContact.user_id == user_id,
        RetentionContact.channel == "whatsapp",
        RetentionContact.message_id.isnot(None),
        RetentionContact.created_at >= now - dt.timedelta(days=7),
        RetentionContact.created_at <= now,
    )
    if context_id:
        query = query.filter(RetentionContact.message_id == context_id)
    contact = query.order_by(RetentionContact.created_at.desc()).first()
    if contact and contact.responded_at is None:
        contact.responded_at = now
        db.commit()


def record_delivery(db: Session, message_id: str, status: str, at: dt.datetime) -> None:
    contact = db.query(RetentionContact).filter_by(channel="whatsapp", message_id=message_id).first()
    if contact is None:
        return
    if status in {"delivered", "read"}:
        if contact.delivered_at is None or utc(at) < utc(contact.delivered_at):
            contact.delivered_at = at
        contact.status = "delivered"
    elif status == "failed" and contact.delivered_at is None:
        contact.status, contact.error = "failed", "WhatsApp reported delivery failure."
    db.commit()
