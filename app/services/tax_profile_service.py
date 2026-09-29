"""
Tax Profile Management Service.

Handles business tax profile creation, updates, and compliance tracking.
Following SRP: Tax profile operations only.
"""

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models.tax_models import BusinessSize, TaxProfile

logger = logging.getLogger(__name__)


class TaxProfileService:
    """
    Manage tax profiles for businesses.

    Responsibilities:
    - Create/update tax profiles
    - Business size classification
    - Compliance status tracking
    - FIRS fiscalization registration management (pending external API)
    """

    # Small business thresholds per Nigeria Tax Act 2025 (NTA 2025) effective Jan 1, 2026
    SMALL_BUSINESS_TURNOVER_LIMIT = Decimal("100000000")  # ₦100M (CIT exempt)
    SMALL_BUSINESS_ASSETS_LIMIT = Decimal("250000000")  # ₦250M

    def __init__(self, db: Session):
        self.db = db

    def get_or_create_profile(self, user_id: int) -> TaxProfile:
        """
        Get existing tax profile or create default one.

        Args:
            user_id: User ID

        Returns:
            TaxProfile instance
        """
        profile = self.db.query(TaxProfile).filter(TaxProfile.user_id == user_id).first()

        if not profile:
            logger.info("Creating default tax profile for user %s", user_id)
            profile = self._create_default_profile(user_id)

        return profile

    def _create_default_profile(self, user_id: int) -> TaxProfile:
        """Create default tax profile for new user."""
        profile = TaxProfile(
            user_id=user_id,
            business_size=BusinessSize.SMALL,
            annual_turnover=Decimal("0"),
            fixed_assets=Decimal("0"),
            vat_registered=False,
            firs_registered=False,
            last_compliance_check=datetime.now(timezone.utc),
        )

        self.db.add(profile)
        self.db.commit()
        self.db.refresh(profile)

        logger.info("Default tax profile created for user %s", user_id)
        return profile

    def update_profile(self, user_id: int, data: Dict[str, Any]) -> TaxProfile:
        """
        Update tax profile with new data.

        Args:
            user_id: User ID
            data: Dictionary of fields to update

        Returns:
            Updated TaxProfile

        Raises:
            ValueError: If invalid data provided
        """
        profile = self.get_or_create_profile(user_id)

        # Allowed fields for update
        allowed_fields = {
            "annual_turnover",
            "fixed_assets",
            "tin",
            "vat_registration_number",
            "vat_registered",
            "firs_merchant_id",
            "firs_api_key",
        }

        # Update fields
        updated_fields = []
        for field, value in data.items():
            if field in allowed_fields:
                # Convert monetary values to Decimal
                if field in ["annual_turnover", "fixed_assets"] and value is not None:
                    value = Decimal(str(value))

                setattr(profile, field, value)
                updated_fields.append(field)

        # Auto-update business size classification
        if "annual_turnover" in data or "fixed_assets" in data:
            profile.business_size = self._classify_business_size(profile)
            updated_fields.append("business_size")

        # Update NRS registration status
        if "firs_merchant_id" in data and data["firs_merchant_id"]:
            profile.firs_registered = True
            updated_fields.append("firs_registered")

        profile.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(profile)

        logger.info(
            "Tax profile updated for user %s: fields=%s",
            user_id,
            ", ".join(updated_fields),
        )

        return profile

    def _classify_business_size(self, profile: TaxProfile) -> str:
        """
        Classify business size based on NRS 2026 thresholds.

        Small business criteria (both must be met):
        - Annual turnover ≤ ₦100M
        - Fixed assets ≤ ₦250M

        Args:
            profile: TaxProfile instance

        Returns:
            BusinessSize enum value
        """
        if (
            profile.annual_turnover <= self.SMALL_BUSINESS_TURNOVER_LIMIT
            and profile.fixed_assets <= self.SMALL_BUSINESS_ASSETS_LIMIT
        ):
            return BusinessSize.SMALL
        elif profile.annual_turnover <= Decimal("500000000"):  # ₦500M
            return BusinessSize.MEDIUM
        else:
            return BusinessSize.LARGE

    def update_turnover(self, user_id: int, new_invoice_amount: Decimal) -> TaxProfile:
        """
        Update annual turnover when new invoice is created.

        This helps track if user is approaching small business limit.

        Args:
            user_id: User ID
            new_invoice_amount: Amount of new invoice

        Returns:
            Updated TaxProfile
        """
        profile = self.get_or_create_profile(user_id)

        # Add to annual turnover
        profile.annual_turnover += new_invoice_amount

        # Reclassify business size
        old_size = profile.business_size
        profile.business_size = self._classify_business_size(profile)

        # Log if business size changed
        if old_size != profile.business_size:
            logger.warning(
                "User %s business size changed: %s → %s. Turnover: ₦%s",
                user_id,
                old_size,
                profile.business_size,
                f"{profile.annual_turnover:,}",
            )

        self.db.commit()
        self.db.refresh(profile)

        return profile

    def check_small_business_eligibility(self, user_id: int) -> Dict[str, Any]:
        """
        Check if user qualifies for small business tax exemptions.

        Returns detailed eligibility info and benefits.

        Args:
            user_id: User ID

        Returns:
            Dictionary with eligibility details
        """
        profile = self.get_or_create_profile(user_id)

        is_eligible = profile.is_small_business

        # Calculate distance from thresholds
        turnover_remaining = self.SMALL_BUSINESS_TURNOVER_LIMIT - profile.annual_turnover
        assets_remaining = self.SMALL_BUSINESS_ASSETS_LIMIT - profile.fixed_assets

        # Tax benefits
        benefits = []
        if is_eligible:
            benefits = [
                "0% Company Income Tax (CIT)",
                "0% Capital Gains Tax (CGT)",
                "0% Development Levy",
                "Simplified filing requirements",
                "Reduced compliance burden",
            ]

        return {
            "eligible": is_eligible,
            "business_size": profile.business_size,
            "current_turnover": float(profile.annual_turnover),
            "turnover_limit": float(self.SMALL_BUSINESS_TURNOVER_LIMIT),
            "turnover_remaining": float(turnover_remaining) if is_eligible else 0,
            "current_assets": float(profile.fixed_assets),
            "assets_limit": float(self.SMALL_BUSINESS_ASSETS_LIMIT),
            "assets_remaining": float(assets_remaining) if is_eligible else 0,
            "tax_rates": profile.tax_rates,
            "benefits": benefits,
            "approaching_limit": (
                turnover_remaining < Decimal("10000000")  # ₦10M buffer
                or assets_remaining < Decimal("25000000")  # ₦25M buffer
            )
            if is_eligible
            else False,
        }

    def register_for_firs(self, user_id: int, merchant_id: str, api_key: Optional[str] = None) -> TaxProfile:
        """
        Register business with FIRS (Federal Inland Revenue Service) for future fiscalization.

        Args:
            user_id: User ID
            merchant_id: NRS merchant/business ID
            api_key: NRS API key (optional)

        Returns:
            Updated TaxProfile
        """
        profile = self.get_or_create_profile(user_id)
        # Persist provisional FIRS/NRS registration metadata
        profile.firs_merchant_id = merchant_id
        profile.firs_registered = True
        if api_key:
            profile.firs_api_key = api_key
        profile.updated_at = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(profile)
        logger.info("User %s registered with FIRS (provisional): %s", user_id, merchant_id)
        return profile

    def register_for_vat(self, user_id: int, vat_number: str) -> TaxProfile:
        """
        Register business for VAT.

        Args:
            user_id: User ID
            vat_number: VAT registration number

        Returns:
            Updated TaxProfile
        """
        profile = self.get_or_create_profile(user_id)

        profile.vat_registration_number = vat_number
        profile.vat_registered = True
        profile.updated_at = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(profile)

        logger.info("User %s registered for VAT: %s", user_id, vat_number)

        return profile

    def get_compliance_summary(self, user_id: int) -> Dict[str, Any]:
        """
        Get tax compliance summary for user.

        Args:
            user_id: User ID

        Returns:
            Compliance summary dictionary
        """
        profile = self.get_or_create_profile(user_id)

        # Check what's required vs what's done
        requirements = {
            "tin_registered": profile.tin is not None,
            "vat_registered": profile.vat_registered,
            "firs_registered": profile.firs_registered,
        }

        # Compliance score
        completed = sum(1 for v in requirements.values() if v)
        total = len(requirements)
        compliance_score = (completed / total) * 100

        # Compliance status
        if compliance_score == 100:
            status = "fully_compliant"
        elif compliance_score >= 66:
            status = "mostly_compliant"
        elif compliance_score >= 33:
            status = "partially_compliant"
        else:
            status = "non_compliant"

        # Next actions
        next_actions = []
        if not profile.tin:
            next_actions.append("Register for Tax Identification Number (TIN)")
        if not profile.vat_registered and profile.annual_turnover > Decimal("25000000"):
            next_actions.append("Register for VAT (turnover exceeds ₦25M)")
        if not profile.firs_registered:
            next_actions.append("Register for future FIRS fiscalization / e-invoicing")

        return {
            "compliance_status": status,
            "compliance_score": compliance_score,
            "requirements": requirements,
            "next_actions": next_actions,
            "business_size": profile.business_size,
            "small_business_benefits": profile.is_small_business,
            "last_check": profile.last_compliance_check.isoformat() if profile.last_compliance_check else None,
        }

    def update_compliance_check(self, user_id: int) -> TaxProfile:
        """
        Update last compliance check timestamp.

        Args:
            user_id: User ID

        Returns:
            Updated TaxProfile
        """
        profile = self.get_or_create_profile(user_id)
        profile.last_compliance_check = datetime.now(timezone.utc)

        self.db.commit()
        self.db.refresh(profile)

        return profile
