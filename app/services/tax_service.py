"""
Tax Profile Management Service.

Handles:
- Business classification (small/medium/large)
- Tax profile updates
- Automatic tax rate determination

Single Responsibility: Tax profile management
"""
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional

from sqlalchemy.orm import Session

from app.models.tax_models import BusinessSize, MonthlyTaxReport, TaxProfile

logger = logging.getLogger(__name__)


class BusinessClassifier:
    """
    Business size classification (SRP: Classification logic only).
    
    Based on Nigeria Tax Act 2025 (NTA 2025) effective January 1, 2026:
    - Small: Turnover ≤ ₦100M (EXEMPT from CIT - 0%)
    - Medium: ₦100M < Turnover ≤ ₦250M (20% CIT)
    - Large: Turnover > ₦250M (30% CIT)
    
    Note: VAT exemption threshold remains at ₦25M separately.
    Small businesses owned by individuals may pay Personal Income Tax (PIT)
    using progressive rates instead of Company Income Tax (CIT).
    """
    
    SMALL_TURNOVER_THRESHOLD = Decimal("100000000")   # ₦100M (NTA 2025)
    SMALL_ASSETS_THRESHOLD = Decimal("250000000")     # ₦250M
    MEDIUM_TURNOVER_THRESHOLD = Decimal("250000000")  # ₦250M (medium/large boundary)
    
    @classmethod
    def classify(cls, turnover: Decimal, assets: Decimal) -> str:
        """
    Classify business size based on assumed criteria.
        
        Args:
            turnover: Annual turnover in Naira
            assets: Total fixed assets in Naira
            
        Returns:
            BusinessSize enum value (small/medium/large)
        """
        if turnover <= cls.SMALL_TURNOVER_THRESHOLD and assets <= cls.SMALL_ASSETS_THRESHOLD:
            return BusinessSize.SMALL
        elif turnover < cls.MEDIUM_TURNOVER_THRESHOLD:
            return BusinessSize.MEDIUM
        else:
            return BusinessSize.LARGE


class TaxProfileService:
    """
    Main tax profile service (orchestrates profile operations).
    
    Manages:
    - Profile creation and retrieval
    - Profile updates
    - Business classification
    """
    
    def __init__(self, db: Session):
        self.db = db
        self.classifier = BusinessClassifier()
    
    def get_or_create_profile(self, user_id: int) -> TaxProfile:
        """
        Get existing tax profile or create default one.
        
        Args:
            user_id: User ID
            
        Returns:
            TaxProfile instance
        """
        profile = self.db.query(TaxProfile).filter(
            TaxProfile.user_id == user_id
        ).first()
        
        if not profile:
            profile = TaxProfile(user_id=user_id)
            self.db.add(profile)
            self.db.commit()
            self.db.refresh(profile)
            logger.info("Created default tax profile for user %s", user_id)
        
        return profile
    
    def update_profile(
        self,
        user_id: int,
        annual_turnover: Optional[Decimal] = None,
        fixed_assets: Optional[Decimal] = None,
        tin: Optional[str] = None,
        vat_registration_number: Optional[str] = None,
        vat_registered: Optional[bool] = None,
        business_type: Optional[str] = None,
        vat_apply_to: Optional[str] = None,
        withholding_vat_applies: Optional[bool] = None,
    ) -> TaxProfile:
        """
        Update tax profile with new data.
        
        Args:
            user_id: User ID
            annual_turnover: Annual business turnover
            fixed_assets: Total fixed assets value
            tin: Tax Identification Number
            vat_registration_number: VAT registration number
            vat_registered: VAT registration status
            
        Returns:
            Updated TaxProfile
        """
        profile = self.get_or_create_profile(user_id)
        
        # Update fields if provided
        if annual_turnover is not None:
            profile.annual_turnover = annual_turnover
        
        if fixed_assets is not None:
            profile.fixed_assets = fixed_assets
        
        if tin is not None:
            # Basic TIN format validation (Nigeria): numeric, length 10, not trivial
            if tin:
                if not tin.isdigit():
                    raise ValueError("TIN must be numeric")
                if len(tin) != 10:
                    raise ValueError("TIN must be exactly 10 digits")
                if tin in {"0000000000", "1111111111", "1234567890"}:
                    raise ValueError("TIN appears invalid (trivial sequence)")
            profile.tin = tin
            # Reset verification flags if value changed
            profile.tin_verified = False
            profile.verification_status = "pending"
        
        if vat_registration_number is not None:
            if vat_registration_number:
                # Provisional VAT reg format check: allow alnum, length 8-15 (placeholder rule)
                import re
                if not re.fullmatch(r"[A-Za-z0-9]{8,15}", vat_registration_number):
                    raise ValueError("VAT registration number must be 8-15 alphanumeric characters")
            profile.vat_registration_number = vat_registration_number
            profile.vat_verified = False
            profile.verification_status = "pending"
        
        if vat_registered is not None:
            profile.vat_registered = vat_registered

        if business_type is not None:
            profile.business_type = business_type

        if vat_apply_to is not None:
            profile.vat_apply_to = vat_apply_to

        if withholding_vat_applies is not None:
            profile.withholding_vat_applies = withholding_vat_applies
        
        # Auto-classify business size
        if annual_turnover is not None or fixed_assets is not None:
            profile.business_size = self.classifier.classify(
                profile.annual_turnover,
                profile.fixed_assets
            )
            logger.info(
                f"User {user_id} classified as {profile.business_size} business "
                f"(turnover: ₦{profile.annual_turnover}, assets: ₦{profile.fixed_assets})"
            )
        
        self.db.commit()
        self.db.refresh(profile)
        
        return profile
    
    def get_tax_summary(self, user_id: int) -> Dict:
        """
        Get comprehensive tax profile summary.
        
        Args:
            user_id: User ID
            
        Returns:
            Dict with classification, rates, registration status
        """
        profile = self.get_or_create_profile(user_id)
        
        return {
            "user_id": user_id,
            "business_size": profile.business_size,
            "is_small_business": profile.is_small_business,
            "classification": {
                "annual_turnover": float(profile.annual_turnover),
                "fixed_assets": float(profile.fixed_assets),
                "small_business_threshold": {
                    "turnover": 100_000_000,
                    "assets": 250_000_000
                },
                "meets_small_criteria": profile.is_small_business
            },
            "tax_rates": profile.tax_rates,
            "registration": {
                "tin": profile.tin,
                "tin_verified": profile.tin_verified or False,
                "vat_registered": profile.vat_registered,
                "vat_number": profile.vat_registration_number,
                "firs_registered": profile.firs_registered,
                "firs_merchant_id": profile.firs_merchant_id,
                "business_type": profile.business_type or "mixed",
                "vat_apply_to": profile.vat_apply_to or "all",
                "withholding_vat_applies": profile.withholding_vat_applies or False,
                "rc_number": profile.rc_number,
                "cac_verified": profile.cac_verified or False,
                "cac_registered_name": profile.cac_registered_name,
            },
            "tax_benefits": self._get_tax_benefits(profile)
        }

    # Monthly report & assessment moved to tax_reporting_service.TaxReportingService
    
    def _get_tax_benefits(self, profile: TaxProfile) -> Dict:
        """Get list of applicable tax benefits based on classification"""
        if profile.is_small_business:
            return {
                "company_income_tax": "EXEMPT (₦0)",
                "capital_gains_tax": "EXEMPT (₦0)",
                "development_levy": "EXEMPT (₦0)",
                "vat": "APPLICABLE (7.5%)",
                "annual_savings": "Estimated ₦2M-10M (depending on profits)"
            }
        else:
            return {
                "company_income_tax": "25% on profits",
                "capital_gains_tax": "30% on capital gains",
                "development_levy": "4% on assessable profits",
                "vat": "7.5% standard rate",
                "note": "Consider optimizing business structure for tax efficiency"
            }

    # ---------------- Compliance & Eligibility (merged from legacy service) -----------------

    SMALL_BUSINESS_TURNOVER_LIMIT = Decimal("100000000")  # ₦100M (NTA 2025 - effective Jan 1, 2026)
    SMALL_BUSINESS_ASSETS_LIMIT = Decimal("250000000")    # ₦250M

    def check_small_business_eligibility(self, user_id: int) -> Dict[str, object]:
        """Return detailed small business eligibility info (unified schema)."""
        profile = self.get_or_create_profile(user_id)
        is_eligible = profile.is_small_business
        turnover_remaining = self.SMALL_BUSINESS_TURNOVER_LIMIT - profile.annual_turnover
        assets_remaining = self.SMALL_BUSINESS_ASSETS_LIMIT - profile.fixed_assets
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
                (turnover_remaining < Decimal("10000000"))
                or (assets_remaining < Decimal("25000000"))
            ) if is_eligible else False,
        }

    def get_compliance_summary(self, user_id: int) -> Dict[str, object]:
        """Unified compliance summary (TIN/VAT/NRS)."""
        profile = self.get_or_create_profile(user_id)
        requirements = {
            "tin_registered": profile.tin is not None,
            "vat_registered": profile.vat_registered,
            "firs_registered": profile.firs_registered,
        }
        completed = sum(1 for v in requirements.values() if v)
        total = len(requirements)
        compliance_score = (completed / total) * 100 if total else 0
        if compliance_score == 100:
            status = "fully_compliant"
        elif compliance_score >= 66:
            status = "mostly_compliant"
        elif compliance_score >= 33:
            status = "partially_compliant"
        else:
            status = "non_compliant"
        next_actions: list[str] = []
        if not profile.tin:
            next_actions.append("Register for Tax Identification Number (TIN)")
        if not profile.vat_registered and profile.annual_turnover > Decimal("25000000"):
            next_actions.append("Register for VAT (turnover exceeds ₦25M - VAT threshold unchanged under NTA 2025)")
        if not profile.firs_registered:
            next_actions.append("Register for forthcoming FIRS fiscalization/e-invoicing")
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
        profile = self.get_or_create_profile(user_id)
        # Use timezone-aware UTC timestamp for consistency
        profile.last_compliance_check = datetime.now(timezone.utc)
        self.db.commit()
        self.db.refresh(profile)
        return profile

    # -------- Alert Recording (lightweight) --------
    def record_alert(self, category: str, message: str, severity: str = "error") -> None:
        """Persist a simple alert event (best-effort)."""
        try:
            from app.models.alert_models import AlertEvent  # type: ignore
        except Exception:  # noqa: BLE001
            logger.debug("AlertEvent model not available, skipping alert")
            return
        evt = AlertEvent(category=category, message=message, severity=severity)
        self.db.add(evt)
        try:
            self.db.commit()
        except Exception:  # noqa: BLE001
            logger.debug("Failed to persist alert: %s", message)
            self.db.rollback()

    # ---------------- Development Levy Computation (minimal compliance helper) -----------------
    # Development levy calculation moved to reporting service.
    def compute_development_levy(self, user_id: int, assessable_profit: Decimal):  # backward compat wrapper
        """Wrapper delegating to TaxReportingService.compute_development_levy.

        NOTE: Core implementation moved to tax_reporting_service.TaxReportingService.
        This wrapper preserves existing public API while callers migrate.
        """
        from app.services.tax_reporting_service import (
            TaxReportingService,  # local import to avoid circular
        )
        return TaxReportingService(self.db).compute_development_levy(user_id, assessable_profit)

    # ---------------- Assessable profit computation -----------------
    # Assessable profit computation moved to reporting service.
    def compute_assessable_profit(
        self,
        user_id: int,
        year: Optional[int] = None,
        month: Optional[int] = None,
        basis: str = "paid",
    ) -> Decimal:  # type: ignore[override]
        """Wrapper delegating to TaxReportingService.compute_assessable_profit."""
        from app.services.tax_reporting_service import TaxReportingService
        return TaxReportingService(self.db).compute_assessable_profit(user_id, year=year, month=month, basis=basis)

    def generate_monthly_report(
        self,
        user_id: int,
        year: int,
        month: int,
        basis: str = "paid",
        force_regenerate: bool = False,
    ) -> MonthlyTaxReport:  # type: ignore[override]
        """Wrapper delegating to TaxReportingService.generate_monthly_report."""
        from app.services.tax_reporting_service import TaxReportingService
        return TaxReportingService(self.db).generate_monthly_report(
            user_id, year, month, basis=basis, force_regenerate=force_regenerate
        )

    def attach_report_pdf(self, report: MonthlyTaxReport, pdf_url: str) -> MonthlyTaxReport:  # type: ignore[override]
        """Wrapper delegating to TaxReportingService.attach_report_pdf."""
        from app.services.tax_reporting_service import TaxReportingService
        return TaxReportingService(self.db).attach_report_pdf(report, pdf_url)

    # ---------------- Tax constants (exposed to frontend) -----------------
    def get_tax_constants(self) -> Dict[str, object]:
        """Return static tax thresholds & rates for UI consumption."""
        from app.core.config import settings
        return {
            "small_business_turnover_limit": float(self.SMALL_BUSINESS_TURNOVER_LIMIT),
            "small_business_assets_limit": float(self.SMALL_BUSINESS_ASSETS_LIMIT),
            "development_levy_rate": 0.04,  # 4%
            "cit_rate_standard": 25,
            "cgt_rate_company": 30,
            "vat_rate_standard": settings.VAT_RATE,
        }
