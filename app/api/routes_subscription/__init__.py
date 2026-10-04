"""Historical billing records and legacy subscription cancellation.

SuoOps uses commission billing and no longer sells subscription plans. Payment
history remains available, and existing recurring subscriptions can still be
inspected and cancelled.
"""

from fastapi import APIRouter

from .cancel import router as cancel_router
from .constants import PAYSTACK_PLAN_CODES, PLAN_PRICES
from .history import router as history_router

router = APIRouter()
router.include_router(history_router)
router.include_router(cancel_router)

__all__ = ["router", "PLAN_PRICES", "PAYSTACK_PLAN_CODES"]
