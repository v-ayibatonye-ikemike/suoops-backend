"""
Referral service for managing referral codes, tracking referrals, and distributing rewards.

COMMISSION MODEL (Updated June 2026 — Influencer Program):
- Each user gets a unique 8-character referral code
- Influencer codes can have custom vanity slugs (e.g. "coachade")
- First Pro purchase commission: ₦500 (25% of ₦2,000 Pro Pack) — configurable per code
- Recurring commission: ₦100 for months 2–5 (5%) — configurable per code
- Max total per referred user: ₦900 over 5 months
- Bonus invoices: influencer codes can grant extra free invoices to signups
- CASH PAYOUT: Commissions are paid out at the end of each month
- Rewards expire after 90 days if not claimed for payout
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session, joinedload

from app.models.models import User
from app.models.referral_models import (
    REFERRAL_COMMISSION_AMOUNT,
    Referral,
    ReferralCode,
    ReferralReward,
    ReferralStatus,
    ReferralType,
    RewardStatus,
    generate_referral_code,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class ReferralService:
    """Service for managing the referral program."""

    def __init__(self, db: Session):
        self.db = db

    # ==================== REFERRAL CODE MANAGEMENT ====================

    def get_or_create_referral_code(self, user_id: int) -> ReferralCode:
        """
        Get user's referral code or create one if it doesn't exist.
        """
        # Check for existing code
        existing = self.db.execute(select(ReferralCode).where(ReferralCode.user_id == user_id)).scalar_one_or_none()

        if existing:
            return existing

        # Generate new unique code
        for _ in range(10):  # Max 10 attempts to avoid collision
            code = generate_referral_code()
            # Check uniqueness
            exists = self.db.execute(select(ReferralCode).where(ReferralCode.code == code)).scalar_one_or_none()
            if not exists:
                break
        else:
            # Fallback: add user_id suffix
            code = f"{generate_referral_code()[:5]}{user_id}"

        referral_code = ReferralCode(user_id=user_id, code=code)
        self.db.add(referral_code)
        self.db.commit()
        self.db.refresh(referral_code)

        logger.info("Created referral code %s for user %s", code, user_id)
        return referral_code

    def get_code_by_string(self, code: str) -> ReferralCode | None:
        """Look up a referral code by its code string or custom vanity slug."""
        # Try exact code match first
        result = self.db.execute(
            select(ReferralCode).where(ReferralCode.code == code.upper()).where(ReferralCode.is_active.is_(True))
        ).scalar_one_or_none()
        if result:
            return result
        # Try custom slug (case-insensitive)
        return self.db.execute(
            select(ReferralCode)
            .where(func.lower(ReferralCode.custom_slug) == code.lower())
            .where(ReferralCode.is_active.is_(True))
        ).scalar_one_or_none()

    def validate_referral_code(self, code: str, referred_user_id: int) -> tuple[bool, str]:
        """
        Validate a referral code for a new user.
        Returns (is_valid, error_message).
        """
        referral_code = self.get_code_by_string(code)

        if not referral_code:
            return False, "Invalid referral code"

        if referral_code.user_id == referred_user_id:
            return False, "You cannot use your own referral code"

        # Check if user was already referred
        existing_referral = self.db.execute(
            select(Referral).where(Referral.referred_id == referred_user_id)
        ).scalar_one_or_none()

        if existing_referral:
            return False, "You have already been referred by someone"

        return True, ""

    # ==================== REFERRAL TRACKING ====================

    def record_referral(
        self,
        code: str,
        referred_user_id: int,
        referral_type: ReferralType = ReferralType.FREE_SIGNUP,
        ip_address: str | None = None,
        user_agent: str | None = None,
    ) -> Referral | None:
        """
        Record a new referral when a user signs up with a referral code.
        """
        referral_code = self.get_code_by_string(code)
        if not referral_code:
            logger.warning("Invalid referral code: %s", code)
            return None

        # Validate
        is_valid, error = self.validate_referral_code(code, referred_user_id)
        if not is_valid:
            logger.warning("Invalid referral: %s", error)
            return None

        referral = Referral(
            referral_code_id=referral_code.id,
            referrer_id=referral_code.user_id,
            referred_id=referred_user_id,
            referral_type=referral_type,
            status=ReferralStatus.PENDING,
            ip_address=ip_address,
            user_agent=user_agent,
        )
        self.db.add(referral)
        self.db.commit()
        self.db.refresh(referral)

        logger.info(
            "Recorded referral: user %s referred user %s (type: %s)",
            referral_code.user_id,
            referred_user_id,
            referral_type.value,
        )
        return referral

    def complete_referral(self, referred_user_id: int) -> bool:
        """
        Mark a referral as completed (e.g., after user verification).
        This is called when the referred user completes signup/verification.
        """
        referral = self.db.execute(
            select(Referral)
            .where(Referral.referred_id == referred_user_id)
            .where(Referral.status == ReferralStatus.PENDING)
        ).scalar_one_or_none()

        if not referral:
            return False

        referral.status = ReferralStatus.COMPLETED
        referral.completed_at = dt.datetime.now(dt.timezone.utc)
        self.db.commit()

        logger.info("Completed referral for user %s", referred_user_id)

        # Check if referrer has earned a reward
        self._check_and_create_reward(referral.referrer_id)

        return True

    def upgrade_referral_to_paid(self, referred_user_id: int) -> bool:
        """
        Upgrade a referral from free to paid when referred user subscribes to Pro.
        COMMISSION MODEL: Creates instant ₦488 commission reward for the referrer.
        """
        referral = self.db.execute(
            select(Referral)
            .where(Referral.referred_id == referred_user_id)
            .where(Referral.status == ReferralStatus.COMPLETED)
        ).scalar_one_or_none()

        if not referral:
            return False

        if referral.referral_type == ReferralType.PAID_SIGNUP:
            return True  # Already paid

        referral.referral_type = ReferralType.PAID_SIGNUP
        self.db.commit()

        logger.info("Upgraded referral for user %s to paid", referred_user_id)

        # COMMISSION MODEL: Create immediate reward for the referrer
        self._create_commission_reward(referral.referrer_id, referred_user_id)

        return True

    def process_recurring_commission(self, referred_user_id: int, purchase_amount: int = 2000) -> bool:
        """
        Create a recurring or perpetual commission reward when a referred user
        makes a purchase (Pro Pack or invoice pack).

        Commission tiers:
        - Purchases 2 to (1 + commission_months): fixed commission_recurring amount
        - After that: commission_perpetual_pct % of purchase_amount, forever

        Args:
            referred_user_id: The user who was referred and just made a purchase
            purchase_amount: The purchase amount in Naira (for perpetual % calc)

        Returns True if a commission reward was created.
        """
        referral = self.db.execute(
            select(Referral)
            .where(Referral.referred_id == referred_user_id)
            .where(Referral.status == ReferralStatus.COMPLETED)
            .where(Referral.referral_type == ReferralType.PAID_SIGNUP)
        ).scalar_one_or_none()

        if not referral:
            return False

        # Get the referral code to check commission config
        code_obj = self.db.execute(
            select(ReferralCode).where(ReferralCode.id == referral.referral_code_id)
        ).scalar_one_or_none()

        if not code_obj or not code_obj.is_active:
            return False

        # Count how many recurring/perpetual rewards have already been paid
        past_rewards = (
            self.db.execute(
                select(func.count(ReferralReward.id))
                .where(ReferralReward.user_id == referral.referrer_id)
                .where(ReferralReward.reward_type.in_(["commission_recurring", "commission_perpetual"]))
                .where(ReferralReward.reward_description.contains(f"user #{referred_user_id}"))
            ).scalar()
            or 0
        )

        # Determine which tier we're in
        max_recurring = code_obj.commission_months  # e.g. 2 (purchases 2-3)
        perpetual_pct = code_obj.commission_perpetual_pct  # e.g. 5

        if past_rewards < max_recurring:
            # Still in the fixed recurring window (₦200 per purchase)
            commission_amount = code_obj.commission_recurring
            reward_type = "commission_recurring"
            purchase_num = past_rewards + 2  # +2 because purchase 1 was the first
            desc_label = f"purchase #{purchase_num}"
        elif perpetual_pct > 0:
            # Perpetual phase: 5% of purchase amount
            commission_amount = purchase_amount * perpetual_pct // 100
            if commission_amount <= 0:
                return False
            reward_type = "commission_perpetual"
            purchase_num = past_rewards + 2
            desc_label = f"purchase #{purchase_num}, {perpetual_pct}% of ₦{purchase_amount:,}"
        else:
            # No perpetual commission configured
            return False

        referred_user = self.db.execute(select(User).where(User.id == referred_user_id)).scalar_one_or_none()
        referred_name = referred_user.name if referred_user else "a user"

        reward = ReferralReward(
            user_id=referral.referrer_id,
            reward_type=reward_type,
            reward_description=(
                f"₦{commission_amount} commission for {referred_name}'s " f"{desc_label} (user #{referred_user_id})"
            ),
            free_referrals_count=0,
            paid_referrals_count=0,
            status=RewardStatus.PENDING,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90),
        )
        self.db.add(reward)
        self.db.commit()

        logger.info(
            "Created %s commission ₦%s for referrer %s (referred user %s, %s)",
            reward_type,
            commission_amount,
            referral.referrer_id,
            referred_user_id,
            desc_label,
        )
        return True

    def process_topup_commission(self, referred_user_id: int, topup_naira: int) -> bool:
        """Pay the referrer a share of a referred user's wallet top-up.

        This is the settlement event under the commission billing model: when a
        referred business tops up their invoice wallet (money we receive), the
        referrer earns commission_perpetual_pct%% of that top-up. Because it is a
        fraction of cash we just collected, the platform always stays profitable
        (we keep 100 - pct%%, minus Paystack fees the customer already covered).

        The first top-up also converts the referral to PAID_SIGNUP.
        Returns True if a commission reward was created.
        """
        if topup_naira <= 0:
            return False

        referral = self.db.execute(
            select(Referral)
            .where(Referral.referred_id == referred_user_id)
            .where(Referral.status == ReferralStatus.COMPLETED)
        ).scalar_one_or_none()
        if not referral:
            return False

        code_obj = self.db.execute(
            select(ReferralCode).where(ReferralCode.id == referral.referral_code_id)
        ).scalar_one_or_none()
        if not code_obj or not code_obj.is_active:
            return False

        # First top-up converts the referral to "paid".
        if referral.referral_type != ReferralType.PAID_SIGNUP:
            referral.referral_type = ReferralType.PAID_SIGNUP

        pct = max(0, int(code_obj.commission_perpetual_pct or 0))
        commission_amount = topup_naira * pct // 100
        if commission_amount <= 0:
            self.db.commit()  # persist the conversion even if pct is 0
            return False

        referred_user = self.db.execute(select(User).where(User.id == referred_user_id)).scalar_one_or_none()
        referred_name = referred_user.name if referred_user else "a user"

        reward = ReferralReward(
            user_id=referral.referrer_id,
            reward_type="commission_perpetual",
            reward_description=(
                f"₦{commission_amount} commission ({pct}% of {referred_name}'s "
                f"₦{topup_naira:,} wallet top-up, user #{referred_user_id})"
            ),
            free_referrals_count=0,
            paid_referrals_count=0,
            status=RewardStatus.PENDING,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90),
        )
        self.db.add(reward)
        self.db.commit()
        logger.info(
            "Referral top-up commission ₦%s (%s%%) for referrer %s from user %s top-up ₦%s",
            commission_amount,
            pct,
            referral.referrer_id,
            referred_user_id,
            topup_naira,
        )
        return True

    def process_storefront_commission(self, referred_user_id: int, suoops_fee_naira: int) -> bool:
        """Pay the referrer a share of SuoOps' commission on a referred business's
        storefront / online sale.

        Unlike wallet top-ups (where the entire top-up is prepaid commission), a
        storefront order settles the flat 3% to SuoOps via Paystack at the moment
        of payment and never touches the wallet. The referrer therefore earns
        ``commission_perpetual_pct``%% of THAT 3% fee — i.e. a slice of what SuoOps
        actually earns, never the gross sale value or the customer's payment.

        The first commissionable sale also converts the referral to PAID_SIGNUP.
        Returns True if a commission reward was created.
        """
        if suoops_fee_naira <= 0:
            return False

        referral = self.db.execute(
            select(Referral)
            .where(Referral.referred_id == referred_user_id)
            .where(Referral.status == ReferralStatus.COMPLETED)
        ).scalar_one_or_none()
        if not referral:
            return False

        code_obj = self.db.execute(
            select(ReferralCode).where(ReferralCode.id == referral.referral_code_id)
        ).scalar_one_or_none()
        if not code_obj or not code_obj.is_active:
            return False

        # First commissionable sale converts the referral to "paid".
        if referral.referral_type != ReferralType.PAID_SIGNUP:
            referral.referral_type = ReferralType.PAID_SIGNUP

        pct = max(0, int(code_obj.commission_perpetual_pct or 0))
        commission_amount = suoops_fee_naira * pct // 100
        if commission_amount <= 0:
            self.db.commit()  # persist the conversion even if pct is 0
            return False

        referred_user = self.db.execute(select(User).where(User.id == referred_user_id)).scalar_one_or_none()
        referred_name = referred_user.name if referred_user else "a user"

        reward = ReferralReward(
            user_id=referral.referrer_id,
            reward_type="commission_online",
            reward_description=(
                f"₦{commission_amount} commission ({pct}% of SuoOps' ₦{suoops_fee_naira:,} "
                f"3% fee on {referred_name}'s online sale, user #{referred_user_id})"
            ),
            free_referrals_count=0,
            paid_referrals_count=0,
            status=RewardStatus.PENDING,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90),
        )
        self.db.add(reward)
        self.db.commit()
        logger.info(
            "Referral online commission ₦%s (%s%% of ₦%s fee) for referrer %s from user %s",
            commission_amount,
            pct,
            suoops_fee_naira,
            referral.referrer_id,
            referred_user_id,
        )
        return True

    # ==================== REWARD MANAGEMENT ====================

    def _create_commission_reward(self, referrer_id: int, referred_user_id: int) -> ReferralReward | None:
        """
        Create an instant commission reward when a referred user subscribes to Pro.
        Uses per-code commission_first rate (default ₦500).
        CASH PAYOUT: Commissions are paid out at the end of each month.
        """
        # Get referred user name for reward description
        referred_user = self.db.execute(select(User).where(User.id == referred_user_id)).scalar_one_or_none()

        referred_name = referred_user.name if referred_user else "a user"

        # Look up the referral code to get the per-code commission rate
        referral = self.db.execute(
            select(Referral).where(Referral.referred_id == referred_user_id)
        ).scalar_one_or_none()
        commission_amount = REFERRAL_COMMISSION_AMOUNT  # default ₦500
        if referral:
            code_obj = self.db.execute(
                select(ReferralCode).where(ReferralCode.id == referral.referral_code_id)
            ).scalar_one_or_none()
            if code_obj:
                commission_amount = code_obj.commission_first

        # Create commission reward
        reward = ReferralReward(
            user_id=referrer_id,
            reward_type="commission_first_purchase",
            reward_description=f"₦{commission_amount} commission for {referred_name}'s first Pro purchase",
            free_referrals_count=0,
            paid_referrals_count=1,
            status=RewardStatus.PENDING,
            expires_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=90),
        )
        self.db.add(reward)
        self.db.commit()
        self.db.refresh(reward)

        logger.info(
            "Created commission reward for user %s: ₦%s " "(referred user %s subscribed to Pro)",
            referrer_id,
            commission_amount,
            referred_user_id,
        )
        return reward

    def _check_and_create_reward(self, referrer_id: int) -> ReferralReward | None:
        """
        Check if referrer has earned a new reward and create it.
        NOTE: This method is kept for backward compatibility but commission rewards
        are now created immediately via _create_commission_reward when a referral upgrades to paid.
        """
        # Get counts of completed referrals
        free_count = self._get_completed_referral_count(referrer_id, ReferralType.FREE_SIGNUP)
        paid_count = self._get_completed_referral_count(referrer_id, ReferralType.PAID_SIGNUP)

        # Get count of rewards already earned
        rewards_earned = self._get_rewards_count(referrer_id)

        # COMMISSION MODEL: Paid referrals create instant rewards via _create_commission_reward
        # This method now only logs stats, rewards are created immediately on upgrade
        logger.debug(
            f"Referral stats for user {referrer_id}: "
            f"free={free_count}, paid={paid_count}, rewards_earned={rewards_earned}"
        )

        return None

    def _get_completed_referral_count(self, referrer_id: int, referral_type: ReferralType) -> int:
        """Get count of completed referrals of a specific type."""
        result = self.db.execute(
            select(func.count(Referral.id))
            .where(Referral.referrer_id == referrer_id)
            .where(Referral.referral_type == referral_type)
            .where(Referral.status == ReferralStatus.COMPLETED)
        ).scalar()
        return result or 0

    def _get_rewards_count(self, user_id: int) -> int:
        """Get count of rewards earned by user."""
        result = self.db.execute(
            select(func.count(ReferralReward.id)).where(ReferralReward.user_id == user_id)
        ).scalar()
        return result or 0

    def get_pending_rewards(self, user_id: int) -> list[ReferralReward]:
        """Get all pending (unclaimed) rewards for a user."""
        return list(
            self.db.execute(
                select(ReferralReward)
                .where(ReferralReward.user_id == user_id)
                .where(ReferralReward.status == RewardStatus.PENDING)
                .where(
                    (ReferralReward.expires_at.is_(None))
                    | (ReferralReward.expires_at > dt.datetime.now(dt.timezone.utc))
                )
            )
            .scalars()
            .all()
        )

    def apply_reward(self, user_id: int, reward_id: int) -> tuple[bool, str]:
        """
        Mark a pending reward as claimed for cash payout.
        CASH PAYOUT MODEL: Commissions are paid out at the end of each month.
        Returns (success, message).
        """
        reward = self.db.execute(
            select(ReferralReward)
            .where(ReferralReward.id == reward_id)
            .where(ReferralReward.user_id == user_id)
            .where(ReferralReward.status == RewardStatus.PENDING)
        ).scalar_one_or_none()

        if not reward:
            return False, "Reward not found or already claimed"

        # Check expiry
        if reward.expires_at and reward.expires_at < dt.datetime.now(dt.timezone.utc):
            reward.status = RewardStatus.EXPIRED
            self.db.commit()
            return False, "Reward has expired"

        now = dt.datetime.now(dt.timezone.utc)

        # CASH PAYOUT MODEL: Mark as applied (pending payout at end of month)
        # The actual cash transfer happens in a separate monthly payout process
        reward.status = RewardStatus.APPLIED
        reward.applied_at = now

        self.db.commit()

        logger.info(
            f"Claimed referral reward {reward_id} for user {user_id}: "
            f"₦{REFERRAL_COMMISSION_AMOUNT} cash payout pending"
        )

        return True, f"Reward claimed! ₦{REFERRAL_COMMISSION_AMOUNT} will be paid out at the end of this month."

    # ==================== STATISTICS ====================

    def get_referral_stats(self, user_id: int) -> dict:
        """
        Get referral statistics for a user.
        COMMISSION MODEL: Shows earnings from paid referrals (₦488 each).
        """
        referral_code = self.get_or_create_referral_code(user_id)

        # Get referral counts
        free_completed = self._get_completed_referral_count(user_id, ReferralType.FREE_SIGNUP)
        paid_completed = self._get_completed_referral_count(user_id, ReferralType.PAID_SIGNUP)

        # Get pending referrals count
        pending_count = (
            self.db.execute(
                select(func.count(Referral.id))
                .where(Referral.referrer_id == user_id)
                .where(Referral.status == ReferralStatus.PENDING)
            ).scalar()
            or 0
        )

        # Get rewards
        pending_rewards = self.get_pending_rewards(user_id)
        total_rewards = self._get_rewards_count(user_id)

        # COMMISSION MODEL: Calculate total earnings
        total_commission_earned = paid_completed * REFERRAL_COMMISSION_AMOUNT  # ₦488 per paid referral

        from app.services.referral_share import build_referral_link

        return {
            "referral_code": referral_code.code,
            "referral_link": build_referral_link(referral_code.code),
            "total_referrals": free_completed + paid_completed,
            "pending_referrals": pending_count,
            "free_signups": free_completed,
            "paid_signups": paid_completed,
            "rewards_earned": total_rewards,
            "pending_rewards": len(pending_rewards),
            "pending_rewards_list": [
                {
                    "id": r.id,
                    "description": r.reward_description,
                    "expires_at": r.expires_at.isoformat() if r.expires_at else None,
                }
                for r in pending_rewards
            ],
            # CASH PAYOUT MODEL: Commission info
            "commission": {
                "rate_percentage": 15,  # 15% commission
                "amount_per_referral": REFERRAL_COMMISSION_AMOUNT,  # ₦488
                "total_earned": total_commission_earned,
                "payout_schedule": "monthly",  # Cash paid at end of month
            },
            # Simplified progress (no thresholds needed for commission model)
            "progress": {
                "paid_signups": {
                    "current": paid_completed,
                    "earnings": total_commission_earned,
                },
            },
        }

    def get_recent_referrals(self, user_id: int, limit: int = 10) -> list[dict]:
        """Get recent referrals for a user."""
        referrals = (
            self.db.execute(
                select(Referral)
                .options(joinedload(Referral.referred))
                .where(Referral.referrer_id == user_id)
                .order_by(Referral.created_at.desc())
                .limit(limit)
            )
            .scalars()
            .all()
        )

        return [
            {
                "id": r.id,
                "referred_name": r.referred.name if r.referred else "Unknown",
                "type": r.referral_type.value,
                "status": r.status.value,
                "created_at": r.created_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in referrals
        ]
