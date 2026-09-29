"""
Tax Profile Routes.

Handles tax profile management, small business checks, and compliance summaries.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.metrics import compliance_check_record, tax_profile_updated
from app.services.tax_service import TaxProfileService

from .schemas import (
    ComplianceSummaryOut,
    SmallBusinessCheckOut,
    TaxConfigOut,
    TaxProfileUpdate,
    TaxProfileUpdateOut,
    TaxSummaryOut,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/profile", response_model=TaxSummaryOut)
def get_tax_profile(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Return comprehensive tax profile summary including classification and benefits."""
    try:
        tax_service = TaxProfileService(db)
        return tax_service.get_tax_summary(current_user_id)
    except Exception as e:
        logger.exception("Failed to fetch tax profile summary")
        raise HTTPException(status_code=500, detail="Failed to fetch tax profile") from e


@router.post("/profile", response_model=TaxProfileUpdateOut)
def update_tax_profile(
    data: TaxProfileUpdate,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Update tax profile and return updated classification & rates."""
    try:
        tax_service = TaxProfileService(db)
        tax_service.update_profile(
            user_id=current_user_id,
            annual_turnover=data.annual_turnover,
            fixed_assets=data.fixed_assets,
            tin=data.tin,
            vat_registration_number=data.vat_registration_number,
            vat_registered=data.vat_registered,
            business_type=data.business_type,
            vat_apply_to=data.vat_apply_to,
            withholding_vat_applies=data.withholding_vat_applies,
        )
        tax_profile_updated()
        return {
            "message": "Tax profile updated successfully",
            "summary": tax_service.get_tax_summary(current_user_id),
        }
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:
        logger.exception("Failed to update tax profile")
        raise HTTPException(status_code=500, detail="Failed to update tax profile") from e


@router.get("/small-business-check", response_model=SmallBusinessCheckOut)
def small_business_check(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Return small business eligibility, remaining thresholds and benefits."""
    try:
        tax_service = TaxProfileService(db)
        return tax_service.check_small_business_eligibility(current_user_id)
    except Exception as e:
        logger.exception("Failed small business eligibility check")
        raise HTTPException(status_code=500, detail="Failed small business check") from e


@router.get("/compliance", response_model=ComplianceSummaryOut)
def tax_compliance(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Return tax compliance summary (TIN/VAT/NRS registration status & next actions)."""
    try:
        tax_service = TaxProfileService(db)
        summary = tax_service.get_compliance_summary(current_user_id)
        tax_service.update_compliance_check(current_user_id)
        compliance_check_record()
        return summary
    except Exception as e:
        logger.exception("Failed tax compliance summary")
        raise HTTPException(status_code=500, detail="Failed compliance summary") from e


@router.get("/config", response_model=TaxConfigOut)
def tax_config(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Expose tax constants for frontend (thresholds, rates)."""
    try:
        service = TaxProfileService(db)
        return service.get_tax_constants()
    except Exception as e:
        logger.exception("Failed to fetch tax config")
        raise HTTPException(status_code=500, detail="Failed tax config") from e
