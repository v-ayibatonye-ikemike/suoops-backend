"""User profile and settings management endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.api.rate_limit import limiter
from app.api.routes_admin_auth import get_current_admin
from app.api.routes_auth import get_current_user_id
from app.core.cache import cached
from app.core.encryption import decrypt_value
from app.db.session import get_db
from app.models import models, schemas
from app.services.account_deletion_service import (
    AccountDeletionBlockedError,
    AccountDeletionService,
)
from app.services.otp_service import OTPService

logger = logging.getLogger(__name__)
router = APIRouter()
otp_service = OTPService()


class InvoiceUsage(BaseModel):
    used_this_month: int
    limit: int | None = None
    remaining: int | None = None
    can_create_more: bool
    limit_message: str | None = None


class FeatureAccessOut(BaseModel):
    """GET /me/features — excludes internal user_id."""

    current_plan: str
    plan_price: float | None = None
    is_free_tier: bool
    features: dict[str, object]
    invoice_usage: InvoiceUsage
    upgrade_available: bool
    upgrade_url: str | None = None


class ActivationStateOut(BaseModel):
    """Derived activation facts; storefront setup is recommended, not required for 100%."""

    business_profile_ready: bool = Field(description="True when a non-empty business name is saved.")
    bank_details_ready: bool = Field(
        description="True when bank name, account number, and verified account name are all saved."
    )
    storefront_enabled: bool = Field(description="True when the public storefront is enabled.")
    storefront_profile_ready: bool = Field(
        description=(
            "True when logo, storefront description and state are saved and at least one active product "
            "has both a photo and description. This is recommended and does not affect progress_percent."
        )
    )
    product_count: int = Field(description="Total number of products, including inactive products.")
    online_payments_enabled: bool = Field(description="True when the user's Paystack subaccount is active.")
    invoice_count: int = Field(description="Total revenue invoices; expense invoices are excluded.")
    paid_invoice_count: int = Field(description="Revenue invoices whose status is paid; expenses are excluded.")
    progress_percent: int = Field(
        ge=0,
        le=100,
        description=(
            "Percentage of six core milestones completed: business profile, bank details, first product, "
            "online payments, first revenue invoice, and first paid revenue invoice. Storefront milestones "
            "are recommendations and never block 100%."
        ),
    )


def _check_is_influencer(db: Session, user_id: int) -> bool:
    """Check if user has an active influencer referral code."""
    from app.models.referral_models import ReferralCode

    code = (
        db.query(ReferralCode.is_influencer)
        .filter(
            ReferralCode.user_id == user_id,
            ReferralCode.is_influencer.is_(True),
        )
        .first()
    )
    return bool(code)


@router.get("/me/activation-state", response_model=ActivationStateOut)
def get_activation_state(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> ActivationStateOut:
    """Return current activation state derived entirely from existing records."""
    from app.models.inventory_models import Product
    from app.models.models import Invoice

    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    product_count, listable_product_count = (
        db.query(
            func.count(Product.id),
            func.sum(
                case(
                    (
                        Product.is_active.is_(True)
                        & Product.description.isnot(None)
                        & (Product.description != "")
                        & Product.image_url.isnot(None)
                        & (Product.image_url != ""),
                        1,
                    ),
                    else_=0,
                )
            ),
        )
        .filter(Product.user_id == current_user_id)
        .one()
    )
    invoice_count, paid_invoice_count = (
        db.query(
            func.count(Invoice.id),
            func.sum(case((Invoice.status == "paid", 1), else_=0)),
        )
        .filter(
            Invoice.issuer_id == current_user_id,
            Invoice.invoice_type == "revenue",
        )
        .one()
    )

    business_profile_ready = bool((user.business_name or "").strip())
    bank_details_ready = bool(user.bank_name and user.account_number and user.account_name)
    online_payments_enabled = bool(user.paystack_subaccount_active)
    product_count = int(product_count or 0)
    invoice_count = int(invoice_count or 0)
    paid_invoice_count = int(paid_invoice_count or 0)
    storefront_profile_ready = bool(
        user.logo_url
        and (user.storefront_description or "").strip()
        and user.storefront_state
        and (listable_product_count or 0) > 0
    )

    core_milestones = (
        business_profile_ready,
        bank_details_ready,
        product_count > 0,
        online_payments_enabled,
        invoice_count > 0,
        paid_invoice_count > 0,
    )
    progress_percent = sum(core_milestones) * 100 // len(core_milestones)

    return ActivationStateOut(
        business_profile_ready=business_profile_ready,
        bank_details_ready=bank_details_ready,
        storefront_enabled=bool(user.storefront_enabled),
        storefront_profile_ready=storefront_profile_ready,
        product_count=product_count,
        online_payments_enabled=online_payments_enabled,
        invoice_count=invoice_count,
        paid_invoice_count=paid_invoice_count,
        progress_percent=progress_percent,
    )


@router.get("/me", response_model=schemas.UserOut)
def get_profile(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Return current user's core profile and subscription details."""
    from app.utils.feature_gate import FeatureGate

    # FeatureGate.user triggers the subscription-expiry check (downgrades a
    # lapsed paid plan to FREE) and returns the user; raises 404 if missing.
    gate = FeatureGate(db, current_user_id)
    user = gate.user

    # If pilot encryption stored encrypted email, attempt decrypt
    email_plain = decrypt_value(user.email) if user.email else None

    # Safely get legacy invoice count (kept for backward compat; frontend uses wallet)
    invoice_balance = getattr(user, "invoice_balance", 0) or 0

    # Generate fresh presigned URLs for stored branding assets.
    def _fresh_branding_url(stored_url: str | None) -> str | None:
        if not stored_url:
            return None
        from app.storage.s3_client import s3_client

        key = s3_client.extract_key_from_url(stored_url)
        fresh_url = s3_client.get_presigned_url(key, expires_in=3600) if key else None
        return fresh_url or stored_url

    fresh_logo_url = _fresh_branding_url(user.logo_url)
    fresh_cover_url = _fresh_branding_url(user.storefront_cover_url)

    # Has the user ever created a revenue invoice? Drives the dashboard's
    # first-invoice activation prompt (invoices_this_month is deprecated/0, so
    # the prompt used to never clear — e.g. after invoicing via WhatsApp).
    from app.models.models import Invoice

    has_invoiced = (
        db.query(Invoice.id).filter(Invoice.issuer_id == user.id, Invoice.invoice_type == "revenue").first() is not None
    )

    return schemas.UserOut(
        id=user.id,
        phone=user.phone,
        phone_verified=user.phone_verified,
        email=email_plain,
        name=user.name,
        business_name=user.business_name,
        bank_name=user.bank_name,
        account_number=user.account_number,
        plan=user.effective_plan.value,  # Uses effective_plan to respect pro_override
        invoice_balance=invoice_balance,  # Legacy count (kept for backward compat)
        wallet_balance_kobo=getattr(user, "wallet_balance_kobo", 0) or 0,
        invoices_this_month=0,  # Deprecated, kept for backward compat
        logo_url=fresh_logo_url,
        storefront_cover_url=fresh_cover_url,
        subscription_expires_at=user.subscription_expires_at,
        subscription_started_at=user.usage_reset_at,  # When current billing cycle started
        is_influencer=_check_is_influencer(db, user.id),
        online_payments_enabled=bool(getattr(user, "paystack_subaccount_active", False)),
        storefront_enabled=bool(getattr(user, "storefront_enabled", False)),
        has_invoiced=has_invoiced,
    )


@router.get("/me/features", response_model=FeatureAccessOut)
async def get_feature_access(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Get current user's feature access and subscription limits.

    Returns detailed information about:
    - Current subscription plan
    - Monthly invoice usage and limits
    - Premium feature access (OCR, voice, etc)
    - Upgrade options
    """
    from app.utils.feature_gate import FeatureGate

    async def _produce():
        gate = FeatureGate(db, current_user_id)
        user = gate.user
        plan = user.effective_plan  # Uses effective_plan to respect pro_override
        can_create, limit_message = gate.can_create_invoice()
        monthly_count = gate.get_monthly_invoice_count()
        return {
            "user_id": user.id,
            "current_plan": plan.value,
            "plan_price": plan.price,
            "is_free_tier": gate.is_free_tier(),
            "features": plan.features,
            "invoice_usage": {
                "used_this_month": monthly_count,
                "limit": plan.invoice_limit,
                "remaining": (plan.invoice_limit - monthly_count) if plan.invoice_limit else None,
                "can_create_more": can_create,
                "limit_message": limit_message,
            },
            "upgrade_available": gate.is_free_tier(),
            "upgrade_url": "/subscription/initialize" if gate.is_free_tier() else None,
        }

    # Cache per-user feature access for 20s to reduce DB pressure during polling
    return await cached(f"user:{current_user_id}:features", 20, _produce)


class UpdateProfileRequest(BaseModel):
    """Request to update user profile."""

    name: str = Field(..., min_length=1, max_length=120, description="User's full name")


class UpdateProfileResponse(BaseModel):
    """Response after profile update."""

    success: bool
    message: str
    name: str


@router.patch("/me", response_model=UpdateProfileResponse)
@limiter.limit("10/minute")
def update_profile(
    request: Request,
    body: UpdateProfileRequest,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Update the current user's profile information.

    Currently supports:
    - Name updates

    Returns updated profile data.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Update name
    user.name = body.name.strip()

    try:
        db.commit()
        db.refresh(user)

        return UpdateProfileResponse(success=True, message="Profile updated successfully", name=user.name)
    except Exception as e:
        logger.error("Profile update failed for user %s: %s", current_user_id, e, exc_info=True)
        db.rollback()
        raise HTTPException(status_code=500, detail="Failed to update profile. Please try again.")


class DeleteAccountRequest(BaseModel):
    """Request to delete user account."""

    confirmation: str  # Must be "DELETE MY ACCOUNT" to confirm


class DeleteAccountResponse(BaseModel):
    """Response after account deletion."""

    success: bool
    message: str
    deleted_items: dict | None = None


@router.delete("/me", response_model=DeleteAccountResponse)
@limiter.limit("3/hour")
def delete_own_account(
    request: Request,
    request_body: DeleteAccountRequest,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Delete the current user's account and all associated data.

    This action is IRREVERSIBLE. All data including invoices, customers,
    inventory, and settings will be permanently deleted.

    Requires confirmation text "DELETE MY ACCOUNT" to proceed.
    """
    # Require explicit confirmation
    if request_body.confirmation != "DELETE MY ACCOUNT":
        raise HTTPException(
            status_code=400, detail="Invalid confirmation. Type 'DELETE MY ACCOUNT' to confirm deletion."
        )

    try:
        service = AccountDeletionService(db)
        result = service.delete_account(user_id=current_user_id, deleted_by_user_id=current_user_id)

        return DeleteAccountResponse(
            success=True,
            message="Your account has been permanently deleted.",
            deleted_items=result.get("deleted_items"),
        )
    except AccountDeletionBlockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Account deletion failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete account. Please contact support.")


@router.delete("/admin/{user_id}", response_model=DeleteAccountResponse)
def admin_delete_account(
    user_id: int,
    request: DeleteAccountRequest,
    admin_user=Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """
    Admin endpoint to delete any user account.

    Requires admin role and confirmation text "DELETE MY ACCOUNT".
    Note: get_current_admin already verifies admin privileges.
    """
    # Require explicit confirmation
    if request.confirmation != "DELETE MY ACCOUNT":
        raise HTTPException(
            status_code=400, detail="Invalid confirmation. Type 'DELETE MY ACCOUNT' to confirm deletion."
        )

    # Verify target user exists
    target_user = db.query(models.User).filter(models.User.id == user_id).one_or_none()
    if not target_user:
        raise HTTPException(status_code=404, detail="User not found")

    service = AccountDeletionService(db)

    try:
        result = service.delete_account(
            user_id=user_id,
            deleted_by_user_id=None,  # Admin deletion, not self-deletion
        )

        logger.info("Admin %s deleted user %s", admin_user.email, user_id)

        return DeleteAccountResponse(
            success=True,
            message=f"Account {user_id} has been permanently deleted.",
            deleted_items=result.get("deleted_items"),
        )
    except AccountDeletionBlockedError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.error("Admin account deletion failed: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete account. Please check logs.")


"""Logo, phone, and bank endpoints moved to dedicated modules.

Remaining responsibilities:
- Profile retrieval
- Feature access introspection
"""
