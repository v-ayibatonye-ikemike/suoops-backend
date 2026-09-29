"""Cancel subscription endpoint."""

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.core.config import settings
from app.db.session import get_db
from app.models import models
from app.services.paystack_http import paystack_async_client

from .schemas import CancelSubscriptionOut, SubscriptionStatusOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/cancel", response_model=CancelSubscriptionOut)
async def cancel_subscription(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Cancel user's Paystack subscription (stop auto-renewal).

    **Important:** This stops future charges but does NOT immediately downgrade.
    User keeps their plan until subscription_expires_at date.

    **Flow:**
    1. Disable subscription on Paystack
    2. Clear subscription code from user
    3. User keeps plan until expiry, then auto-downgrades to FREE

    **Returns:**
    - success: Confirmation message
    - expires_at: When the current subscription period ends
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Check if user has a subscription to cancel
    subscription_code = getattr(user, "paystack_subscription_code", None)

    if not subscription_code:
        # Check if they're on a paid plan without subscription (legacy one-time payment)
        if user.plan.value.upper() in ["PRO", "BUSINESS"]:
            return {
                "status": "info",
                "message": (
                    "You don't have auto-renewal enabled. " "Your plan will expire at the end of your billing period."
                ),
                "plan": user.plan.value,
                "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
            }
        raise HTTPException(status_code=400, detail="No active subscription to cancel")

    # Disable subscription on Paystack
    try:
        async with paystack_async_client() as client:
            # First get subscription details to get the email_token
            sub_response = await client.get(
                f"https://api.paystack.co/subscription/{subscription_code}",
                headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET}"},
                timeout=10.0,
            )

            if sub_response.status_code != 200:
                logger.error(f"Failed to fetch subscription: {sub_response.text}")
                # Subscription might not exist on Paystack, clear locally anyway
                if hasattr(user, "paystack_subscription_code"):
                    user.paystack_subscription_code = None
                db.commit()
                return {
                    "status": "success",
                    "message": "Subscription cancelled. You keep your plan until the end of your billing period.",
                    "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
                }

            sub_data = sub_response.json().get("data", {})
            email_token = sub_data.get("email_token")

            # Disable the subscription
            disable_response = await client.post(
                "https://api.paystack.co/subscription/disable",
                headers={
                    "Authorization": f"Bearer {settings.PAYSTACK_SECRET}",
                    "Content-Type": "application/json",
                },
                json={
                    "code": subscription_code,
                    "token": email_token,
                },
                timeout=10.0,
            )

            if disable_response.status_code != 200:
                logger.error(f"Failed to disable subscription: {disable_response.text}")
                raise HTTPException(status_code=500, detail="Failed to cancel subscription. Please contact support.")

            # Clear subscription code from user
            if hasattr(user, "paystack_subscription_code"):
                user.paystack_subscription_code = None

            db.commit()

            logger.info(
                "✅ Subscription cancelled for user %s (keeps %s until %s)",
                current_user_id,
                user.plan.value,
                user.subscription_expires_at,
            )

            return {
                "status": "success",
                "message": (
                    "Subscription cancelled. You won't be charged again. " "Your {} features remain active until {}."
                ).format(
                    user.plan.value,
                    user.subscription_expires_at.strftime("%B %d, %Y")
                    if user.subscription_expires_at
                    else "end of billing period",
                ),
                "plan": user.plan.value,
                "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
            }

    except httpx.RequestError as e:
        logger.error(f"Paystack request error during cancellation: {str(e)}")
        raise HTTPException(status_code=500, detail="Payment service unavailable. Please try again later.")


@router.get("/status", response_model=SubscriptionStatusOut)
def get_subscription_status(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Get current subscription status including billing info.

    **Returns:**
    - plan: Current plan name
    - is_recurring: Whether subscription auto-renews
    - expires_at: When current period ends
    - invoice_balance: Available invoices
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    subscription_code = getattr(user, "paystack_subscription_code", None)

    return {
        "plan": user.plan.value,
        "is_recurring": subscription_code is not None,
        "subscription_started_at": user.subscription_started_at.isoformat() if user.subscription_started_at else None,
        "expires_at": user.subscription_expires_at.isoformat() if user.subscription_expires_at else None,
        "invoice_balance": getattr(user, "invoice_balance", 0),
    }
