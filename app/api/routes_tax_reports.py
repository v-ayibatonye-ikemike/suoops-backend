"""Monthly tax report & CSV/PDF endpoints."""

from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models.tax_models import MonthlyTaxReport
from app.services.tax_reporting_service import TaxReportingService
from app.storage.s3_client import s3_client

router = APIRouter(prefix="/tax", tags=["tax-reports"])


@router.post("/reports/generate", response_model=dict)
def generate_monthly_tax_report(
    year: int = Query(..., ge=2020, le=2100),
    month: int = Query(..., ge=1, le=12),
    basis: str = Query("paid", pattern="^(paid|all)$"),
    force: bool = Query(False),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    # Use reporting service directly (profile service wrappers retained for backward compat)
    report = TaxReportingService(db).generate_monthly_report(
        current_user_id,
        year,
        month,
        basis=basis,
        force_regenerate=force,
    )
    return {
        "year": report.year,
        "month": report.month,
        "assessable_profit": float(report.assessable_profit or 0),
        "levy_amount": float(report.levy_amount or 0),
        "vat_collected": float(report.vat_collected or 0),
        "taxable_sales": float(report.taxable_sales or 0),
        "zero_rated_sales": float(report.zero_rated_sales or 0),
        "exempt_sales": float(report.exempt_sales or 0),
        "pdf_url": report.pdf_url,
        "basis": basis,
    }


@router.get("/reports/{year}/{month}/download")
def download_monthly_tax_report(
    year: int,
    month: int,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    report = (
        db.query(MonthlyTaxReport)
        .filter(
            MonthlyTaxReport.user_id == current_user_id,
            MonthlyTaxReport.year == year,
            MonthlyTaxReport.month == month,
        )
        .first()
    )
    if not report or not report.pdf_url:
        raise HTTPException(status_code=404, detail="Report or PDF not found. Generate first.")
    return {"pdf_url": report.pdf_url}


@router.get("/reports/{year}/{month}/csv")
def download_monthly_tax_report_csv(
    year: int,
    month: int,
    basis: str = Query("paid", pattern="^(paid|all)$"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    report = (
        db.query(MonthlyTaxReport)
        .filter(
            MonthlyTaxReport.user_id == current_user_id,
            MonthlyTaxReport.year == year,
            MonthlyTaxReport.month == month,
        )
        .first()
    )
    if not report:
        raise HTTPException(status_code=404, detail="Report not found. Generate first.")
    refreshed = TaxReportingService(db).generate_monthly_report(
        current_user_id,
        year,
        month,
        basis=basis,
        force_regenerate=True,
    )
    buf = StringIO()
    headers = [
        "year",
        "month",
        "basis",
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
        str(refreshed.year),
        f"{refreshed.month:02d}",
        basis,
        f"{float(refreshed.assessable_profit or 0):.2f}",
        f"{float(refreshed.levy_amount or 0):.2f}",
        f"{float(refreshed.vat_collected or 0):.2f}",
        f"{float(refreshed.taxable_sales or 0):.2f}",
        f"{float(refreshed.zero_rated_sales or 0):.2f}",
        f"{float(refreshed.exempt_sales or 0):.2f}",
        (refreshed.generated_at.isoformat() if refreshed.generated_at else ""),
    ]
    buf.write(",".join(row) + "\n")
    data = buf.getvalue().encode("utf-8")
    key = f"tax-reports/{current_user_id}/{year}-{month:02d}-{basis}.csv"
    url = s3_client.upload_bytes(data, key, content_type="text/csv")
    return {"csv_url": url, "basis": basis}
