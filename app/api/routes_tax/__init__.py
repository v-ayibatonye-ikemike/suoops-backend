"""
Tax API Routes Module.

This module provides a modular, SRP-compliant tax routing system.
All routes are prefixed with /tax.

Sub-modules:
- profile: Tax profile management, small business checks, compliance
- reports: Tax report generation, downloads, CSV exports
- vat: VAT summary, calculation, return generation
- fiscalization: Fiscalization status, invoice fiscalization, development levy
"""

from __future__ import annotations

from fastapi import APIRouter

from .fiscalization import router as fiscalization_router
from .profile import router as profile_router
from .reports import router as reports_router
from .vat import router as vat_router

# Main router with /tax prefix
router = APIRouter(prefix="/tax", tags=["tax"])

# Include all sub-routers
router.include_router(profile_router)
router.include_router(reports_router)
router.include_router(vat_router)
router.include_router(fiscalization_router)

__all__ = ["router"]
