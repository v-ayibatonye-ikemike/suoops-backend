"""VAT endpoints split from routes_tax.py."""

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.services.fiscalization_service import VATCalculator
from app.services.vat_service import VATService

router = APIRouter(prefix="/tax", tags=["tax-vat"])


@router.get("/vat/summary")
def get_vat_summary(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    try:
        service = VATService(db)
        return service.get_vat_summary(current_user_id)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed to fetch VAT summary") from e


@router.get("/vat/calculate")
def calculate_vat(
    amount: float = Query(..., gt=0),
    category: str = Query("standard"),
):
    try:
        result = VATCalculator.calculate(Decimal(str(amount)), category)
        return {
            "subtotal": float(result["subtotal"]),
            "vat_rate": float(result["vat_rate"]),
            "vat_amount": float(result["vat_amount"]),
            "total": float(result["total"]),
            "category": category,
        }
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=400, detail="Invalid VAT calculation parameters") from e


@router.post("/vat/return")
def generate_vat_return(
    year: int = Query(..., ge=2024, le=2030),
    month: int = Query(..., ge=1, le=12),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    try:
        service = VATService(db)
        vat_return = service.generate_vat_return(current_user_id, year, month)
        return {
            "tax_period": vat_return.tax_period,
            "output_vat": float(vat_return.output_vat),
            "input_vat": float(vat_return.input_vat),
            "net_vat": float(vat_return.net_vat),
            "zero_rated_sales": float(vat_return.zero_rated_sales),
            "exempt_sales": float(vat_return.exempt_sales),
            "total_invoices": vat_return.total_invoices,
            "fiscalized_invoices": vat_return.fiscalized_invoices,
            "status": vat_return.status,
            "message": "VAT return generated successfully",
        }
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed to generate VAT return") from e
