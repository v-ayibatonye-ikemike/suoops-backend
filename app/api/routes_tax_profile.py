"""Tax profile & small business endpoints split from routes_tax.py for modularity.
Tax features require PRO plan.
"""
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.services.mono_lookup_service import (
    LOOKUP_COST_KOBO,
    verify_business_cac,
    verify_business_tin,
)
from app.services.tax_service import TaxProfileService
from app.utils.feature_gate import require_plan_feature

router = APIRouter(prefix="/tax", tags=["tax-profile"])


class TaxProfileUpdate(BaseModel):
    annual_turnover: Optional[Decimal] = Field(None, ge=0)
    fixed_assets: Optional[Decimal] = Field(None, ge=0)
    tin: Optional[str] = Field(None, max_length=20)
    vat_registration_number: Optional[str] = Field(None, max_length=20)
    vat_registered: Optional[bool] = None
    business_type: Optional[str] = Field(None, pattern="^(goods|services|mixed)$")
    vat_apply_to: Optional[str] = Field(None, pattern="^(all|selected)$")
    withholding_vat_applies: Optional[bool] = None


class CACVerifyIn(BaseModel):
    rc_number: str = Field(..., min_length=2, max_length=20, description="CAC/RC registration number")


class VerificationResultOut(BaseModel):
    verified: bool
    verification_status: str
    charged_kobo: int
    registered_name: Optional[str] = None
    message: str


@router.get("/profile")
def get_tax_profile(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Get user's tax profile. Requires PRO plan."""
    require_plan_feature(db, current_user_id, "tax_reports", "Tax Reports")
    try:
        service = TaxProfileService(db)
        return service.get_tax_summary(current_user_id)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed to fetch tax profile") from e


@router.post("/profile")
def update_tax_profile(
    data: TaxProfileUpdate,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Update user's tax profile. Requires PRO plan."""
    require_plan_feature(db, current_user_id, "tax_reports", "Tax Reports")
    try:
        service = TaxProfileService(db)
        service.update_profile(
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
        return {"message": "Tax profile updated successfully", "summary": service.get_tax_summary(current_user_id)}
    except ValueError as ve:
        raise HTTPException(status_code=422, detail=str(ve))
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed to update tax profile") from e


@router.get("/small-business-check")
def small_business_check(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Check small business eligibility. Requires PRO plan."""
    require_plan_feature(db, current_user_id, "tax_reports", "Tax Reports")
    try:
        service = TaxProfileService(db)
        return service.check_small_business_eligibility(current_user_id)
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed small business check") from e


@router.get("/compliance")
def tax_compliance(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Get tax compliance summary. Requires PRO plan."""
    require_plan_feature(db, current_user_id, "tax_reports", "Tax Reports")
    try:
        service = TaxProfileService(db)
        summary = service.get_compliance_summary(current_user_id)
        service.update_compliance_check(current_user_id)
        return summary
    except Exception as e:  # pragma: no cover
        raise HTTPException(status_code=500, detail="Failed compliance summary") from e


@router.post("/profile/verify-tin", response_model=VerificationResultOut)
def verify_tin(
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Verify the business's TIN via Mono Lookup, charged from their own
    wallet — not gated by plan, since it's a metered, opt-in, self-funded
    action rather than a subscription feature. Idempotent: an
    already-verified TIN returns immediately at no charge.
    SuoOpsException subclasses (invalid TIN, insufficient wallet balance,
    Mono not configured/unavailable) are handled by the global exception
    handler registered in app.api.main.
    """
    try:
        profile = verify_business_tin(db, current_user_id)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    return VerificationResultOut(
        verified=profile.tin_verified,
        verification_status=profile.verification_status,
        charged_kobo=LOOKUP_COST_KOBO["tin"] if profile.tin_verified else 0,
        message="TIN verified via Mono." if profile.tin_verified else "TIN verification pending.",
    )


@router.post("/profile/verify-cac", response_model=VerificationResultOut)
def verify_cac(
    data: CACVerifyIn,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """Verify the business's CAC/RC registration via Mono Lookup, charged
    from their own wallet. Confirms the business is a formally registered
    legal entity — independent of, and a meaningful signal alongside, VAT
    registration status.
    """
    try:
        profile = verify_business_cac(db, current_user_id, data.rc_number)
    except ValueError as ve:
        raise HTTPException(status_code=400, detail=str(ve))
    return VerificationResultOut(
        verified=profile.cac_verified,
        verification_status=profile.verification_status,
        charged_kobo=LOOKUP_COST_KOBO["cac"] if profile.cac_verified else 0,
        registered_name=profile.cac_registered_name,
        message="CAC registration verified via Mono." if profile.cac_verified else "CAC verification pending.",
    )
