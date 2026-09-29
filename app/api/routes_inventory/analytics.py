"""Analytics endpoints."""

import logging

from fastapi import APIRouter

from app.models import inventory_schemas as schemas

from .dependencies import InventoryServiceDep

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/analytics/summary", response_model=schemas.InventorySummary)
def get_inventory_summary(
    service: InventoryServiceDep,
):
    """Get inventory summary statistics for dashboard."""
    return service.get_inventory_summary()


@router.get("/analytics/low-stock", response_model=list[schemas.LowStockAlert])
def get_low_stock_alerts(
    service: InventoryServiceDep,
):
    """Get products that need restocking."""
    return service.get_low_stock_alerts()
