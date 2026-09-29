"""Miscellaneous tax endpoints: config, levy, fiscalization status, alerts, invoice fiscalize."""

from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes_admin_auth import get_current_admin
from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models.models import Invoice
from app.services.fiscalization_service import FiscalizationService
from app.services.tax_reporting_service import TaxReportingService
from app.services.tax_service import TaxProfileService

router = APIRouter(prefix="/tax", tags=["tax-misc"])


class FiscalizationStatus(BaseModel):
    accredited: bool
    generated_count: int
    pending_external_count: int
    timestamp: str


@router.get("/config")
def tax_config(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    try:
        service = TaxProfileService(db)
        return service.get_tax_constants()
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed tax config") from e


@router.get("/fiscalization/status", response_model=FiscalizationStatus)
def get_fiscalization_status(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    try:
        from app.core.config import settings
        from app.models.tax_models import FiscalInvoice

        accredited = bool(getattr(settings, "FISCALIZATION_ACCREDITED", False))
        generated_count = db.query(FiscalInvoice).join(Invoice).filter(Invoice.issuer_id == current_user_id).count()
        pending_external_count = (
            db.query(FiscalInvoice)
            .join(Invoice)
            .filter(
                Invoice.issuer_id == current_user_id,
                ~FiscalInvoice.transmitted_at.isnot(None),
            )
            .count()
        )
        return FiscalizationStatus(
            accredited=accredited,
            generated_count=generated_count,
            pending_external_count=pending_external_count if accredited else generated_count,
            timestamp=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed fiscalization status") from e


@router.get("/levy")
def development_levy(
    profit: float | None = Query(None, ge=0),
    year: int | None = Query(None, ge=2024, le=2030),
    month: int | None = Query(None, ge=1, le=12),
    basis: str = Query("paid", pattern="^(paid|all)$"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    try:
        reporting = TaxReportingService(db)
        computed_profit = None
        source = "override"
        if profit is None:
            computed_profit = reporting.compute_assessable_profit(current_user_id, year=year, month=month, basis=basis)
            profit_to_use = computed_profit
            source = f"{basis}_invoices"
        else:
            profit_to_use = Decimal(str(profit))
        result = reporting.compute_development_levy(current_user_id, profit_to_use)
        period = f"{year:04d}-{month:02d}" if year and month else None
        result["period"] = period
        result["source"] = source
        return result
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed levy calculation") from e


class AlertEventOut(BaseModel):
    id: int
    category: str
    severity: str
    message: str
    created_at: str | None
    model_config = dict(from_attributes=True)


@router.get("/admin/alerts", response_model=list[AlertEventOut])
def list_recent_alerts(
    limit: int = Query(50, ge=1, le=200),
    category: str | None = Query(None),
    db: Session = Depends(get_db),
    admin_user=Depends(get_current_admin),
):
    try:
        from app.models.alert_models import AlertEvent  # type: ignore
    except Exception:
        raise HTTPException(status_code=500, detail="Alert model unavailable")
    q = db.query(AlertEvent).order_by(AlertEvent.created_at.desc())
    if category:
        q = q.filter(AlertEvent.category == category)
    records = q.limit(limit).all()
    return [
        AlertEventOut(
            id=r.id,
            category=r.category,
            severity=r.severity,
            message=r.message,
            created_at=r.created_at.isoformat() if r.created_at else None,
        )
        for r in records
    ]


@router.post("/invoice/{invoice_id}/fiscalize")
async def fiscalize_invoice(
    invoice_id: int,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    try:
        invoice = db.query(Invoice).filter(Invoice.id == invoice_id, Invoice.issuer_id == current_user_id).first()
        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")
        if invoice.is_fiscalized:
            return {
                "message": "Invoice already fiscalized",
                "fiscal_code": invoice.fiscal_code,
                "status": "already_fiscalized",
            }
        service = FiscalizationService(db)
        fiscal_data = await service.fiscalize_invoice(invoice_id)
        return {
            "message": "Invoice fiscalized successfully",
            "fiscal_code": fiscal_data.fiscal_code,
            "fiscal_signature": fiscal_data.fiscal_signature[:32] + "...",
            "qr_code": fiscal_data.qr_code_data[:100] + "...",
            "vat_breakdown": {
                "subtotal": float(fiscal_data.subtotal),
                "vat_rate": fiscal_data.vat_rate,
                "vat_amount": float(fiscal_data.vat_amount),
                "total": float(fiscal_data.total_amount),
            },
            "fiscalization_status": fiscal_data.firs_validation_status,
            "fiscalization_transaction_id": fiscal_data.firs_transaction_id,
        }
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed to fiscalize invoice") from e
