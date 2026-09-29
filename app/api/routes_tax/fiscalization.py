"""
Fiscalization and Development Levy Routes.

Handles fiscalization status, invoice fiscalization, and development levy calculation.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models.models import Invoice
from app.services.fiscalization_service import FiscalizationService
from app.services.tax_service import TaxProfileService

from .schemas import DevelopmentLevyResponse, FiscalizationStatus, FiscalizeInvoiceOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/fiscalization/status", response_model=FiscalizationStatus)
def get_fiscalization_status(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Return provisional fiscalization readiness metrics."""
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
    except Exception as e:
        logger.exception("Failed fiscalization status")
        raise HTTPException(status_code=500, detail="Failed fiscalization status") from e


@router.get("/levy", response_model=DevelopmentLevyResponse)
def development_levy(
    profit: float | None = Query(None, ge=0, description="Override assessable profit base"),
    year: int | None = Query(None, ge=2024, le=2030, description="Year for computation"),
    month: int | None = Query(None, ge=1, le=12, description="Month for computation"),
    basis: str = Query("paid", pattern="^(paid|all)$", description="Profit basis"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Compute development levy (4% for non-small businesses)."""
    try:
        tax_service = TaxProfileService(db)
        source = "override"

        if profit is None:
            computed_profit = tax_service.compute_assessable_profit(
                current_user_id,
                year=year,
                month=month,
                basis=basis,
            )
            profit_to_use = computed_profit
            source = f"{basis}_invoices"
        else:
            profit_to_use = Decimal(str(profit))

        result = tax_service.compute_development_levy(current_user_id, profit_to_use)
        period = f"{year:04d}-{month:02d}" if year and month else None
        result["period"] = period
        result["source"] = source
        return DevelopmentLevyResponse(**result)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Failed development levy calculation")
        raise HTTPException(status_code=500, detail="Failed levy calculation") from e


@router.post("/invoice/{invoice_id}/fiscalize", response_model=FiscalizeInvoiceOut)
async def fiscalize_invoice(
    invoice_id: int,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Fiscalize an invoice (provisional FIRS readiness)."""
    try:
        invoice = (
            db.query(Invoice)
            .filter(
                Invoice.id == invoice_id,
                Invoice.issuer_id == current_user_id,
            )
            .first()
        )

        if not invoice:
            raise HTTPException(status_code=404, detail="Invoice not found")

        if invoice.is_fiscalized:
            return {
                "message": "Invoice already fiscalized",
                "fiscal_code": invoice.fiscal_code,
                "status": "already_fiscalized",
            }

        fiscal_service = FiscalizationService(db)
        fiscal_data = await fiscal_service.fiscalize_invoice(invoice_id)

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
    except Exception as e:
        logger.error(f"Error fiscalizing invoice: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fiscalize invoice")
