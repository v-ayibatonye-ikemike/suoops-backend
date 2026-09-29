"""
Referral system models for tracking referral codes, referrals, and rewards.

NOTE: The referral program is currently DISABLED (no UI, router and signup
capture removed). These models/tables are kept dormant so existing data is
preserved and the feature can be revived later. Figures below are legacy.

COMMISSION-BASED REFERRAL MODEL (legacy):
- Paid subscription referrals: 15% commission = ₦488 per Pro subscriber
- Free signup referrals: No reward (focus on quality paid referrals)
- CASH PAYOUT: Commissions are paid out at the end of each month

Note: Pro is now prepaid (₦2,000 Pro Pack). The ₦3,250/month figure below
reflects the old recurring price used when this program was active.
Only Pro counted as paid referrals and earned commission.

Economics (legacy, when Pro was ₦3,250/month recurring):
- Pro plan revenue: ₦3,250/month
- Commission per referral: ₦488 (15%)
- Referrer gets: ₦488 cash (paid monthly)
- Your profit after commission: ~₦2,762/month per customer
"""

from __future__ import annotations

import datetime as dt
import enum
import secrets
import string
from typing import TYPE_CHECKING

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base_class import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def generate_referral_code() -> str:
    """Generate a unique 8-character referral code (uppercase alphanumeric)."""
    alphabet = string.ascii_uppercase + string.digits
    # Remove ambiguous characters (0, O, I, 1, L)
    alphabet = alphabet.replace("0", "").replace("O", "").replace("I", "").replace("1", "").replace("L", "")
    return "".join(secrets.choice(alphabet) for _ in range(8))


class ReferralType(str, enum.Enum):
    """Type of referral based on referred user's action."""

    FREE_SIGNUP = "free_signup"  # Referred user signed up (free/starter tier)
    PAID_SIGNUP = "paid_signup"  # Referred user subscribed to Pro plan


class ReferralStatus(str, enum.Enum):
    """Status of a referral."""

    PENDING = "pending"  # User signed up but not yet verified/confirmed
    COMPLETED = "completed"  # Referral is valid and counted
    EXPIRED = "expired"  # Referral expired (user never completed action)
    FRAUDULENT = "fraudulent"  # Detected as abuse


class RewardStatus(str, enum.Enum):
    """Status of a referral reward."""

    PENDING = "pending"  # Reward earned but not yet applied
    APPLIED = "applied"  # Reward has been applied to user's account
    EXPIRED = "expired"  # Reward expired before being used


if TYPE_CHECKING:
    from app.models.models import User


class ReferralCode(Base):
    """
    Unique referral code for each user.
    Each user gets one code that they can share.
    Influencer codes have custom slugs, commission rates, and signup perks.
    """

    __tablename__ = "referral_code"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), unique=True, index=True)
    code: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    # ── Influencer / affiliate fields ────────────────────────────
    is_influencer: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    custom_slug: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True, index=True)
    influencer_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    influencer_contact: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # Commission: first Pro purchase
    commission_first: Mapped[int] = mapped_column(Integer, default=500, server_default="500")
    # Commission: recurring (purchases 2–N within commission_months)
    commission_recurring: Mapped[int] = mapped_column(Integer, default=200, server_default="200")
    # How many recurring commission purchases (after the first)
    commission_months: Mapped[int] = mapped_column(Integer, default=2, server_default="2")
    # Perpetual commission % on every purchase after the recurring window (0 = disabled)
    commission_perpetual_pct: Mapped[int] = mapped_column(Integer, default=5, server_default="5")
    # Bonus free invoices for users who sign up through this code
    bonus_invoices: Mapped[int] = mapped_column(Integer, default=3, server_default="3")
    # Admin notes
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    user: Mapped[User] = relationship("User", back_populates="referral_code")
    referrals: Mapped[list[Referral]] = relationship(
        "Referral",
        back_populates="referral_code",
        foreign_keys="Referral.referral_code_id",
    )


class Referral(Base):
    """
    Track each referral: who referred whom.
    """

    __tablename__ = "referral"

    id: Mapped[int] = mapped_column(primary_key=True)
    referral_code_id: Mapped[int] = mapped_column(ForeignKey("referral_code.id"), index=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)  # The user who shared the code
    referred_id: Mapped[int] = mapped_column(ForeignKey("user.id"), unique=True, index=True)  # The new user

    # Type and status
    referral_type: Mapped[ReferralType] = mapped_column(
        Enum(ReferralType),
        default=ReferralType.FREE_SIGNUP,
        index=True,
    )
    status: Mapped[ReferralStatus] = mapped_column(
        Enum(ReferralStatus),
        default=ReferralStatus.PENDING,
        index=True,
    )

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        index=True,
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Anti-abuse tracking
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)  # IPv6 max length
    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Relationships
    referral_code: Mapped[ReferralCode] = relationship(
        "ReferralCode",
        back_populates="referrals",
        foreign_keys=[referral_code_id],
    )
    referrer: Mapped[User] = relationship("User", foreign_keys=[referrer_id])
    referred: Mapped[User] = relationship("User", foreign_keys=[referred_id])


class ReferralReward(Base):
    """
    Track rewards earned and applied from referrals.
    """

    __tablename__ = "referral_reward"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True)  # The referrer who earned the reward

    # Reward details
    reward_type: Mapped[str] = mapped_column(String(50))  # e.g., "free_month_starter"
    reward_description: Mapped[str] = mapped_column(String(255))  # e.g., "1 month free Starter plan"

    # Thresholds at time of earning (for audit)
    free_referrals_count: Mapped[int] = mapped_column(Integer, default=0)  # How many free signups at time of reward
    paid_referrals_count: Mapped[int] = mapped_column(Integer, default=0)  # How many paid signups at time of reward

    # Status
    status: Mapped[RewardStatus] = mapped_column(
        Enum(RewardStatus),
        default=RewardStatus.PENDING,
    )

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    applied_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    # Relationship
    user: Mapped[User] = relationship("User", back_populates="referral_rewards")


# Referral thresholds (configurable)
# COMMISSION-BASED MODEL: 15% commission per paid referral - CASH PAYOUT
REFERRAL_THRESHOLDS = {
    "free_signup": {
        "required": 0,  # No reward for free signups (focus on paid referrals)
        "reward_type": "none",
        "reward_description": "No reward for free signups",
    },
    "paid_signup": {
        "required": 1,  # Each paid signup = instant commission
        "reward_type": "commission",
        "reward_description": "₦488 cash commission (15% of Pro plan)",
        "commission_amount": 488,  # ₦488 = 15% of ₦3,250 Pro plan
        "commission_percentage": 15,
        "payout_schedule": "monthly",  # Cash payout at end of month
    },
}

# Pro plan price for commission calculation
PRO_PLAN_PRICE = 2000  # ₦2,000 Pro Pack
REFERRAL_COMMISSION_PERCENTAGE = 25  # 25% first purchase
REFERRAL_COMMISSION_AMOUNT = 500  # ₦500 first purchase
REFERRAL_RECURRING_AMOUNT = 100  # ₦100 months 2–5
REFERRAL_RECURRING_MONTHS = 5  # pay recurring for up to 5 months after first
