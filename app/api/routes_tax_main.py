"""Aggregate tax router combining split modules for main app inclusion."""

from fastapi import APIRouter

from app.api import routes_tax_misc, routes_tax_profile, routes_tax_vat
from app.api.routes_tax import reports as routes_tax_reports_new

router = APIRouter()

# Each sub-router already has prefix /tax; just include them
router.include_router(routes_tax_profile.router)
router.include_router(routes_tax_vat.router)
router.include_router(routes_tax_reports_new.router, prefix="/tax")  # New multi-period reports
router.include_router(routes_tax_misc.router)

__all__ = ["router"]
