"""
Feature gating utilities for subscription-based access control.

NEW BILLING MODEL:
- Invoice Packs: 50 invoices for ₦1,250 (one-time purchase, never expires)
- FREE: 2 free invoices to start, then purchase packs. Tax features included.
- PRO (₦2,000 Pro Pack): 20 invoices + 30 days of premium features (branding, inventory, team, voice)

Note: STARTER plan removed. Users start FREE, buy packs as needed.
Frontend shows "Starter" as a UX label for non-Pro users.
Invoice balance is decremented per use. All plans can purchase more packs.
Pro users keep features even when invoices are exhausted.
"""

import datetime as dt
import logging

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import models

logger = logging.getLogger(__name__)

# Invoice pack pricing (invoices only, never expire)
INVOICE_PACK_SIZE = 50
INVOICE_PACK_PRICE = 1250  # ₦1,250

# Pro features grant window (days) — kept for the in-flight subscription webhook
# that honours existing Pro subscribers until their period lapses. Pro is no
# longer sold.
PRO_FEATURES_DAYS = 30
PRO_FEATURES_PRICE = 1500  # ₦1,500/month (legacy recurring plan, not sold)

# ── Commission billing model ──
# The platform takes a percentage per invoice, but the rate/caps differ by
# channel:
#   • Manual invoices     → 0.5%, floor ₦100, ₦400 cap under ₦500,000, uncapped above.
#     Charged from the business's prepaid wallet at creation (the business pays
#     it), so the rate is kept low.
#   • Storefront / online → 3%, floor ₦20, cap ₦2,000 per ₦500,000 band.
#     Passed to Paystack as a commission the customer pays at checkout.

# Storefront / online commission.
STOREFRONT_FEE_PERCENT = 3
STOREFRONT_MIN_FEE_KOBO = 2000  # ₦20 floor per storefront order
STOREFRONT_CAP_BASE_KOBO = 200000  # ₦2,000 cap per ₦500,000 band
STOREFRONT_CAP_TIER_NAIRA = 500000  # storefront cap steps every ₦500,000

# Manual-invoice commission (wallet fee): 0.5% with a ₦100 floor and a flat ₦400
# ceiling for invoices UNDER ₦500,000. From ₦500,000 up the cap is removed, so
# high-value invoices pay the full 0.5% (₦500,000 → ₦2,500, ₦1,000,000 → ₦5,000).
MANUAL_FEE_PERCENT = 0.5  # 0.5% of the invoice amount
MANUAL_MIN_FEE_KOBO = 10000  # ₦100 floor per manual invoice
MANUAL_MAX_FEE_KOBO = 40000  # ₦400 cap (applies only below the uncap threshold)
MANUAL_UNCAP_THRESHOLD_NAIRA = 500000  # invoices ≥ this pay uncapped 0.5%

# Tiered fee cap (storefront). ``base_kobo`` for the first ₦500,000, then
# +``base_kobo`` for every additional ₦500,000 band the amount crosses into.
FEE_CAP_TIER_NAIRA = STOREFRONT_CAP_TIER_NAIRA  # storefront tier (₦500,000)

# ── Backward-compatible aliases (existing imports keep working) ──
PLATFORM_FEE_PERCENT = STOREFRONT_FEE_PERCENT
FEE_CAP_BASE_KOBO = STOREFRONT_CAP_BASE_KOBO
# Wallet affordability floor: the smallest wallet balance that can fund one
# manual invoice (₦100, matching the manual fee floor).
MANUAL_INVOICE_MIN_FEE_KOBO = MANUAL_MIN_FEE_KOBO
MANUAL_INVOICE_MAX_FEE_KOBO = MANUAL_MAX_FEE_KOBO

# Wallet top-up tiers (Naira) sold to fund manual invoicing.
WALLET_TOPUP_TIERS = [1250, 5000, 20000]


def fee_cap_kobo(
    amount,
    base_kobo: int = STOREFRONT_CAP_BASE_KOBO,
    tier_naira: int = FEE_CAP_TIER_NAIRA,
) -> int:
    """Tiered fee cap in kobo for a transaction of ``amount`` Naira.

    ``base_kobo`` for the first ``tier_naira`` band, then +``base_kobo`` for
    every additional band the amount CROSSES into (exact multiples stay in the
    lower band).
    """
    from decimal import Decimal
    from math import ceil

    amt = Decimal(str(amount or 0))
    if amt <= 0:
        tiers = 1
    else:
        tiers = max(1, ceil(amt / Decimal(tier_naira)))
    return base_kobo * tiers


def platform_fee_kobo(amount, channel: str = "storefront") -> int:
    """Platform commission in kobo for ``amount`` Naira.

    ``channel="manual"``     → 0.5%, floor ₦100, ₦400 cap below ₦500,000, then
    uncapped 0.5% at/above ₦500,000 (₦500k → ₦2,500, ₦1M → ₦5,000).
    ``channel="storefront"`` → 3%, floor ₦20, cap ₦2,000 per ₦500,000 band
    (default, also used for any online/Paystack commission).

    Amount is in Naira, so pct% of it in kobo is simply ``amount * pct``.
    """
    from decimal import ROUND_HALF_UP, Decimal

    amt = Decimal(str(amount or 0))

    if channel == "manual":
        fee_kobo = int((amt * Decimal(str(MANUAL_FEE_PERCENT))).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        # Flat ₦400 cap for invoices under ₦500,000; ≥ ₦500,000 pays uncapped 0.5%.
        if amt < MANUAL_UNCAP_THRESHOLD_NAIRA:
            fee_kobo = min(fee_kobo, MANUAL_MAX_FEE_KOBO)
        return max(fee_kobo, MANUAL_MIN_FEE_KOBO)

    # Storefront / online (default): 3% with a tiered ₦2,000-per-₦500,000 cap.
    fee_kobo = (amt * Decimal(str(STOREFRONT_FEE_PERCENT))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return min(
        max(int(fee_kobo), STOREFRONT_MIN_FEE_KOBO),
        fee_cap_kobo(amount, STOREFRONT_CAP_BASE_KOBO, STOREFRONT_CAP_TIER_NAIRA),
    )


class FeatureGate:
    """Check if user has access to features and invoice balance."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self._user = None

    @property
    def user(self) -> models.User:
        """Lazy load user from database and check subscription expiry."""
        if self._user is None:
            self._user = self.db.query(models.User).filter(models.User.id == self.user_id).first()
            if not self._user:
                raise HTTPException(status_code=404, detail="User not found")
            # Check if paid subscription (Pro/Business) has expired
            self._check_subscription_expiry()
        return self._user

    def _check_subscription_expiry(self) -> None:
        """
        Check if user's Pro/Business subscription has expired.

        When expired:
        - Pro/Business → FREE (lose all premium features)
        - Invoice balance is preserved (they paid for those invoices)
        - FREE users can still buy invoice packs as needed
        """
        user = self._user
        now = dt.datetime.now(dt.timezone.utc)

        # Only Pro and Business have monthly subscriptions that can expire
        if not user.plan.has_monthly_subscription:
            return

        if user.subscription_expires_at is None:
            return  # No expiry set (legacy data)

        expiry = user.subscription_expires_at
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=dt.timezone.utc)

        if now > expiry:
            # Subscription has expired - downgrade to FREE (basic invoicing only)
            old_plan = user.plan.value
            user.plan = models.SubscriptionPlan.FREE
            user.subscription_expires_at = None
            # Keep invoice_balance - they paid for those!
            self.db.commit()
            logger.info(
                "Subscription expired for user %s: downgraded from %s to FREE (invoice balance: %d)",
                user.id,
                old_plan,
                getattr(user, "invoice_balance", 0),
            )

    def is_free_tier(self) -> bool:
        """Check if user is on free tier (respects pro_override)."""
        return self.user.effective_plan == models.SubscriptionPlan.FREE

    def is_paid_tier(self) -> bool:
        """Check if user has any paid subscription (not FREE, respects pro_override)."""
        return self.user.effective_plan != models.SubscriptionPlan.FREE

    def can_create_invoice(self) -> tuple[bool, str | None]:
        """
        Check if the wallet can cover at least the minimum manual-invoice fee.

        The precise per-invoice fee (3%, min ₦20, tiered ₦2,000-per-₦500k cap) is charged at creation; this
        is a cheap pre-gate so we fail fast when the wallet is effectively empty.

        Returns:
            (can_create: bool, error_message: str | None)
        """
        wallet = int(getattr(self.user, "wallet_balance_kobo", 0) or 0)

        if wallet < MANUAL_INVOICE_MIN_FEE_KOBO:
            return False, (
                "Your invoice wallet is empty. Top up to keep creating invoices, "
                "or share your storefront link so customers order and pay online."
            )

        return True, None

    def get_monthly_invoice_count(self) -> int:
        """Return count of revenue invoices created in the current month.

        This is primarily used for dashboards/analytics and legacy UI.
        It does not affect invoice balance enforcement (which is balance-based).
        """
        now = dt.datetime.now(dt.timezone.utc)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        count = (
            self.db.query(func.count(models.Invoice.id))
            .filter(
                models.Invoice.issuer_id == self.user_id,
                models.Invoice.invoice_type == "revenue",
                models.Invoice.created_at >= month_start,
            )
            .scalar()
        )
        return int(count or 0)

    def require_paid_plan(self, feature_name: str = "This feature") -> None:
        """No-op: every feature is free under the commission model.

        Premium tiers were removed — the platform monetises via a flat 3% per
        invoice (wallet on manual, commission on storefront), so all features are
        available to all users. Kept as a no-op so existing call sites are
        unchanged.
        """
        return None

    def check_invoice_creation(self) -> None:
        """
        Check if user can create invoice and raise exception if not.

        Raises:
            HTTPException: 403 if no invoice balance
        """
        can_create, error_msg = self.can_create_invoice()
        if not can_create:
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "invoice_wallet_empty",
                    "message": error_msg,
                    "wallet_balance_kobo": int(getattr(self.user, "wallet_balance_kobo", 0) or 0),
                    "topup_from": WALLET_TOPUP_TIERS[0],
                    "purchase_url": "/invoices/purchase-pack",
                },
            )

    def get_monthly_voice_count(self) -> int:
        """
        Get number of voice invoices created this month.
        Used for Pro plan quota enforcement.
        """
        # TODO: Track voice invoices separately in database
        # For now, return 0 (will implement proper tracking in next iteration)
        return 0

    def can_use_voice(self) -> tuple[bool, str | None]:
        """
        Check if user can use voice features (Pro plan with quota check).

        Pro plan: 15 voice invoices per month

        Returns:
            (can_use: bool, error_message: str | None)
        """
        plan = self.user.effective_plan
        features = plan.features

        # Check if plan has voice access at all
        if not features.get("voice_invoice"):
            return False, (
                "Voice invoices are only available on the Pro plan. " "Upgrade to unlock this premium feature."
            )

        # Pro plan: check quota (15 per month)
        if plan == models.SubscriptionPlan.PRO:
            quota = features.get("voice_quota", 15)  # 15 premium invoices
            current_count = self.get_monthly_voice_count()

            if current_count >= quota:
                return False, (
                    f"You've reached your Pro plan voice quota of {quota} premium invoices per month. "
                    "You can still create manual text invoices."
                )

            return True, None

        # Shouldn't reach here, but safe fallback
        return False, "Voice not available on your plan"

    def check_voice_quota(self) -> None:
        """
        Check voice quota and raise exception if exceeded.

        Raises:
            HTTPException: 403 if quota exceeded or feature not available
        """
        can_use, error_msg = self.can_use_voice()
        if not can_use:
            quota = 15 if self.user.effective_plan == models.SubscriptionPlan.PRO else 0
            current_count = self.get_monthly_voice_count()

            raise HTTPException(
                status_code=403,
                detail={
                    "error": "voice_quota_exceeded" if current_count >= quota else "voice_not_available",
                    "message": error_msg,
                    "current_count": current_count,
                    "quota": quota,
                    "current_plan": self.user.plan.value,
                    "upgrade_url": "/subscription/initialize",
                },
            )


def require_paid_plan(db: Session, user_id: int, feature_name: str = "This feature") -> None:
    """
    Convenience function to check if user has paid plan.

    Args:
        db: Database session
        user_id: User ID to check
        feature_name: Name of feature for error message

    Raises:
        HTTPException: 403 if user is on free tier
    """
    gate = FeatureGate(db, user_id)
    gate.require_paid_plan(feature_name)


def check_invoice_limit(db: Session, user_id: int) -> None:
    """
    Convenience function to check invoice creation limits.

    Args:
        db: Database session
        user_id: User ID to check

    Raises:
        HTTPException: 403 if invoice limit reached
    """
    gate = FeatureGate(db, user_id)
    gate.check_invoice_creation()


def grant_pro_features(user: models.User, days: int) -> None:
    """Grant/extend prepaid (non-recurring) Pro features for ``days`` days.

    Used by Pro Pack / Pro Features pass purchases. Sets the plan to PRO and
    extends ``subscription_expires_at``. If the user still has active Pro time
    the new window is stacked on top of the current expiry; otherwise it starts
    from now. No Paystack subscription code is set, so there is no auto-renew —
    when the window passes the user lapses back to FREE (invoice balance kept).

    The caller is responsible for committing the session.
    """
    if days <= 0:
        return
    now = dt.datetime.now(dt.timezone.utc)
    current = getattr(user, "subscription_expires_at", None)
    if current is not None and current.tzinfo is None:
        current = current.replace(tzinfo=dt.timezone.utc)
    base = current if (current is not None and current > now) else now
    user.plan = models.SubscriptionPlan.PRO
    user.subscription_expires_at = base + dt.timedelta(days=days)
    if getattr(user, "subscription_started_at", None) is None:
        user.subscription_started_at = now


def check_voice_ocr_quota(db: Session, user_id: int) -> None:
    """
    Convenience function to check voice quota for Pro plan.

    Note: OCR feature has been removed. This function now only checks voice quota.
    Kept the old name for backward compatibility with existing code.

    Args:
        db: Database session
        user_id: User ID to check

    Raises:
        HTTPException: 403 if quota exceeded or feature not available
    """
    gate = FeatureGate(db, user_id)
    gate.check_voice_quota()


def require_plan_feature(db: Session, user_id: int, feature_key: str, feature_name: str = None) -> None:
    """
    Check if user's plan has a specific feature.

    Args:
        db: Database session
        user_id: User ID to check
        feature_key: Key in plan.features dict (e.g., 'custom_branding', 'tax_automation')
        feature_name: Human-readable feature name for error message

    Raises:
        HTTPException: 403 if feature not available on user's plan
    """
    gate = FeatureGate(db, user_id)
    features = gate.user.effective_plan.features

    if not features.get(feature_key):
        feature_display = feature_name or feature_key.replace("_", " ").title()
        raise HTTPException(
            status_code=403,
            detail={
                "error": "feature_not_available",
                "message": (
                    f"{feature_display} is not available on your "
                    f"{gate.user.effective_plan.value} plan. Please upgrade."
                ),
                "current_plan": gate.user.effective_plan.value,
                "required_feature": feature_key,
                "upgrade_url": "/subscription/initialize",
            },
        )
