"""
VAT Returns and Compliance Service.

Handles:
- Monthly VAT calculations
- VAT return generation
- Compliance status checking

Single Responsibility: VAT returns management
"""

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List

from sqlalchemy import and_
from sqlalchemy.orm import Session

from app.models.models import Invoice
from app.models.tax_models import TaxProfile, VATReturn

logger = logging.getLogger(__name__)


class VATCalculationService:
    """VAT calculation for reporting periods (SRP: Calculations only)"""

    @staticmethod
    def calculate_for_period(invoices: List[Invoice]) -> Dict:
        """
        Calculate VAT totals for a collection of invoices.

        Returns:
            Dict with output_vat, zero_rated_sales, exempt_sales, totals
        """
        output_vat = Decimal("0")
        zero_rated_sales = Decimal("0")
        exempt_sales = Decimal("0")
        total_invoices = 0
        fiscalized_invoices = 0

        for invoice in invoices:
            total_invoices += 1

            if invoice.is_fiscalized:
                fiscalized_invoices += 1

            # Categorize by VAT type
            if invoice.vat_category == "zero_rated":
                zero_rated_sales += invoice.amount
            elif invoice.vat_category == "exempt":
                exempt_sales += invoice.amount
            else:
                output_vat += invoice.vat_amount or Decimal("0")

        return {
            "output_vat": output_vat,
            "zero_rated_sales": zero_rated_sales,
            "exempt_sales": exempt_sales,
            "total_invoices": total_invoices,
            "fiscalized_invoices": fiscalized_invoices,
        }


class ComplianceChecker:
    """VAT compliance status checking (SRP: Compliance logic only)"""

    @staticmethod
    def check_status(tax_profile: TaxProfile, recent_returns: List[VATReturn]) -> str:
        """
        Determine VAT compliance status.

        Returns:
            Status string: not_registered, compliant, return_pending, return_overdue
        """
        if not tax_profile or not tax_profile.vat_registered:
            return "not_registered"

        if not recent_returns:
            return "return_overdue"

        # Check if last month's return is submitted (timezone-aware UTC)
        now = datetime.now(timezone.utc)
        last_month = now.replace(day=1) - timedelta(days=1)
        last_period = f"{last_month.year:04d}-{last_month.month:02d}"

        last_month_return = next(
            (vat_return for vat_return in recent_returns if vat_return.tax_period == last_period),
            None,
        )
        if last_month_return is None:
            return "return_overdue"
        if last_month_return.status == "submitted":
            return "compliant"
        return "return_pending"


class VATService:
    """
    Main VAT service (orchestrates VAT operations).

    Manages:
    - Monthly VAT calculations
    - Return generation and updates
    - Compliance summaries
    """

    def __init__(self, db: Session):
        self.db = db
        self.calculator = VATCalculationService()
        self.compliance_checker = ComplianceChecker()

    def calculate_monthly_vat(self, user_id: int, year: int, month: int) -> Dict:
        """
        Calculate VAT for a specific month.

        Args:
            user_id: User ID
            year: Year (e.g., 2026)
            month: Month (1-12)

        Returns:
            Dict with all VAT calculations for the period
        """
        # Get date range for the month
        start_date = datetime(year, month, 1, tzinfo=timezone.utc)
        if month == 12:
            next_month_start = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
        else:
            next_month_start = datetime(year, month + 1, 1, tzinfo=timezone.utc)
        end_date = next_month_start - timedelta(microseconds=1)

        # Fetch invoices for period
        invoices = (
            self.db.query(Invoice)
            .filter(
                and_(
                    Invoice.issuer_id == user_id,
                    Invoice.created_at >= start_date,
                    Invoice.created_at < next_month_start,
                    Invoice.status != "cancelled",
                )
            )
            .all()
        )

        # Calculate VAT
        vat_data = self.calculator.calculate_for_period(invoices)

        # TODO: Add input VAT from expenses when implemented
        input_vat = Decimal("0")

        return {
            "tax_period": f"{year:04d}-{month:02d}",
            "start_date": start_date,
            "end_date": end_date,
            "output_vat": vat_data["output_vat"],
            "input_vat": input_vat,
            "net_vat": vat_data["output_vat"] - input_vat,
            "zero_rated_sales": vat_data["zero_rated_sales"],
            "exempt_sales": vat_data["exempt_sales"],
            "total_invoices": vat_data["total_invoices"],
            "fiscalized_invoices": vat_data["fiscalized_invoices"],
        }

    def generate_vat_return(self, user_id: int, year: int, month: int) -> VATReturn:
        """
        Generate or update VAT return for a month.

        Args:
            user_id: User ID
            year: Year
            month: Month

        Returns:
            VATReturn record (created or updated)
        """
        tax_period = f"{year:04d}-{month:02d}"

        # Check for existing return
        vat_return = (
            self.db.query(VATReturn)
            .filter(and_(VATReturn.user_id == user_id, VATReturn.tax_period == tax_period))
            .first()
        )

        # Calculate current VAT data
        vat_data = self.calculate_monthly_vat(user_id, year, month)

        if vat_return:
            # Update existing return (only if in draft status)
            if vat_return.status == "draft":
                for key, value in vat_data.items():
                    if hasattr(vat_return, key):
                        setattr(vat_return, key, value)
                logger.info("Updated VAT return for user %s, period %s", user_id, tax_period)
            else:
                logger.warning("Cannot update submitted VAT return: %s", tax_period)
        else:
            # Create new return
            vat_return = VATReturn(user_id=user_id, **vat_data)
            self.db.add(vat_return)
            logger.info("Created VAT return for user %s, period %s", user_id, tax_period)

        self.db.commit()
        self.db.refresh(vat_return)

        return vat_return

    def get_vat_summary(self, user_id: int) -> Dict:
        """
        Get comprehensive VAT summary and compliance status.

        Args:
            user_id: User ID

        Returns:
            Dict with registration status, current month VAT, recent returns, compliance
        """
        # Get tax profile
        tax_profile = self.db.query(TaxProfile).filter(TaxProfile.user_id == user_id).first()

        if not tax_profile:
            return {"registered": False, "message": "Tax profile not set up. Please configure your tax settings."}

        # Get current month VAT (timezone-aware UTC)
        now = datetime.now(timezone.utc)
        current_vat = self.calculate_monthly_vat(user_id, now.year, now.month)

        # Get recent returns (last 3 months)
        recent_returns = (
            self.db.query(VATReturn)
            .filter(VATReturn.user_id == user_id)
            .order_by(VATReturn.tax_period.desc())
            .limit(3)
            .all()
        )

        # Check compliance
        compliance_status = self.compliance_checker.check_status(tax_profile, recent_returns)

        return {
            "registered": tax_profile.vat_registered,
            "vat_number": tax_profile.vat_registration_number,
            "tin": tax_profile.tin,
            "current_month": {
                "period": current_vat["tax_period"],
                "tax_period": current_vat["tax_period"],  # alias for frontend expectations
                "output_vat": float(current_vat["output_vat"]),
                "input_vat": float(current_vat["input_vat"]),
                "net_vat": float(current_vat["net_vat"]),
                "invoices": current_vat["total_invoices"],
                "fiscalized": current_vat["fiscalized_invoices"],
            },
            "recent_returns": [
                {
                    "period": r.tax_period,
                    "net_vat": float(r.net_vat),
                    "status": r.status,
                    "submitted_at": r.submitted_at.isoformat() if r.submitted_at else None,
                }
                for r in recent_returns
            ],
            "compliance_status": compliance_status,
            "next_action": self._get_next_action(compliance_status),
        }

    def _get_next_action(self, compliance_status: str) -> str:
        """Get recommended next action based on compliance status"""
        actions = {
            "not_registered": "Register for VAT with NRS",
            "compliant": "Up to date - no action needed",
            "return_pending": "Submit pending VAT return",
            "return_overdue": "File overdue VAT returns immediately",
        }
        return actions.get(compliance_status, "Unknown status")
