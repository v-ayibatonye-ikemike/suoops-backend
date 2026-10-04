"""
Tax Reports Routes.

Handles tax report generation, downloads, and CSV exports.
Requires STARTER or PRO plan for access.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from io import StringIO
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import models
from app.models.tax_models import MonthlyTaxReport
from app.services.pdf_service import PDFService
from app.services.tax_reporting_service import TaxReportingService
from app.utils.feature_gate import require_plan_feature

from .schemas import ReportCsvOut, ReportDownloadOut, TaxReportOut

logger = logging.getLogger(__name__)
router = APIRouter()


def _fresh_pdf_url(stored_url: str | None) -> str | None:
    """Return a freshly-signed download URL for a stored tax-report PDF.

    ``report.pdf_url`` is a SHORT-LIVED presigned S3 URL (``S3_PRESIGN_TTL``, ~1h).
    Returning the stored value directly yields "AccessDenied / Request has expired"
    once it goes stale — which broke both the emailed link and the dashboard
    Download button. Re-sign from the object key on every download so the link is
    always valid when the user actually clicks it. Falls back to the stored value
    when S3 isn't configured (local dev) or the key can't be parsed.
    """
    if not stored_url:
        return stored_url
    from app.storage.s3_client import s3_client

    key = s3_client.extract_key_from_url(stored_url)
    if not key:
        return stored_url
    return s3_client.get_presigned_url(key) or stored_url


@router.post("/reports/generate", response_model=TaxReportOut)
def generate_tax_report(
    period_type: Literal["day", "week", "month", "year"] = Query(
        "month",
        description="Period type: day, week, month, or year",
    ),
    year: int = Query(..., ge=2020, le=2100, description="Year"),
    month: int | None = Query(None, ge=1, le=12, description="Month"),
    day: int | None = Query(None, ge=1, le=31, description="Day"),
    week: int | None = Query(None, ge=1, le=53, description="ISO week number"),
    basis: Literal["paid", "all"] = Query("paid", description="Basis"),
    force: bool = Query(False, description="Force regeneration"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Generate tax report for specified period. Requires STARTER or PRO plan."""
    # Gate: Require tax_reports feature (STARTER+)
    require_plan_feature(db, current_user_id, "tax_reports", "Tax Reports")

    try:
        reporting_service = TaxReportingService(db)
        report = reporting_service.generate_report(
            user_id=current_user_id,
            period_type=period_type,
            year=year,
            month=month,
            day=day,
            week=week,
            basis=basis,
            force_regenerate=force,
        )

        # Compute revenue & expenses separately so the frontend and PDF can display them
        from app.services.tax_reporting.computations import (
            compute_expense_evidence_breakdown,
            compute_expenses_by_date_range,
            compute_personal_income_tax,
            compute_revenue_by_date_range,
        )

        total_revenue = float(
            compute_revenue_by_date_range(db, current_user_id, report.start_date, report.end_date, basis)
        )
        total_expenses = float(compute_expenses_by_date_range(db, current_user_id, report.start_date, report.end_date))
        expense_breakdown = compute_expense_evidence_breakdown(db, current_user_id, report.start_date, report.end_date)
        cogs_amount = float(report.cogs_amount or 0)

        # Always use fresh-computed profit for consistency with revenue/expenses
        # This prevents stale cached assessable_profit from disagreeing with
        # freshly computed revenue/expenses.
        fresh_profit = max(total_revenue - total_expenses - cogs_amount, 0.0)
        fresh_pit = compute_personal_income_tax(Decimal(str(fresh_profit)))
        fresh_pit_amount = float(fresh_pit["pit_amount"])

        # Always generate PDF on-demand if not present
        # (ensures PDF is available for download button)
        if not report.pdf_url:
            try:
                from app.storage.s3_client import S3Client

                pdf_service = PDFService(S3Client())
                pdf_url = pdf_service.generate_monthly_tax_report_pdf(
                    report,
                    basis=basis,
                    total_revenue=total_revenue,
                    total_expenses=total_expenses,
                )
                reporting_service.attach_report_pdf(report, pdf_url)
                report.pdf_url = pdf_url
                db.commit()  # Persist the pdf_url
                logger.info(f"Generated PDF for tax report {report.id}: {pdf_url}")
            except Exception as pdf_err:
                logger.warning(f"Failed to generate PDF for tax report {report.id}: {pdf_err}")
                # Continue without PDF - report is still valid

        period_label = _format_period_label(period_type, year, month, day, week)
        user = db.query(models.User).filter(models.User.id == current_user_id).first()
        user_plan = user.plan.value if user else "free"

        annual_revenue_estimate = _estimate_annual_revenue(
            total_revenue,
            period_type,
        )
        alerts = _generate_tax_alerts(user_plan, annual_revenue_estimate)
        pit_band_info = _get_pit_band_info(fresh_profit)
        is_cit_eligible = user_plan in ("pro", "business")

        # Debug: Get invoice counts for troubleshooting
        invoice_debug = _get_invoice_debug_info(db, current_user_id, report.start_date, report.end_date, basis)
        # Override calculated_revenue with the same value used for the cards
        # so debug_info is always consistent with the displayed revenue.
        invoice_debug["calculated_revenue"] = total_revenue

        return {
            "id": report.id,
            "period_type": report.period_type,
            "period_label": period_label,
            "start_date": report.start_date.isoformat() if report.start_date else None,
            "end_date": report.end_date.isoformat() if report.end_date else None,
            "year": report.year,
            "month": report.month,
            "total_revenue": total_revenue,
            "total_expenses": total_expenses,
            "documented_expenses": float(expense_breakdown["documented"]),
            "self_reported_expenses": float(expense_breakdown["self_reported"]),
            "flagged_expenses": float(expense_breakdown["flagged"]),
            "cogs_amount": cogs_amount,
            "assessable_profit": fresh_profit,
            "levy_amount": float(report.levy_amount or 0),
            "pit_amount": fresh_pit_amount,
            "cit_amount": float(report.cit_amount or 0) if is_cit_eligible else 0,
            "vat_collected": float(report.vat_collected or 0),
            "taxable_sales": float(report.taxable_sales or 0),
            "zero_rated_sales": float(report.zero_rated_sales or 0),
            "exempt_sales": float(report.exempt_sales or 0),
            "pdf_url": report.pdf_url,
            "basis": basis,
            "user_plan": user_plan,
            "is_vat_eligible": user_plan in ("pro", "business"),
            "is_cit_eligible": is_cit_eligible,
            "pit_band_info": pit_band_info,
            "alerts": alerts,
            "annual_revenue_estimate": annual_revenue_estimate,
            # Debug info for troubleshooting
            "debug_info": invoice_debug,
        }
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed to generate tax report")
        raise HTTPException(status_code=500, detail=f"Failed to generate tax report: {str(e)}")


@router.get("/reports/{report_id}/download", response_model=ReportDownloadOut)
def download_tax_report_by_id(
    report_id: int,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Download tax report PDF by report ID."""
    report = (
        db.query(MonthlyTaxReport)
        .filter(
            MonthlyTaxReport.id == report_id,
            MonthlyTaxReport.user_id == current_user_id,
        )
        .first()
    )

    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")

    # Generate PDF on-demand if not already present
    if not report.pdf_url:
        try:
            from app.storage.s3_client import S3Client

            pdf_service = PDFService(S3Client())
            pdf_url = pdf_service.generate_monthly_tax_report_pdf(report, basis="paid")
            report.pdf_url = pdf_url
            db.commit()
            logger.info(f"Generated PDF on download for report {report.id}: {pdf_url}")
        except Exception as e:
            logger.error(f"Failed to generate PDF for report {report.id}: {e}")
            raise HTTPException(status_code=500, detail="Failed to generate PDF.")

    return {
        "pdf_url": _fresh_pdf_url(report.pdf_url),
        "period_type": report.period_type,
        "start_date": report.start_date.isoformat() if report.start_date else None,
        "end_date": report.end_date.isoformat() if report.end_date else None,
    }


@router.get("/reports/{year}/{month}/download", response_model=ReportDownloadOut)
def download_monthly_tax_report(
    year: int,
    month: int,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Download monthly tax report PDF (backward compatible endpoint)."""
    report = (
        db.query(MonthlyTaxReport)
        .filter(
            MonthlyTaxReport.user_id == current_user_id,
            MonthlyTaxReport.period_type == "month",
            MonthlyTaxReport.year == year,
            MonthlyTaxReport.month == month,
        )
        .first()
    )

    if not report or not report.pdf_url:
        raise HTTPException(status_code=404, detail="Report or PDF not found.")

    return {"pdf_url": _fresh_pdf_url(report.pdf_url)}


@router.get("/reports/{report_id}/csv", response_model=ReportCsvOut)
def download_tax_report_csv_by_id(
    report_id: int,
    basis: str = Query("paid", pattern="^(paid|all)$"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Generate CSV export for a tax report by ID."""
    from app.storage.s3_client import s3_client

    report = (
        db.query(MonthlyTaxReport)
        .filter(
            MonthlyTaxReport.id == report_id,
            MonthlyTaxReport.user_id == current_user_id,
        )
        .first()
    )

    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")

    service = TaxReportingService(db)
    refreshed = service.generate_report(
        user_id=current_user_id,
        period_type=report.period_type,
        year=report.year,
        month=report.month,
        day=report.start_date.day if report.period_type == "day" and report.start_date else None,
        week=report.start_date.isocalendar()[1] if report.period_type == "week" and report.start_date else None,
        basis=basis,
        force_regenerate=True,
    )

    csv_data = _generate_csv_content(refreshed, basis)
    filename = _generate_csv_filename(report, basis)
    key = f"tax-reports/{current_user_id}/{filename}"
    url = s3_client.upload_bytes(csv_data, key, content_type="text/csv")

    return {"csv_url": url, "basis": basis}


@router.get("/reports/{year}/{month}/csv", response_model=ReportCsvOut)
def download_monthly_tax_report_csv(
    year: int,
    month: int,
    basis: str = Query("paid", pattern="^(paid|all)$"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Generate CSV export for monthly tax report (backward compatible)."""
    report = (
        db.query(MonthlyTaxReport)
        .filter(
            MonthlyTaxReport.user_id == current_user_id,
            MonthlyTaxReport.period_type == "month",
            MonthlyTaxReport.year == year,
            MonthlyTaxReport.month == month,
        )
        .first()
    )

    if not report:
        raise HTTPException(status_code=404, detail="Report not found.")

    return download_tax_report_csv_by_id(report.id, basis, current_user_id, db)


# ============================================================================
# Private Helper Functions
# ============================================================================


def _format_period_label(period_type: str, year: int, month: int | None, day: int | None, week: int | None) -> str:
    """Format period label for response."""
    if period_type == "day":
        return f"{year}-{month:02d}-{day:02d}"
    elif period_type == "week":
        return f"{year}-W{week:02d}"
    elif period_type == "month":
        return f"{year}-{month:02d}"
    return str(year)


def _estimate_annual_revenue(period_revenue: float, period_type: str) -> float:
    """Estimate annual revenue from period revenue."""
    multipliers = {"day": 365, "week": 52, "month": 12, "year": 1}
    return period_revenue * multipliers.get(period_type, 12)


def _generate_tax_alerts(user_plan: str, annual_revenue: float) -> list[dict]:
    """Generate tax alerts independently of legacy plan values."""
    _ = user_plan
    alerts = []

    if annual_revenue >= 25_000_000:
        alerts.append(
            {
                "type": "vat_threshold",
                "severity": "warning",
                "message": (
                    f"Your estimated annual turnover (₦{annual_revenue:,.0f}) exceeds ₦25M. "
                    "VAT registration required."
                ),
            }
        )
    elif annual_revenue >= 20_000_000:
        alerts.append(
            {
                "type": "vat_approaching",
                "severity": "info",
                "message": f"You're approaching the ₦25M VAT threshold (current: ₦{annual_revenue:,.0f}).",
            }
        )

    if annual_revenue >= 50_000_000:
        alerts.append(
            {
                "type": "cit_threshold",
                "severity": "warning",
                "message": (
                    f"Your turnover (₦{annual_revenue:,.0f}) exceeds ₦50M. "
                    "Review your company income tax obligations."
                ),
            }
        )

    return alerts


def _get_invoice_debug_info(
    db: Session,
    user_id: int,
    start_date,
    end_date,
    basis: str,
) -> dict:
    """Get invoice counts and totals for debugging."""
    from datetime import datetime, timezone

    from app.models.models import Invoice

    # Convert dates
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

    # Query revenue invoices
    base_query = db.query(Invoice).filter(
        Invoice.issuer_id == user_id,
        Invoice.invoice_type == "revenue",
        Invoice.created_at >= start_dt,
        Invoice.created_at <= end_dt,
    )

    # Get counts by status
    all_invoices = base_query.all()
    paid_invoices = [i for i in all_invoices if i.status == "paid"]

    # Get top 5 invoices by amount
    top_invoices = sorted(all_invoices, key=lambda i: float(i.amount or 0), reverse=True)[:5]

    # Calculate basis-specific totals (matching the actual revenue calculation logic)
    if basis == "paid":
        relevant_invoices = paid_invoices
    else:
        # Exclude both refunded AND cancelled invoices for "all" basis
        relevant_invoices = [i for i in all_invoices if i.status not in ("refunded", "cancelled")]

    total_revenue = sum(float(i.amount or 0) - float(i.discount_amount or 0) for i in relevant_invoices)

    return {
        "total_invoices_in_period": len(all_invoices),
        "paid_invoices": len(paid_invoices),
        "non_refunded_invoices": len([i for i in all_invoices if i.status not in ("refunded", "cancelled")]),
        "invoices_counted_for_basis": len(relevant_invoices),
        "calculated_revenue": total_revenue,
        "top_5_invoices": [
            {
                "invoice_id": i.invoice_id,
                "amount": float(i.amount or 0),
                "status": i.status,
                "created_at": i.created_at.isoformat() if i.created_at else None,
            }
            for i in top_invoices
        ],
    }


def _get_pit_band_info(profit: float) -> str:
    """Get PIT band information based on profit."""
    if profit <= 800_000:
        return "0% band (profit ≤₦800K)"
    elif profit <= 3_000_000:
        return "15% band (₦800K-₦3M)"
    elif profit <= 12_000_000:
        return "18% band (₦3M-₦12M)"
    elif profit <= 25_000_000:
        return "21% band (₦12M-₦25M)"
    elif profit <= 50_000_000:
        return "23% band (₦25M-₦50M)"
    return "25% band (>₦50M)"


def _generate_csv_content(report: MonthlyTaxReport, basis: str) -> bytes:
    """Generate CSV content from report."""
    buf = StringIO()
    headers = [
        "period_type",
        "start_date",
        "end_date",
        "year",
        "month",
        "basis",
        "total_revenue",
        "total_expenses",
        "cogs_amount",
        "assessable_profit",
        "levy_amount",
        "vat_collected",
        "taxable_sales",
        "zero_rated_sales",
        "exempt_sales",
        "generated_at",
    ]
    buf.write(",".join(headers) + "\n")

    row = [
        report.period_type,
        report.start_date.isoformat() if report.start_date else "",
        report.end_date.isoformat() if report.end_date else "",
        str(report.year) if report.year else "",
        f"{report.month:02d}" if report.month else "",
        basis,
        "",  # total_revenue — computed on-the-fly, not stored on report
        "",  # total_expenses — computed on-the-fly, not stored on report
        f"{float(report.cogs_amount or 0):.2f}",
        f"{float(report.assessable_profit or 0):.2f}",
        f"{float(report.levy_amount or 0):.2f}",
        f"{float(report.vat_collected or 0):.2f}",
        f"{float(report.taxable_sales or 0):.2f}",
        f"{float(report.zero_rated_sales or 0):.2f}",
        f"{float(report.exempt_sales or 0):.2f}",
        report.generated_at.isoformat() if report.generated_at else "",
    ]
    buf.write(",".join(row) + "\n")

    return buf.getvalue().encode("utf-8")


def _generate_csv_filename(report: MonthlyTaxReport, basis: str) -> str:
    """Generate CSV filename based on period type."""
    if report.period_type == "day":
        return f"{report.start_date.isoformat()}-{basis}.csv"
    elif report.period_type == "week":
        week_num = report.start_date.isocalendar()[1]
        return f"{report.year}-W{week_num:02d}-{basis}.csv"
    elif report.period_type == "month":
        return f"{report.year}-{report.month:02d}-{basis}.csv"
    return f"{report.year}-{basis}.csv"
