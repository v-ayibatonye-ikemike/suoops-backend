"""
Inventory API Routes.

RESTful endpoints for inventory management:
- Products (CRUD, search, stock levels)
- Categories (CRUD)
- Stock Movements (adjustments, history)
- Suppliers (CRUD)
- Analytics (summary, alerts)

Refactored from monolithic routes_inventory.py for SRP compliance.
"""

from fastapi import APIRouter

from .analytics import router as analytics_router
from .categories import router as categories_router
from .products import router as products_router
from .stock import router as stock_router
from .suppliers import router as suppliers_router

# Create main router and include sub-routers
router = APIRouter()
router.include_router(categories_router)
router.include_router(products_router)
router.include_router(stock_router)
router.include_router(suppliers_router)
router.include_router(analytics_router)

__all__ = ["router"]
