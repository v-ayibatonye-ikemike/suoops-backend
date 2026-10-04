"""Tax Reporting Service.

Main service class for generating and managing tax reports
with multi-period aggregation support.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from app.models.tax_models import MonthlyTaxReport
from app.services.tax_service import TaxProfileService

from .computations import (
    compute_actual_profit_by_date_range,
    compute_company_income_tax,
    compute_personal_income_tax,
)
from .inventory_integration import get_inventory_cogs
from .period_utils import calculate_period_range

logger = logging.getLogger(__name__)


class TaxReportingService:
    """Service for generating tax reports with multi-period support.

    Responsibilities:
    - Assessable profit computation (basis-aware) for multiple time periods
    - VAT aggregation for day/week/month/year periods
    - Development levy computation
    - Report persistence & PDF attachment
    - Period type: day, week, month, year
    """

    def __init__(self, db: Session):
        self.db = db
        self.profile_service = TaxProfileService(db)

    def update_profile(self, user_id: int, **kwargs):
        """Wrapper to maintain backward compatibility for tests."""
        return self.profile_service.update_profile(user_id, **kwargs)

    def _calculate_period_range(
        self,
        period_type: str,
        year: Optional[int] = None,
        month: Optional[int] = None,
        day: Optional[int] = None,
        week: Optional[int] = None,
    ):
        """Calculate start_date and end_date for a given period type."""
        return calculate_period_range(
            period_type=period_type,
            year=year,
            month=month,
            day=day,
            week=week,
        )

    def _get_inventory_cogs(
        self,
        user_id: int,
        start_date: date,
        end_date: date,
    ) -> dict:
        """Get COGS data from inventory for a period."""
        return get_inventory_cogs(self.db, user_id, start_date, end_date)

    def generate_report(
        self,
        user_id: int,
        period_type: str = "month",
        year: Optional[int] = None,
        month: Optional[int] = None,
        day: Optional[int] = None,
        week: Optional[int] = None,
        basis: str = "paid",
        force_regenerate: bool = False,
    ) -> MonthlyTaxReport:
        """Generate or retrieve tax report for any period type.

        Args:
            user_id: User ID
            period_type: 'day', 'week', 'month', or 'year'
            year: Required for all period types
            month: Required for month/day reports
            day: Required for day reports
            week: Required for week reports (ISO week number)
            basis: 'paid' (only paid invoices) or 'all' (all non-refunded)
            force_regenerate: Force regeneration even if report exists

        Returns:
            MonthlyTaxReport instance with calculated data
        """
        # Calculate date range for the period
        start_date, end_date = self._calculate_period_range(
            period_type=period_type,
            year=year,
            month=month,
            day=day,
            week=week,
        )

        # Check for existing report
        existing = (
            self.db.query(MonthlyTaxReport)
            .filter(
                MonthlyTaxReport.user_id == user_id,
                MonthlyTaxReport.period_type == period_type,
                MonthlyTaxReport.start_date == start_date,
                MonthlyTaxReport.end_date == end_date,
            )
            .first()
        )

        if existing and not force_regenerate:
            return existing

        # Get COGS data from inventory FIRST (needed for profit calculation)
        cogs_data = self._get_inventory_cogs(user_id, start_date, end_date)
        cogs_amount = cogs_data.get("cogs_amount", Decimal("0"))

        # Compute profit: Revenue - Expenses - COGS
        base_profit = self.compute_assessable_profit_by_date_range(
            user_id=user_id,
            start_date=start_date,
            end_date=end_date,
            basis=basis,
        )
        # Deduct inventory COGS from profit
        profit = base_profit - cogs_amount
        if profit < Decimal("0"):
            profit = Decimal("0")  # Profit can't be negative for tax purposes

        logger.info(
            f"Tax profit calculation for user {user_id}: "
            f"Base profit (Rev-Exp)={base_profit}, COGS={cogs_amount}, Final profit={profit}"
        )

        levy = self.compute_development_levy(user_id, profit)

        # Calculate Personal Income Tax (PIT) on profit
        pit_calc = compute_personal_income_tax(profit)
        pit_amount = pit_calc["pit_amount"]

        # Calculate Company Income Tax (CIT) for PRO+ plans
        cit_data = self._compute_cit_data(user_id, profit, start_date, end_date)
        cit_amount = cit_data.get("cit_amount", Decimal("0"))

        # Get VAT data
        vat_data = self._compute_vat_data(user_id, start_date, end_date, basis)

        # Create or update report
        if not existing:
            report = self._create_report(
                user_id,
                period_type,
                start_date,
                end_date,
                year,
                month,
                profit,
                levy,
                pit_amount,
                cit_amount,
                vat_data,
                cogs_data,
            )
        else:
            report = self._update_report(existing, profit, levy, pit_amount, cit_amount, vat_data, cogs_data)

        self.db.commit()
        self.db.refresh(report)
        return report

    def _compute_cit_data(
        self,
        user_id: int,
        profit: Decimal,
        start_date: date,
        end_date: date,
    ) -> dict:
        """Compute Company Income Tax (CIT) for PRO plan."""
        from app.models.models import SubscriptionPlan, User

        user = self.db.query(User).filter(User.id == user_id).first()
        user_plan = user.plan if user else SubscriptionPlan.FREE

        # CIT is only calculated for PRO plan
        is_cit_eligible = user_plan == SubscriptionPlan.PRO

        if not is_cit_eligible:
            return {
                "cit_amount": Decimal("0"),
                "development_levy": Decimal("0"),
                "company_size": "n/a",
                "notes": "CIT guidance is included; confirm obligations with a qualified adviser",
            }

        # Get annual turnover estimate (for company size classification)
        # Estimate based on current period profit (annualized)
        days_in_period = (end_date - start_date).days + 1
        annual_turnover = (profit / days_in_period * 365) if days_in_period > 0 else profit * 12

        # Get tax profile for capital allowances if available
        from app.models.tax_models import TaxProfile

        tax_profile = self.db.query(TaxProfile).filter(TaxProfile.user_id == user_id).first()
        capital_allowances = None
        if tax_profile and tax_profile.fixed_assets:
            # Simplified: assume 25% depreciation on fixed assets per year
            annual_allowance = float(tax_profile.fixed_assets) * 0.25
            capital_allowances = Decimal(str(annual_allowance * days_in_period / 365))

        cit_calc = compute_company_income_tax(
            profit=profit,
            annual_turnover=Decimal(str(annual_turnover)),
            capital_allowances=capital_allowances,
        )

        return cit_calc

    def _compute_vat_data(
        self,
        user_id: int,
        start_date: date,
        end_date: date,
        basis: str,
    ) -> dict:
        """Compute VAT aggregation data for the period.

        VAT is opt-in: only computed for VAT-registered businesses.
        SuoOps calculates VAT from what the business charges — not what the law assumes.
        """
        from app.models.models import Invoice
        from app.models.tax_models import TaxProfile

        vat_data = {
            "taxable_sales": Decimal("0"),
            "zero_rated_sales": Decimal("0"),
            "exempt_sales": Decimal("0"),
            "vat_collected": Decimal("0"),
        }

        # VAT aggregation is opt-in: only for VAT-registered businesses
        tax_profile = self.db.query(TaxProfile).filter(TaxProfile.user_id == user_id).first()
        if not (tax_profile and tax_profile.vat_registered):
            return vat_data

        start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
        end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

        q = self.db.query(Invoice).filter(
            Invoice.issuer_id == user_id,
            Invoice.invoice_type == "revenue",
            Invoice.created_at >= start_dt,
            Invoice.created_at <= end_dt,
        )
        if basis == "paid":
            q = q.filter(Invoice.status == "paid")
        else:
            # Exclude both refunded AND cancelled invoices for "all" basis
            q = q.filter(Invoice.status.notin_(["refunded", "cancelled"]))
        invoices = q.all()

        for inv in invoices:
            amount = Decimal(str(inv.amount))
            if inv.discount_amount:
                amount -= Decimal(str(inv.discount_amount))
            cat = (inv.vat_category or "standard").lower()

            if cat in {"standard"}:
                # Use stored VAT or compute on-the-fly for legacy invoices
                vat_amt = Decimal(str(inv.vat_amount or 0))
                if not vat_amt:
                    from app.services.fiscalization_service import VATCalculator

                    vat_result = VATCalculator.calculate(amount, "standard")
                    vat_amt = vat_result["vat_amount"]
                # taxable_sales = VAT-exclusive base (net amount)
                vat_data["taxable_sales"] += amount - vat_amt
                vat_data["vat_collected"] += vat_amt
            elif cat in {"zero_rated", "export"}:
                vat_data["zero_rated_sales"] += amount
            elif cat in {"exempt"}:
                vat_data["exempt_sales"] += amount
            else:
                # Unknown category — treat as standard
                from app.services.fiscalization_service import VATCalculator

                vat_result = VATCalculator.calculate(amount, "standard")
                vat_data["taxable_sales"] += amount - vat_result["vat_amount"]
                vat_data["vat_collected"] += vat_result["vat_amount"]

        return vat_data

    def _create_report(
        self,
        user_id: int,
        period_type: str,
        start_date: date,
        end_date: date,
        year: int,
        month: Optional[int],
        profit: Decimal,
        levy: dict,
        pit_amount: Decimal,
        cit_amount: Decimal,
        vat_data: dict,
        cogs_data: dict,
    ) -> MonthlyTaxReport:
        """Create a new tax report."""
        report = MonthlyTaxReport(
            user_id=user_id,
            period_type=period_type,
            start_date=start_date,
            end_date=end_date,
            year=year,
            month=month,
            assessable_profit=profit,
            levy_amount=Decimal(str(levy["levy_amount"])),
            pit_amount=pit_amount,
            cit_amount=cit_amount,
            vat_collected=vat_data["vat_collected"],
            taxable_sales=vat_data["taxable_sales"],
            zero_rated_sales=vat_data["zero_rated_sales"],
            exempt_sales=vat_data["exempt_sales"],
            cogs_amount=cogs_data.get("cogs_amount", Decimal(0)),
            inventory_purchases=cogs_data.get("purchases_amount", Decimal(0)),
            inventory_value=cogs_data.get("current_inventory_value", Decimal(0)),
            pdf_url=None,
        )
        self.db.add(report)
        return report

    def _update_report(
        self,
        report: MonthlyTaxReport,
        profit: Decimal,
        levy: dict,
        pit_amount: Decimal,
        cit_amount: Decimal,
        vat_data: dict,
        cogs_data: dict,
    ) -> MonthlyTaxReport:
        """Update an existing tax report."""
        report.assessable_profit = profit
        report.levy_amount = Decimal(str(levy["levy_amount"]))
        report.pit_amount = pit_amount
        report.cit_amount = cit_amount
        report.vat_collected = vat_data["vat_collected"]
        report.taxable_sales = vat_data["taxable_sales"]
        report.zero_rated_sales = vat_data["zero_rated_sales"]
        report.exempt_sales = vat_data["exempt_sales"]
        report.cogs_amount = cogs_data.get("cogs_amount", Decimal(0))
        report.inventory_purchases = cogs_data.get("purchases_amount", Decimal(0))
        report.inventory_value = cogs_data.get("current_inventory_value", Decimal(0))
        # Clear old PDF so it gets regenerated with the latest template
        report.pdf_url = None
        return report

    def generate_monthly_report(
        self,
        user_id: int,
        year: int,
        month: int,
        basis: str = "paid",
        force_regenerate: bool = False,
    ) -> MonthlyTaxReport:
        """Backward-compatible wrapper for monthly reports."""
        return self.generate_report(
            user_id=user_id,
            period_type="month",
            year=year,
            month=month,
            basis=basis,
            force_regenerate=force_regenerate,
        )

    def attach_report_pdf(self, report: MonthlyTaxReport, pdf_url: str) -> MonthlyTaxReport:
        """Attach PDF URL to a tax report."""
        report.pdf_url = pdf_url
        self.db.commit()
        self.db.refresh(report)
        return report

    def compute_development_levy(self, user_id: int, assessable_profit: Decimal) -> dict:
        """Compute development levy for non-small businesses."""
        if assessable_profit < 0:
            raise ValueError("assessable_profit must be >= 0")
        profile = self.profile_service.get_or_create_profile(user_id)
        applies = not profile.is_small_business
        rate = Decimal("0.04") if applies else Decimal("0")
        amount = (assessable_profit * rate).quantize(Decimal("0.01"))
        return {
            "user_id": user_id,
            "business_size": profile.business_size,
            "is_small_business": profile.is_small_business,
            "assessable_profit": float(assessable_profit),
            "levy_rate_percent": float(rate * 100),
            "levy_applicable": applies,
            "levy_amount": float(amount),
            "exemption_reason": "small_business" if not applies else None,
        }

    def compute_assessable_profit(
        self,
        user_id: int,
        year: Optional[int] = None,
        month: Optional[int] = None,
        basis: str = "paid",
    ) -> Decimal:
        """Compute assessable profit (legacy method for backward compat)."""
        from app.models.models import Invoice

        q = self.db.query(Invoice).filter(Invoice.issuer_id == user_id, Invoice.invoice_type == "revenue")
        if basis == "paid":
            q = q.filter(Invoice.status == "paid")
        else:
            q = q.filter(Invoice.status != "refunded")

        # Apply due_date gating only for global computations
        if not (year and month):
            now = datetime.now(timezone.utc)
            q = q.filter((Invoice.due_date.is_(None)) | (Invoice.due_date <= now))
        if year and month:
            start = datetime(year, month, 1, tzinfo=timezone.utc)
            end = datetime(year + (month == 12), (month % 12) + 1, 1, tzinfo=timezone.utc)
            q = q.filter(Invoice.created_at >= start, Invoice.created_at < end)

        invoices = q.all()
        total = Decimal("0")
        for inv in invoices:
            amount = Decimal(str(inv.amount))
            if inv.discount_amount:
                amount -= Decimal(str(inv.discount_amount))
            total += amount
        return total

    def compute_assessable_profit_by_date_range(
        self,
        user_id: int,
        start_date: date,
        end_date: date,
        basis: str = "paid",
    ) -> Decimal:
        """Compute assessable profit (Revenue - Expenses) for a date range."""
        return compute_actual_profit_by_date_range(
            self.db,
            user_id,
            start_date,
            end_date,
            basis,
        )
