"""Verify subscription payment endpoint."""

import datetime as dt
import logging
from datetime import datetime
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app import metrics
from app.api.routes_auth import get_current_user_id
from app.core.config import settings
from app.db.session import get_db
from app.models import models
from app.models.payment_models import PaymentStatus, PaymentTransaction
from app.services.paystack_http import paystack_async_client

from .schemas import PaymentVerifyOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/verify/{reference}", response_model=PaymentVerifyOut)
async def verify_subscription_payment(
    reference: str,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Verify Paystack payment status.

    Called by frontend after user returns from Paystack checkout.
    If payment successful, upgrade plan immediately.
    """
    try:
        async with paystack_async_client() as client:
            response = await client.get(
                f"https://api.paystack.co/transaction/verify/{reference}",
                headers={
                    "Authorization": f"Bearer {settings.PAYSTACK_SECRET}",
                },
                timeout=10.0,
            )

            if response.status_code != 200:
                raise HTTPException(status_code=400, detail="Payment verification failed")

            data = response.json()

            if not data.get("status"):
                raise HTTPException(status_code=400, detail="Invalid payment reference")

            payment = data["data"]

            # Check if payment was successful
            if payment["status"] != "success":
                return {"status": payment["status"], "message": "Payment not completed yet"}

            # Extract plan from metadata
            metadata = payment.get("metadata", {})
            plan = metadata.get("plan")
            user_id = metadata.get("user_id")

            # Verify user ID matches (convert to int since metadata comes as string from JSON)
            try:
                user_id_int = int(user_id)
            except (ValueError, TypeError):
                raise HTTPException(status_code=400, detail="Invalid user_id in payment metadata")
            if user_id_int != current_user_id:
                raise HTTPException(status_code=403, detail="Payment reference does not belong to you")

            # Find payment transaction
            payment_transaction = (
                db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
            )

            if not payment_transaction:
                logger.warning(f"Payment transaction not found for reference: {reference}")
                # Still process the upgrade even if transaction record missing

            # Upgrade user's plan
            user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
            if not user:
                raise HTTPException(status_code=404, detail="User not found")

            old_plan = user.plan.value
            user.plan = models.SubscriptionPlan[plan]

            # Set subscription expiry to 30 days from now
            now = datetime.now(dt.timezone.utc)
            user.subscription_expires_at = now + dt.timedelta(days=30)
            # Reset usage counter for new billing cycle
            user.invoices_this_month = 0
            user.usage_reset_at = now

            # Update payment transaction
            if payment_transaction:
                payment_transaction.status = PaymentStatus.SUCCESS
                payment_transaction.paid_at = now
                payment_transaction.paystack_transaction_id = payment.get("id")
                payment_transaction.payment_method = payment.get("channel")

                # Extract card/bank details if available
                authorization = payment.get("authorization", {})
                if authorization:
                    payment_transaction.card_last4 = authorization.get("last4")
                    payment_transaction.card_brand = authorization.get("brand")
                    payment_transaction.bank_name = authorization.get("bank")

                # Set billing period (30 days from now)
                payment_transaction.billing_start_date = now
                payment_transaction.billing_end_date = now + dt.timedelta(days=30)
                payment_transaction.payment_metadata = payment.get("metadata")

            db.commit()

            # Upgrade referral status to PAID only for Pro/Business (not Starter)
            # Starter has no monthly subscription - only Pro (₦5,000) and Business (₦10,000) count
            if plan in ("PRO", "BUSINESS"):
                try:
                    from app.services.referral_service import ReferralService

                    referral_service = ReferralService(db)
                    # First Pro purchase → one-time ₦500 commission
                    upgraded = referral_service.upgrade_referral_to_paid(current_user_id)
                    if not upgraded:
                        # Already paid → repeat purchase → recurring/perpetual commission
                        amount_naira = payment["amount"] // 100
                        referral_service.process_recurring_commission(current_user_id, purchase_amount=amount_naira)
                except Exception as e:
                    logger.warning(f"Failed to process referral commission: {e}")

            # Record metrics
            metrics.subscription_payment_success(plan.lower())
            metrics.subscription_upgrade(old_plan, plan.lower())

            logger.info(f"Upgraded user {current_user_id} from {old_plan} to {plan}")

            return {
                "status": "success",
                "message": f"Successfully upgraded to {plan} plan!",
                "old_plan": old_plan,
                "new_plan": plan,
                "amount_paid": payment["amount"] / 100,  # Convert kobo to naira
            }

    except httpx.RequestError as e:
        logger.error(f"Paystack verification error: {str(e)}")
        raise HTTPException(status_code=500, detail="Payment verification unavailable. Please contact support.")
