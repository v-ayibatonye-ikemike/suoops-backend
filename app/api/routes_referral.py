"""
Referral API routes for managing referral codes, tracking referrals, and claiming rewards.
"""

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.api.dependencies import CurrentUserDep, DbDep
from app.api.rate_limit import limiter
from app.services.referral_service import ReferralService

router = APIRouter(prefix="/referrals", tags=["Referrals"])


# ==================== SCHEMAS ====================


class ReferralCodeResponse(BaseModel):
    """Response with user's referral code."""

    code: str
    referral_link: str
    is_active: bool


class MessageResponse(BaseModel):
    message: str


class ReferralStatsResponse(BaseModel):
    """Referral statistics for a user."""

    referral_code: str
    referral_link: str
    total_referrals: int
    pending_referrals: int
    free_signups: int
    paid_signups: int
    rewards_earned: int
    pending_rewards: int
    pending_rewards_list: list[dict]
    progress: dict


class RecentReferralResponse(BaseModel):
    """Recent referral entry."""

    id: int
    referred_name: str
    type: str
    status: str
    created_at: str
    completed_at: str | None


class ApplyRewardRequest(BaseModel):
    """Request to apply a reward."""

    reward_id: int


class ApplyRewardResponse(BaseModel):
    """Response after applying a reward."""

    success: bool
    message: str


class ValidateCodeRequest(BaseModel):
    """Request to validate a referral code."""

    code: str = Field(..., min_length=3, max_length=50)


class ValidateCodeResponse(BaseModel):
    """Response after validating a referral code."""

    valid: bool
    referrer_name: str | None = None
    error: str | None = None


class PayoutBankDetailsResponse(BaseModel):
    """Response with payout bank details."""

    bank_name: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    is_complete: bool = False
    using_business_bank: bool = False


class PayoutBankDetailsUpdate(BaseModel):
    """Request to update payout bank details."""

    bank_name: str = Field(..., min_length=2, max_length=100)
    account_number: str = Field(..., min_length=10, max_length=10, pattern=r"^\d{10}$")
    account_name: str = Field(..., min_length=2, max_length=255)


# ==================== ENDPOINTS ====================


@router.get("/code", response_model=ReferralCodeResponse)
def get_referral_code(
    user_id: CurrentUserDep,
    db: DbDep,
):
    """
    Get or create the current user's referral code.

    Every user gets a unique referral code they can share.
    """
    service = ReferralService(db)
    referral_code = service.get_or_create_referral_code(user_id)

    return ReferralCodeResponse(
        code=referral_code.code,
        referral_link=f"https://suoops.ng/register?ref={referral_code.code}",
        is_active=referral_code.is_active,
    )


@router.get("/stats", response_model=ReferralStatsResponse)
def get_referral_stats(
    user_id: CurrentUserDep,
    db: DbDep,
):
    """
    Get referral statistics for the current user.

    Includes:
    - Total referrals (completed)
    - Pending referrals (awaiting verification)
    - Free vs paid signups
    - Rewards earned and pending
    - Progress towards next reward
    """
    service = ReferralService(db)
    stats = service.get_referral_stats(user_id)
    return ReferralStatsResponse(**stats)


@router.get("/recent", response_model=list[RecentReferralResponse])
def get_recent_referrals(
    user_id: CurrentUserDep,
    db: DbDep,
    limit: int = 10,
):
    """
    Get recent referrals for the current user.
    """
    service = ReferralService(db)
    referrals = service.get_recent_referrals(user_id, limit=min(limit, 50))
    return [RecentReferralResponse(**r) for r in referrals]


@router.post("/apply-reward", response_model=ApplyRewardResponse)
def apply_reward(
    user_id: CurrentUserDep,
    db: DbDep,
    request: ApplyRewardRequest,
):
    """
    Apply a pending reward to the user's account.

    This will:
    - Add bonus invoices to the user's balance
    - Add 1 month to their subscription (if Pro)
    """
    service = ReferralService(db)
    success, message = service.apply_reward(user_id, request.reward_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=message,
        )

    return ApplyRewardResponse(success=True, message=message)


@router.post("/validate", response_model=ValidateCodeResponse)
@limiter.limit("10/minute")
def validate_referral_code(
    request: Request,
    db: DbDep,
    payload: ValidateCodeRequest,
):
    """
    Validate a referral code (public endpoint for signup form).

    This endpoint is used to check if a referral code is valid
    before the user completes registration.

    Accepts either the random referral code OR an influencer's custom
    vanity slug (case-insensitive) — matching the same lookup used when
    the referral is actually recorded at signup.
    """
    from app.models.models import User

    code = payload.code.strip()

    # Use the shared lookup so this matches the apply-at-signup path:
    # checks both ReferralCode.code and the influencer custom_slug.
    service = ReferralService(db)
    referral_code = service.get_code_by_string(code)

    if not referral_code:
        return ValidateCodeResponse(
            valid=False,
            error="Invalid referral code",
        )

    # Get referrer's name (for display)
    referrer = db.get(User, referral_code.user_id)

    # Prefer the influencer's display name when present
    referrer_name = referral_code.influencer_name or (referrer.name if referrer else None)

    return ValidateCodeResponse(
        valid=True,
        referrer_name=referrer_name,
    )


@router.get("/payout-bank", response_model=PayoutBankDetailsResponse)
def get_payout_bank_details(
    user_id: CurrentUserDep,
    db: DbDep,
):
    """
    Get the current user's payout bank details for referral commissions.

    Behavior:
    - If a dedicated payout account is set, return that (override)
    - Otherwise, fall back to the user's business invoice bank account
        so influencers don't have to set bank details twice
    """
    from sqlalchemy import select

    from app.models.models import User

    user = db.execute(select(User).where(User.id == user_id)).scalar_one()

    has_payout_account = all(
        [
            user.payout_bank_name,
            user.payout_account_number,
            user.payout_account_name,
        ]
    )

    has_business_bank = all(
        [
            user.bank_name,
            user.account_number,
            user.account_name,
        ]
    )

    using_business_bank = not has_payout_account and has_business_bank

    bank_name = user.payout_bank_name if has_payout_account else user.bank_name
    account_number = user.payout_account_number if has_payout_account else user.account_number
    account_name = user.payout_account_name if has_payout_account else user.account_name

    return PayoutBankDetailsResponse(
        bank_name=bank_name,
        account_number=account_number,
        account_name=account_name,
        is_complete=bool(bank_name and account_number and account_name),
        using_business_bank=using_business_bank,
    )


@router.patch("/payout-bank", response_model=PayoutBankDetailsResponse)
def update_payout_bank_details(
    user_id: CurrentUserDep,
    db: DbDep,
    request: PayoutBankDetailsUpdate,
):
    """
    Update the current user's payout bank details for referral commissions.

    This is where commission payouts will be sent (weekly payout schedule).
    """
    import logging

    from sqlalchemy import select

    from app.models.models import User

    logger = logging.getLogger(__name__)

    try:
        user = db.execute(select(User).where(User.id == user_id)).scalar_one()

        had_payout = bool(user.payout_bank_name and user.payout_account_number)
        old_payout = (user.payout_bank_name, user.payout_account_number)
        user.payout_bank_name = request.bank_name
        user.payout_account_number = request.account_number
        user.payout_account_name = request.account_name

        db.commit()
        db.refresh(user)

        # A change to an existing payout account is a takeover risk — invalidate
        # the escrow recipient, freeze payouts + alert the owner.
        if had_payout and (user.payout_bank_name, user.payout_account_number) != old_payout:
            from app.services.escrow_service import on_payout_details_changed

            on_payout_details_changed(db, user)

        return PayoutBankDetailsResponse(
            bank_name=user.payout_bank_name,
            account_number=user.payout_account_number,
            account_name=user.payout_account_name,
            is_complete=True,
            using_business_bank=False,
        )
    except Exception as e:
        logger.error(f"Error updating payout bank for user {user_id}: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not save payout account: {str(e)}"
        )


@router.delete("/payout-bank", response_model=MessageResponse)
def delete_payout_bank_details(
    user_id: CurrentUserDep,
    db: DbDep,
):
    """
    Clear the current user's payout bank details.
    """
    import logging

    from sqlalchemy import select

    from app.models.models import User

    logger = logging.getLogger(__name__)

    try:
        user = db.execute(select(User).where(User.id == user_id)).scalar_one()

        user.payout_bank_name = None
        user.payout_account_number = None
        user.payout_account_name = None

        db.commit()

        return {"message": "Payout bank details cleared"}
    except Exception as e:
        logger.error(f"Error deleting payout bank for user {user_id}: {str(e)}", exc_info=True)
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Could not clear payout account: {str(e)}"
        )


# ==================== INFLUENCER EARNINGS ====================


class EarningsBreakdown(BaseModel):
    """Influencer earnings (flat-commission model: you earn a % of SuoOps' 3%
    on every referred business's activity, ongoing)."""

    total_earned: int
    online_earned: int  # commission from referred businesses' online/storefront sales
    topup_earned: int  # commission from referred businesses' wallet top-ups
    total_signups: int
    total_conversions: int  # Free → paid
    pending_payout: int
    custom_link: str | None = None
    commission_pct: int  # your ongoing % of SuoOps' fee
    recent_earnings: list[dict]


@router.get("/earnings", response_model=EarningsBreakdown)
def get_influencer_earnings(
    user_id: CurrentUserDep,
    db: DbDep,
):
    """
    Get detailed earnings breakdown for influencer dashboard.

    Only meaningful for users with is_influencer=True on their
    referral code, but any authenticated user can call it.
    """
    from sqlalchemy import func, select

    from app.models.referral_models import (
        Referral,
        ReferralCode,
        ReferralReward,
        ReferralStatus,
        ReferralType,
        RewardStatus,
    )
    from app.services.referral_share import build_referral_link

    # Get referral code
    code_obj = db.execute(select(ReferralCode).where(ReferralCode.user_id == user_id)).scalar_one_or_none()

    if not code_obj:
        return EarningsBreakdown(
            total_earned=0,
            online_earned=0,
            topup_earned=0,
            total_signups=0,
            total_conversions=0,
            pending_payout=0,
            custom_link=None,
            commission_pct=20,
            recent_earnings=[],
        )

    # Build link
    if code_obj.custom_slug:
        custom_link = f"https://suoops.com/join/{code_obj.custom_slug}"
    else:
        custom_link = build_referral_link(code_obj.code)

    # Count signups (all referrals)
    total_signups = db.execute(select(func.count(Referral.id)).where(Referral.referrer_id == user_id)).scalar() or 0

    # Count conversions (paid signups)
    total_conversions = (
        db.execute(
            select(func.count(Referral.id)).where(
                Referral.referrer_id == user_id,
                Referral.referral_type == ReferralType.PAID_SIGNUP,
                Referral.status == ReferralStatus.COMPLETED,
            )
        ).scalar()
        or 0
    )

    # Get all rewards to calculate earnings by type
    rewards = (
        db.execute(
            select(ReferralReward).where(ReferralReward.user_id == user_id).order_by(ReferralReward.created_at.desc())
        )
        .scalars()
        .all()
    )

    online_earned = 0
    topup_earned = 0
    pending_payout = 0

    for r in rewards:
        # Parse amount from reward_description (e.g. "₦500 commission...")
        amount = _extract_amount(r.reward_description)
        if r.reward_type == "commission_online":
            # % of SuoOps' 3% on a referred business's storefront/online sale.
            online_earned += amount
        else:
            # Wallet-top-up commission (commission_perpetual) + any legacy
            # first-purchase/recurring rewards all roll into the top-up bucket.
            topup_earned += amount

        if r.status == RewardStatus.PENDING:
            pending_payout += amount

    total_earned = online_earned + topup_earned

    # Recent earnings (last 20)
    recent = []
    for r in rewards[:20]:
        amount = _extract_amount(r.reward_description)
        recent.append(
            {
                "date": r.created_at.isoformat(),
                "type": r.reward_type,
                "amount": amount,
                "description": r.reward_description,
                "status": r.status.value,
            }
        )

    return EarningsBreakdown(
        total_earned=total_earned,
        online_earned=online_earned,
        topup_earned=topup_earned,
        total_signups=total_signups,
        total_conversions=total_conversions,
        pending_payout=pending_payout,
        custom_link=custom_link,
        commission_pct=int(code_obj.commission_perpetual_pct or 20),
        recent_earnings=recent,
    )


def _extract_amount(description: str) -> int:
    """Extract naira amount from reward description like '₦500 commission...'."""
    import re

    match = re.search(r"[₦N]?([\d,]+)", description)
    if match:
        return int(match.group(1).replace(",", ""))
    return 0
