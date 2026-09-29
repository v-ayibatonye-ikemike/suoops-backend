"""
VAT Routes.

Handles VAT summary, calculation, and return generation.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.metrics import vat_calculation_record
from app.services.fiscalization_service import VATCalculator
from app.services.vat_service import VATService

from .schemas import VATCalculateOut, VATReturnOut, VATSummaryOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/vat/summary", response_model=VATSummaryOut)
def get_vat_summary(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Get VAT summary and compliance status.

    Returns current month VAT calculations, recent VAT returns,
    compliance status, and next recommended action.
    """
    try:
        vat_service = VATService(db)
        summary = vat_service.get_vat_summary(current_user_id)
        vat_calculation_record()
        return summary
    except Exception as e:
        logger.error(f"Error fetching VAT summary: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to fetch VAT summary")


@router.get("/vat/calculate", response_model=VATCalculateOut)
def calculate_vat(
    amount: float = Query(..., gt=0, description="Amount in Naira"),
    category: str = Query(
        "standard",
        description="VAT category: standard, zero_rated, exempt, export",
    ),
):
    """
    Calculate VAT for an amount.

    Categories:
    - standard: 7.5% VAT
    - zero_rated: 0% VAT (medical, education, basic food)
    - exempt: No VAT (financial services)
    - export: 0% VAT (exports)
    """
    try:
        result = VATCalculator.calculate(Decimal(str(amount)), category)
        vat_calculation_record()
        return {
            "subtotal": float(result["subtotal"]),
            "vat_rate": float(result["vat_rate"]),
            "vat_amount": float(result["vat_amount"]),
            "total": float(result["total"]),
            "category": category,
        }
    except Exception as e:
        logger.error(f"Error calculating VAT: {str(e)}")
        raise HTTPException(status_code=400, detail="Invalid VAT calculation parameters")


@router.post("/vat/return", response_model=VATReturnOut)
def generate_vat_return(
    year: int = Query(..., ge=2024, le=2030, description="Year"),
    month: int = Query(..., ge=1, le=12, description="Month (1-12)"),
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Generate VAT return for a specific month.

    Calculates output VAT, input VAT, net VAT payable,
    zero-rated and exempt sales.
    """
    try:
        vat_service = VATService(db)
        vat_return = vat_service.generate_vat_return(current_user_id, year, month)

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
    except Exception as e:
        logger.error(f"Error generating VAT return: {str(e)}")
        raise HTTPException(status_code=500, detail="Failed to generate VAT return")
