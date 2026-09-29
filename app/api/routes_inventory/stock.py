"""Stock movement endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.models import inventory_schemas as schemas

from .dependencies import InventoryServiceAdminDep, InventoryServiceDep
from .helpers import movement_to_out

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/stock/adjust", response_model=schemas.StockMovementOut, status_code=201)
def adjust_stock(
    data: schemas.StockAdjustmentCreate,
    service: InventoryServiceAdminDep,
):
    """
    Adjust stock for a product.

    Use positive quantity to add stock (purchase, return),
    negative to remove (damage, adjustment).
    """
    try:
        movement = service.adjust_stock(data, created_by="user")
        return movement_to_out(movement)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/stock/movements", response_model=schemas.StockMovementListOut)
def list_stock_movements(
    service: InventoryServiceDep,
    product_id: int | None = Query(None, description="Filter by product"),
    movement_type: str | None = Query(None, description="Filter by movement type"),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
):
    """List stock movements (audit trail)."""
    movements, total = service.list_movements(
        product_id=product_id,
        movement_type=movement_type,
        page=page,
        page_size=page_size,
    )
    return schemas.StockMovementListOut(
        movements=[movement_to_out(m) for m in movements],
        total=total,
        page=page,
        page_size=page_size,
    )
