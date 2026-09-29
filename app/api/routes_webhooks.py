import hashlib
import hmac
import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.rate_limit import RATE_LIMITS, limiter
from app.core.config import settings
from app.db.session import get_db
from app.models import models
from app.queue import whatsapp_queue
from app.utils.pii import mask_email

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/whatsapp")
@limiter.limit(RATE_LIMITS["webhook_whatsapp_verify"])
def verify_whatsapp_webhook(
    request: Request,
    hub_mode: str = Query(None, alias="hub.mode"),
    hub_verify_token: str = Query(None, alias="hub.verify_token"),
    hub_challenge: str = Query(None, alias="hub.challenge"),
):
    """Webhook verification endpoint for WhatsApp Business API"""
    verify_token = settings.WHATSAPP_VERIFY_TOKEN
    if not verify_token:
        raise HTTPException(status_code=503, detail="Webhook verification not configured")

    if hub_mode == "subscribe" and hub_verify_token == verify_token:
        return PlainTextResponse(hub_challenge)

    raise HTTPException(status_code=403, detail="Verification failed")


@router.post("/whatsapp")
@limiter.limit(RATE_LIMITS["webhook_whatsapp_inbound"])
async def whatsapp_webhook(request: Request):
    """Handle incoming WhatsApp messages.

    Messages are enqueued for async processing via Celery worker.
    Verifies X-Hub-Signature-256 header from Meta to prevent spoofing.
    """
    raw_body = await request.body()

    # Verify Meta's webhook signature (X-Hub-Signature-256)
    app_secret = getattr(settings, "WHATSAPP_APP_SECRET", None)
    if app_secret:
        signature_header = request.headers.get("x-hub-signature-256", "")
        if not signature_header.startswith("sha256="):
            logger.warning("WhatsApp webhook missing or malformed signature")
            raise HTTPException(status_code=401, detail="Missing signature")

        expected_sig = hmac.new(
            app_secret.encode(),
            raw_body,
            hashlib.sha256,
        ).hexdigest()
        received_sig = signature_header.removeprefix("sha256=")

        if not hmac.compare_digest(expected_sig, received_sig):
            logger.warning("WhatsApp webhook signature verification failed")
            raise HTTPException(status_code=401, detail="Invalid signature")
    else:
        # In production, WHATSAPP_APP_SECRET is required (enforced by config validator).
        # In dev/test, allow without signature but log a warning.
        if settings.ENV.lower() == "prod":
            logger.error("WHATSAPP_APP_SECRET missing in production — rejecting webhook")
            raise HTTPException(status_code=500, detail="Webhook signature verification not configured")
        logger.warning(
            "WHATSAPP_APP_SECRET not configured — webhook signature verification SKIPPED. "
            "Set this in production to prevent spoofed messages."
        )

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    # enqueue for async processing via Celery worker
    whatsapp_queue.enqueue_message(payload)
    return {"ok": True, "queued": True}


@router.post("/ses")
@limiter.limit(RATE_LIMITS["webhook_ses"])
async def ses_webhook(request: Request):
    """Handle Amazon SNS notifications for SES bounces and complaints.

    SNS posts bounce/complaint events here. We verify the SNS signature,
    auto-confirm the topic subscription, and record hard-bounced / complained
    addresses in the suppression list so we stop emailing them.
    """
    from starlette.concurrency import run_in_threadpool

    from app.services.email_suppression import process_sns_payload

    raw_body = await request.body()
    msg_type = request.headers.get("x-amz-sns-message-type", "")

    try:
        result = await run_in_threadpool(process_sns_payload, raw_body, msg_type)
    except ValueError as e:
        logger.warning("SES SNS webhook rejected: %s", e)
        raise HTTPException(status_code=403, detail="Invalid SNS message")

    return result


def _record_webhook(db: Session, provider: str, external_id: str, signature: str | None) -> bool:
    existing = (
        db.query(models.WebhookEvent)
        .filter(models.WebhookEvent.provider == provider, models.WebhookEvent.external_id == external_id)
        .one_or_none()
    )
    if existing:
        return True
    event = models.WebhookEvent(provider=provider, external_id=external_id, signature=signature)
    db.add(event)
    try:
        # Flush now so the (provider, external_id) UNIQUE constraint decides the
        # race atomically: two concurrent identical events can't both proceed.
        db.flush()
    except IntegrityError:
        db.rollback()  # another worker recorded it first → treat as duplicate
        return True
    return False


def _handle_paystack_subscription(payload: dict, db: Session, signature: str | None) -> dict:
    """Handle Paystack subscription and charge events for recurring billing."""
    event_type = (payload.get("event") or "").lower()
    data = payload.get("data") or {}

    # Get reference - could be transaction reference or subscription code
    reference = data.get("reference") or data.get("subscription_code") or data.get("id")
    subscription_code = data.get("subscription_code")

    # For subscription.create events, get subscription code from data
    if event_type == "subscription.create":
        subscription_code = data.get("subscription_code")
        reference = subscription_code

    duplicate = False
    if reference:
        duplicate = _record_webhook(db, "paystack:subscription", f"{event_type}:{reference}", signature)
        if duplicate:
            logger.info("Paystack subscription webhook duplicate for %s:%s", event_type, reference)
            return {"status": "duplicate", "reference": reference}

    # Handle different subscription events
    if event_type == "subscription.create":
        # New subscription created - store subscription code
        return _handle_subscription_created(data, db)
    elif event_type == "subscription.disable":
        # Subscription cancelled
        return _handle_subscription_disabled(data, db)
    elif event_type == "subscription.not_renew":
        # Subscription won't renew (user requested cancellation)
        return _handle_subscription_not_renew(data, db)
    elif event_type == "invoice.payment_failed":
        # Recurring payment failed
        return _handle_invoice_payment_failed(data, db)
    elif event_type == "charge.success":
        # Successful payment (initial or recurring)
        return _handle_charge_success(data, db)
    else:
        db.commit()
        return {"status": "ignored", "event": event_type}


def _handle_subscription_created(data: dict, db: Session) -> dict:
    """Handle subscription.create event - user successfully subscribed."""
    subscription_code = data.get("subscription_code")
    customer = data.get("customer") or {}
    customer_email = customer.get("email")
    plan = data.get("plan") or {}
    plan_name = plan.get("name", "").upper()

    # Map plan name to our plan enum
    if "PRO" in plan_name:
        target_plan = "PRO"
    elif "BUSINESS" in plan_name:
        target_plan = "BUSINESS"
    else:
        logger.warning("Unknown plan in subscription.create: %s", plan_name)
        db.commit()
        return {"status": "error", "message": f"Unknown plan: {plan_name}"}

    # Find user by email
    user = (
        db.query(models.User)
        .filter((models.User.email == customer_email) | (models.User.email == customer_email.lower()))
        .first()
    )

    if not user:
        logger.error("Subscription created but user not found: %s", mask_email(customer_email))
        db.commit()
        return {"status": "error", "message": "User not found"}

    # Store subscription code on user (we'll add this field)
    if hasattr(user, "paystack_subscription_code"):
        user.paystack_subscription_code = subscription_code
    if hasattr(user, "paystack_customer_code"):
        user.paystack_customer_code = customer.get("customer_code")

    db.commit()

    logger.info(
        "✅ Subscription created: user %s, plan %s, subscription_code %s", user.id, target_plan, subscription_code
    )

    return {
        "status": "success",
        "event": "subscription.create",
        "user_id": user.id,
        "subscription_code": subscription_code,
        "plan": target_plan,
    }


def _handle_charge_success(data: dict, db: Session) -> dict:
    """Handle charge.success event - payment completed (initial or recurring)."""
    reference = data.get("reference")
    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    plan = metadata.get("plan")

    # Check if this is a subscription payment
    subscription_code = data.get("subscription_code")
    is_subscription = subscription_code is not None or metadata.get("subscription_type") == "recurring"

    # If no user_id in metadata, try to find by email
    if not user_id:
        customer = data.get("customer") or {}
        customer_email = customer.get("email")
        if customer_email:
            user = (
                db.query(models.User)
                .filter((models.User.email == customer_email) | (models.User.email == customer_email.lower()))
                .first()
            )
            if user:
                user_id = user.id

    if not user_id:
        logger.error("Paystack charge.success webhook missing user_id: %s", metadata)
        db.commit()
        return {"status": "error", "message": "Missing user_id"}

    user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if not user:
        logger.error("Paystack charge.success webhook user %s not found", user_id)
        db.commit()
        return {"status": "error", "message": "User not found"}

    # ── Recurring Pro Features subscription (₦1,500/mo, features only, 0 invoices) ──
    # Each successful charge (initial or auto-renewal) extends Pro features by 30
    # days. We do NOT add invoices here (unlike the generic plan-upgrade path).
    from app.api.routes_subscription.constants import PAYSTACK_PLAN_CODES

    plan_obj = data.get("plan") or {}
    plan_code_in = plan_obj.get("plan_code")
    plan_name_in = (plan_obj.get("name") or "").upper()
    pro_features_code = PAYSTACK_PLAN_CODES.get("PRO_FEATURES")
    is_pro_features = (
        (plan or "").upper() == "PRO_FEATURES"
        or (pro_features_code is not None and plan_code_in == pro_features_code)
        or ("FEATURES" in plan_name_in)
    )
    if is_pro_features:
        from app.utils.feature_gate import PRO_FEATURES_DAYS, grant_pro_features

        grant_pro_features(user, PRO_FEATURES_DAYS)
        if subscription_code and hasattr(user, "paystack_subscription_code"):
            user.paystack_subscription_code = subscription_code
        from app.models.payment_models import PaymentStatus, PaymentTransaction

        txn = db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
        if txn:
            txn.status = PaymentStatus.SUCCESS
            txn.plan_after = user.plan.value
        db.commit()
        logger.info(
            "✅ Pro Features recurring charge: user %s +%d days Pro (ref: %s, sub: %s)",
            user_id,
            PRO_FEATURES_DAYS,
            reference,
            subscription_code,
        )
        return {
            "status": "success",
            "event": "charge.success",
            "plan": "PRO_FEATURES",
            "pro_days": PRO_FEATURES_DAYS,
            "subscription_code": subscription_code,
            "reference": reference,
            "is_recurring": True,
        }

    # Determine plan from metadata or existing subscription
    if not plan:
        # Try to get plan from subscription if available
        if is_subscription and user.plan.value.upper() in ["PRO", "BUSINESS"]:
            plan = user.plan.value.upper()
        else:
            plan = "PRO"  # Default to PRO for subscription charges

    try:
        new_plan = models.SubscriptionPlan[plan.upper()]
    except KeyError:
        logger.error("Paystack charge.success invalid plan '%s'", plan)
        db.commit()
        return {"status": "error", "message": "Invalid plan"}

    old_plan = user.plan.value
    old_balance = getattr(user, "invoice_balance", 5)

    # Update user plan
    user.plan = new_plan

    # Store subscription code if available
    if subscription_code and hasattr(user, "paystack_subscription_code"):
        user.paystack_subscription_code = subscription_code

    # Set subscription dates
    from datetime import datetime, timedelta
    from datetime import timezone as tz

    now = datetime.now(tz.utc)
    user.subscription_started_at = now
    user.subscription_expires_at = now + timedelta(days=32)  # ~1 month buffer

    # Add invoices included with plan
    invoices_added = new_plan.invoices_included
    if invoices_added > 0 and hasattr(user, "invoice_balance"):
        user.invoice_balance += invoices_added
        logger.info(
            "Adding %d invoices to user %s balance (now %d)",
            invoices_added,
            user_id,
            getattr(user, "invoice_balance", 0),
        )

    # Update payment transaction if exists
    from app.models.payment_models import PaymentStatus, PaymentTransaction

    transaction = db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
    if transaction:
        transaction.status = PaymentStatus.SUCCESS

    db.commit()

    logger.info(
        "✅ Paystack charge.success: user %s %s -> %s, +%d invoices (ref: %s, subscription: %s)",
        user_id,
        old_plan,
        new_plan.value,
        invoices_added,
        reference,
        "recurring" if is_subscription else "one-time",
    )

    return {
        "status": "success",
        "event": "charge.success",
        "old_plan": old_plan,
        "new_plan": new_plan.value,
        "invoices_added": invoices_added,
        "invoice_balance": getattr(user, "invoice_balance", old_balance),
        "reference": reference,
        "is_recurring": is_subscription,
    }


def _handle_subscription_disabled(data: dict, db: Session) -> dict:
    """Handle subscription.disable event - subscription cancelled."""
    customer = data.get("customer") or {}
    customer_email = customer.get("email")

    user = (
        db.query(models.User)
        .filter((models.User.email == customer_email) | (models.User.email == customer_email.lower()))
        .first()
    )

    if user:
        # Clear subscription code but keep plan until expiry
        if hasattr(user, "paystack_subscription_code"):
            user.paystack_subscription_code = None

        db.commit()
        logger.info("Subscription disabled for user %s (keeps plan until expiry)", user.id)
        return {"status": "success", "event": "subscription.disable", "user_id": user.id}

    db.commit()
    return {"status": "ignored", "event": "subscription.disable", "reason": "user not found"}


def _handle_subscription_not_renew(data: dict, db: Session) -> dict:
    """Handle subscription.not_renew event - subscription will not auto-renew."""
    customer = data.get("customer") or {}
    customer_email = customer.get("email")

    user = (
        db.query(models.User)
        .filter((models.User.email == customer_email) | (models.User.email == customer_email.lower()))
        .first()
    )

    if user:
        logger.info("Subscription will not renew for user %s", user.id)
        db.commit()
        return {"status": "success", "event": "subscription.not_renew", "user_id": user.id}

    db.commit()
    return {"status": "ignored", "event": "subscription.not_renew", "reason": "user not found"}


def _handle_invoice_payment_failed(data: dict, db: Session) -> dict:
    """Handle invoice.payment_failed event - recurring charge failed."""
    subscription = data.get("subscription") or {}
    subscription_code = subscription.get("subscription_code")
    customer = data.get("customer") or {}
    customer_email = customer.get("email")

    user = (
        db.query(models.User)
        .filter((models.User.email == customer_email) | (models.User.email == customer_email.lower()))
        .first()
    )

    if user:
        logger.warning("⚠️ Payment failed for user %s subscription %s - Paystack will retry", user.id, subscription_code)
        # Paystack will retry, so we don't immediately downgrade
        # They handle dunning (retry attempts) automatically
        db.commit()
        return {"status": "success", "event": "invoice.payment_failed", "user_id": user.id}

    db.commit()
    return {"status": "ignored", "event": "invoice.payment_failed", "reason": "user not found"}


def _handle_paystack_invoice_pack(payload: dict, db: Session, signature: str | None) -> dict:
    """Handle invoice pack purchase payment confirmation."""
    event_type = (payload.get("event") or "").lower()
    data = payload.get("data") or {}
    reference = data.get("reference")

    if not reference or not reference.startswith("INVPACK-"):
        return {"status": "ignored", "reason": "not invoice pack"}

    duplicate = _record_webhook(db, "paystack:invoice_pack", reference, signature)
    if duplicate:
        logger.info("Paystack invoice pack webhook duplicate for %s", reference)
        return {"status": "duplicate", "reference": reference}

    if event_type != "charge.success":
        db.commit()
        return {"status": "ignored", "event": event_type}

    metadata = data.get("metadata") or {}
    user_id = metadata.get("user_id")
    # New model: top-ups credit the prepaid wallet (kobo). Legacy in-flight
    # purchases carried invoices_to_add (bought at ₦25) — convert those too.
    wallet_credit_kobo = int(metadata.get("wallet_credit_kobo") or int(metadata.get("invoices_to_add", 0) or 0) * 2500)
    invoices_to_add = 0  # legacy count field, no longer credited
    pro_days = int(metadata.get("pro_days", 0) or 0)

    if not user_id:
        logger.error("Paystack invoice pack webhook missing user_id: %s", metadata)
        db.commit()
        return {"status": "error", "message": "Missing user_id"}

    user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if not user:
        logger.error("Paystack invoice pack webhook user %s not found", user_id)
        db.commit()
        return {"status": "error", "message": "User not found"}

    # Wallet top-ups don't change plan; in-flight Pro packs still grant their
    # prepaid Pro days (Pro is no longer sold, but honour pending purchases).
    # Credit the prepaid wallet with the purchased top-up.
    old_balance = getattr(user, "invoice_balance", 0)
    if wallet_credit_kobo > 0:
        user.wallet_balance_kobo = int(getattr(user, "wallet_balance_kobo", 0) or 0) + wallet_credit_kobo

    if pro_days > 0:
        from app.utils.feature_gate import grant_pro_features

        grant_pro_features(user, pro_days)

    # Update payment transaction if exists
    from app.models.payment_models import PaymentStatus, PaymentTransaction

    transaction = db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
    if transaction:
        transaction.status = PaymentStatus.SUCCESS
        if pro_days > 0:
            transaction.plan_after = user.plan.value

    db.commit()

    new_balance = getattr(user, "invoice_balance", old_balance + invoices_to_add)
    pro_until = (
        user.subscription_expires_at.isoformat()
        if pro_days > 0 and getattr(user, "subscription_expires_at", None)
        else None
    )

    # Referral settlement: pay the referrer a share of this wallet top-up.
    try:
        from app.services.referral_service import ReferralService

        ref_svc = ReferralService(db)
        topup_naira = wallet_credit_kobo // 100
        if topup_naira > 0:
            ref_svc.process_topup_commission(user_id, topup_naira=topup_naira)
    except Exception as e:
        logger.warning("Failed to process referral commission for top-up: %s", e)

    logger.info(
        "✅ Invoice pack purchased: user %s added %d invoices (balance: %d → %d)" " pro_days=%d pro_until=%s ref: %s",
        user_id,
        invoices_to_add,
        old_balance,
        new_balance,
        pro_days,
        pro_until,
        reference,
    )

    result = {
        "status": "success",
        "invoices_added": invoices_to_add,
        "wallet_credited_naira": wallet_credit_kobo / 100,
        "new_balance": new_balance,
        "pro_days": pro_days,
        "pro_features_until": pro_until,
        "reference": reference,
    }

    return result


def _finalize_invoice_payment(
    db: Session,
    *,
    reference: str,
    invoice_id,
    transaction,
    provider_label: str,
    card_fingerprint: str | None = None,
) -> dict:
    """Mark an invoice paid + activate its escrow hold. Shared by the Paystack and
    Flutterwave collection webhooks (provider-agnostic)."""
    from app.models.payment_models import PaymentStatus
    from app.services.invoice_service import build_invoice_service

    service = build_invoice_service(db)
    try:
        invoice, issuer = service.get_public_invoice(invoice_id)
    except ValueError:
        logger.error("%s webhook invoice %s not found (ref=%s)", provider_label, invoice_id, reference)
        db.commit()
        return {"status": "error", "message": "Invoice not found"}

    if invoice.status != "paid":
        try:
            service.update_status(issuer.id, invoice_id, "paid", via_online=True)
        except Exception:
            logger.exception("%s: failed to mark invoice %s paid", provider_label, invoice_id)

    # Card-fraud gate: a blocked or over-velocity funding card holds the order for
    # review (never auto-releases) instead of paying the seller.
    review_reason = None
    try:
        from app.services.card_risk import card_hold_reason

        review_reason = card_hold_reason(db, card_fingerprint)
    except Exception:  # noqa: BLE001 — risk scoring must never block a payment
        logger.exception("Card risk check failed (ref=%s)", reference)

    # Activate the buyer-protection hold (pending -> held) for storefront orders.
    # Idempotent + best-effort; never break payment confirmation.
    try:
        from app.services.escrow_service import activate_escrow_on_payment

        activate_escrow_on_payment(
            db,
            invoice,
            charge_reference=reference,
            card_fingerprint=card_fingerprint,
            review_reason=review_reason,
        )
    except Exception:
        logger.exception("%s: failed to activate escrow for invoice %s", provider_label, invoice_id)

    if transaction:
        transaction.status = PaymentStatus.SUCCESS

    db.commit()
    logger.info("✅ Invoice %s auto-confirmed paid via %s (ref=%s)", invoice_id, provider_label, reference)
    return {"status": "success", "invoice_id": invoice_id, "reference": reference}


def _handle_paystack_invoice_payment(payload: dict, db: Session, signature: str | None) -> dict:
    """Auto-confirm an invoice paid online via the issuer's Paystack subaccount."""
    from app.models.payment_models import PaymentTransaction

    event_type = (payload.get("event") or "").lower()
    data = payload.get("data") or {}
    reference = data.get("reference")

    if not reference or not reference.startswith("INVPAY-"):
        return {"status": "ignored", "reason": "not invoice payment"}

    # Only a successful charge is actionable. Don't burn the dedup key on other
    # events (a failed attempt can be retried on the same reference).
    if event_type != "charge.success":
        return {"status": "ignored", "event": event_type}

    transaction = db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
    metadata = data.get("metadata") or {}
    invoice_id = metadata.get("invoice_id") or (
        (transaction.payment_metadata or {}).get("invoice_id") if transaction else None
    )
    if not invoice_id:
        logger.error("INVPAY webhook missing invoice_id (ref=%s): %s", reference, metadata)
        return {"status": "error", "message": "Missing invoice_id"}

    # Re-verify with Paystack and confirm the amount matches what we expected —
    # anti-tamper: even a webhook with a leaked/forged signature can't credit an
    # invoice for LESS than was actually paid. Done BEFORE recording dedup so a
    # transient verify failure is safely retried by Paystack.
    from app.services.collections import get_collection_provider_named

    try:
        status = get_collection_provider_named("paystack").verify_charge(reference)
    except Exception:  # noqa: BLE001 — transient → let Paystack retry
        logger.exception("Paystack verify failed (ref=%s)", reference)
        return {"status": "error", "message": "verify failed"}
    if status.status != "successful":
        return {"status": "ignored", "reason": "charge not successful on verify"}
    if transaction is not None and status.amount_kobo is not None and int(status.amount_kobo) < int(transaction.amount):
        logger.error(
            "Paystack webhook amount mismatch ref=%s verify=%s expected=%s",
            reference,
            status.amount_kobo,
            transaction.amount,
        )
        return {"status": "error", "message": "amount mismatch"}

    # Confirmed genuine — record dedup (only now) then finalize.
    if _record_webhook(db, "paystack:invoice_payment", reference, signature):
        logger.info("Paystack invoice payment webhook duplicate for %s", reference)
        return {"status": "duplicate", "reference": reference}

    from app.services.card_risk import extract_fingerprint

    return _finalize_invoice_payment(
        db,
        reference=reference,
        invoice_id=invoice_id,
        transaction=transaction,
        provider_label="Paystack",
        card_fingerprint=extract_fingerprint("paystack", status.raw),
    )


def _handle_flutterwave_invoice_payment(payload: dict, db: Session, signature: str | None) -> dict:
    """Auto-confirm a storefront/escrow invoice collected via Flutterwave."""
    from app.models.payment_models import PaymentTransaction
    from app.services.collections import get_collection_provider_named

    event_type = (payload.get("event") or "").lower()
    data = payload.get("data") or {}
    reference = data.get("tx_ref")  # our INVPAY- reference

    if not reference or not reference.startswith("INVPAY-"):
        return {"status": "ignored", "reason": "not invoice payment"}

    # Only a SUCCESSFUL charge is actionable. Do NOT record the dedup key (or write
    # anything) for failed/other events — a customer can fail then retry & succeed
    # on the SAME tx_ref, and we must still process the later successful event.
    if event_type != "charge.completed" or (data.get("status") or "").lower() != "successful":
        return {"status": "ignored", "event": event_type, "charge_status": data.get("status")}

    transaction = db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
    meta = data.get("meta") or {}
    invoice_id = meta.get("invoice_id") or (
        (transaction.payment_metadata or {}).get("invoice_id") if transaction else None
    )
    if not invoice_id:
        logger.error("FLW webhook missing invoice_id (ref=%s): %s", reference, meta)
        return {"status": "error", "message": "Missing invoice_id"}

    # Re-verify with Flutterwave before giving value, and confirm the amount matches
    # what we expected (anti-tamper — FW explicitly recommends this). Do this BEFORE
    # recording the dedup key so a transient verify failure can be retried by FW.
    try:
        status = get_collection_provider_named("flutterwave").verify_charge(reference)
    except Exception:
        logger.exception("FLW webhook verify failed (ref=%s)", reference)
        return {"status": "error", "message": "verify failed"}
    if status.status != "successful":
        return {"status": "ignored", "reason": f"verify={status.status}"}
    if transaction is not None and status.amount_kobo is not None and int(status.amount_kobo) < int(transaction.amount):
        logger.error(
            "FLW webhook amount mismatch (ref=%s): verified %s < expected %s",
            reference,
            status.amount_kobo,
            transaction.amount,
        )
        return {"status": "error", "message": "amount mismatch"}
    if transaction is not None and status.amount_kobo is not None and int(status.amount_kobo) > int(transaction.amount):
        # Overpayment: accept it (the buyer paid) but audit it — the excess sits
        # in the hold and should be reconciled/refunded manually.
        logger.warning(
            "FLW webhook OVERPAYMENT (ref=%s): verified %s > expected %s — accepting, needs review",
            reference,
            status.amount_kobo,
            transaction.amount,
        )

    # Confirmed successful — record the dedup key and finalize atomically. A repeat
    # of the same successful event is short-circuited here (and finalize is itself
    # idempotent: it won't re-pay an already-paid invoice or re-activate a hold).
    duplicate = _record_webhook(db, "flutterwave:invoice_payment", reference, signature)
    if duplicate:
        logger.info("Flutterwave invoice payment webhook duplicate for %s", reference)
        return {"status": "duplicate", "reference": reference}

    from app.services.card_risk import extract_fingerprint

    return _finalize_invoice_payment(
        db,
        reference=reference,
        invoice_id=invoice_id,
        transaction=transaction,
        provider_label="Flutterwave",
        card_fingerprint=extract_fingerprint("flutterwave", status.raw),
    )


@router.post("/paystack")
@limiter.limit(RATE_LIMITS["webhook_paystack"])
async def paystack_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    raw_body = await request.body()
    signature = request.headers.get("x-paystack-signature")
    if not signature:
        logger.warning("Paystack webhook received without signature")
        raise HTTPException(status_code=400, detail="Missing signature")

    expected_signature = hmac.new(
        settings.PAYSTACK_SECRET.encode(),
        raw_body,
        hashlib.sha512,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected_signature):
        logger.warning("Paystack webhook signature verification failed")
        raise HTTPException(status_code=400, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.error("Invalid Paystack webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    event_type = payload.get("event")
    if not event_type:
        logger.info("Paystack webhook without event payload received; ignoring")
        return {"status": "ignored", "reason": "missing event"}

    # Check reference to determine payment type
    data = payload.get("data") or {}
    reference = data.get("reference") or ""

    # Route to appropriate handler based on reference or event type
    if reference.startswith("INVPAY-"):
        return _handle_paystack_invoice_payment(payload, db, signature)
    if reference.startswith("INVPACK-"):
        return _handle_paystack_invoice_pack(payload, db, signature)
    elif event_type in [
        "subscription.create",
        "subscription.disable",
        "subscription.not_renew",
        "invoice.payment_failed",
        "charge.success",
    ]:
        return _handle_paystack_subscription(payload, db, signature)
    else:
        logger.info("Paystack webhook event %s not handled", event_type)
        return {"status": "ignored", "event": event_type}


@router.post("/flutterwave")
@limiter.limit(RATE_LIMITS["webhook_paystack"])
async def flutterwave_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Flutterwave collection webhook — verified via the `verif-hash` header
    (the secret hash set in the FW dashboard, read from FLUTTERWAVE_WEBHOOK_HASH).
    Flutterwave's webhook source IPs are dynamic, so signature is the trust
    anchor (not IP allow-listing)."""
    raw_body = await request.body()
    expected = settings.FLUTTERWAVE_WEBHOOK_HASH
    if not expected:
        logger.error("Flutterwave webhook hit but FLUTTERWAVE_WEBHOOK_HASH is unset")
        raise HTTPException(status_code=503, detail="Webhook not configured")

    signature = request.headers.get("verif-hash")
    if not signature or not hmac.compare_digest(signature, expected):
        logger.warning("Flutterwave webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.error("Invalid Flutterwave webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    event_type = payload.get("event")
    data = payload.get("data") or {}
    reference = data.get("tx_ref") or ""

    if reference.startswith("INVPAY-"):
        # NOTE: do NOT pass the verif-hash onward — it IS the static webhook secret;
        # storing it (WebhookEvent.signature) would persist the secret at rest.
        return _handle_flutterwave_invoice_payment(payload, db, None)
    logger.info("Flutterwave webhook event %s (ref=%s) not handled", event_type, reference)
    return {"status": "ignored", "event": event_type}


@router.post("/shipbubble")
@limiter.limit(RATE_LIMITS["webhook_paystack"])
async def shipbubble_webhook(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
):
    """Shipbubble courier webhook — shipment status/tracking updates.

    Verified via the ``x-ship-signature`` header (HMAC-SHA512 of the raw body,
    keyed by our Shipbubble secret). Register this URL in the Shipbubble
    dashboard: ``https://api.suoops.com/webhooks/shipbubble``. Must return 200
    within 15s or Shipbubble retries. Fail-soft: a body we can't correlate is
    still acknowledged so the endpoint isn't marked failed.
    """
    raw_body = await request.body()
    secret = settings.SHIPBUBBLE_WEBHOOK_SECRET or settings.SHIPBUBBLE_API_KEY
    if not secret:
        logger.error("Shipbubble webhook hit but no secret/API key configured")
        raise HTTPException(status_code=503, detail="Webhook not configured")

    signature = request.headers.get("x-ship-signature")
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha512).hexdigest()
    if not signature or not hmac.compare_digest(signature, expected):
        logger.warning("Shipbubble webhook signature verification failed")
        raise HTTPException(status_code=401, detail="Invalid signature")

    try:
        payload = json.loads(raw_body.decode("utf-8") or "{}")
    except json.JSONDecodeError as exc:
        logger.error("Invalid Shipbubble webhook payload: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid payload") from exc

    event = (payload.get("event") or "").lower()
    order_id = payload.get("order_id")
    order_status = payload.get("status")
    courier = payload.get("courier") or {}

    # Dedup / audit — (order_id, status, event) is a stable idempotency key.
    if order_id:
        if _record_webhook(db, "shipbubble", f"{order_id}:{order_status}:{event}", signature):
            logger.info("Shipbubble webhook duplicate for %s (%s)", order_id, order_status)
            return {"status": "duplicate", "order_id": order_id}

    # Best-effort: reflect the courier update onto the matching storefront order.
    # (Correlation by shipbubble_order_id is wired with the booking step; until
    # then this safely no-ops and we just acknowledge + log.)
    try:
        _apply_shipbubble_update(db, order_id, order_status, courier, payload)
    except Exception:  # noqa: BLE001 — never fail the webhook on a bookkeeping error
        logger.exception("Shipbubble webhook apply failed (order_id=%s)", order_id)

    db.commit()
    logger.info(
        "Shipbubble webhook: event=%s order=%s status=%s tracking=%s",
        event,
        order_id,
        order_status,
        courier.get("tracking_code"),
    )
    return {"status": "ok", "order_id": order_id}


def _apply_shipbubble_update(
    db: Session,
    order_id: str | None,
    order_status: str | None,
    courier: dict,
    payload: dict,
) -> None:
    """Reflect a Shipbubble shipment update onto the storefront order it belongs
    to, matched by ``shipbubble_order_id``. No-op until booking stores that id."""
    if not order_id or not hasattr(models.StorefrontOrderEscrow, "shipbubble_order_id"):
        return
    escrow = (
        db.query(models.StorefrontOrderEscrow)
        .filter(models.StorefrontOrderEscrow.shipbubble_order_id == order_id)
        .first()
    )
    if not escrow:
        return
    tracking_code = courier.get("tracking_code")
    if tracking_code and not escrow.dispatch_tracking:
        escrow.dispatch_tracking = str(tracking_code)[:120]
    if courier.get("name") and not escrow.dispatch_carrier:
        escrow.dispatch_carrier = str(courier["name"])[:80]
    import datetime as _dt

    from app.services.shipping import shipbubble as _sb

    now = _dt.datetime.now(_dt.timezone.utc)

    # Normalized lifecycle: booked → pickup_enroute → picked_up → in_transit →
    # out_for_delivery → delivered (+ cancelled/returned). Store the latest so the
    # buyer and seller can see live progress, and nudge the buyer on key moves.
    code = _sb.normalize_status(order_status)
    if code and code != escrow.delivery_status:
        escrow.delivery_status = code
        escrow.delivery_status_at = now
        notice = {
            "picked_up": "🚚 Your order has been picked up by the courier — it's on the way.",
            "out_for_delivery": "🛵 Your order is out for delivery — expect it soon.",
        }.get(code)
        if notice:
            try:
                from app.api.routes_storefront import _store_system_message

                _store_system_message(db, escrow, notice)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to post %s notice for escrow %s", code, escrow.id)

    # First movement (picked up / in transit) → record the dispatch timestamp.
    if code in {"picked_up", "in_transit"} and not escrow.seller_dispatched_at:
        escrow.seller_dispatched_at = now
    # Delivered → start the post-delivery inspection window: the payout can now
    # auto-release only after the buyer has had time to inspect/dispute.
    if code == "delivered" and escrow.courier_delivered_at is None:
        from app.core.config import settings

        escrow.courier_delivered_at = now
        if escrow.status == "held":
            escrow.release_due_at = now + _dt.timedelta(hours=settings.ESCROW_POST_DELIVERY_INSPECTION_HOURS)
        # Let the buyer know it's delivered so they can confirm or report a problem.
        try:
            from app.api.routes_storefront import _store_system_message

            _store_system_message(
                db,
                escrow,
                "✅ Your order was delivered. If anything's wrong, tap 'Report a "
                "problem' within the next day — otherwise it completes automatically.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to post delivery notice for escrow %s", escrow.id)
