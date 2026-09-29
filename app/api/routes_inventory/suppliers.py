"""Supplier endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.models import inventory_schemas as schemas

from .dependencies import InventoryServiceAdminDep, InventoryServiceDep

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/suppliers", response_model=schemas.SupplierOut, status_code=201)
def create_supplier(
    data: schemas.SupplierCreate,
    service: InventoryServiceAdminDep,
):
    """Create a new supplier."""
    supplier = service.create_supplier(data)
    return schemas.SupplierOut.model_validate(supplier)


@router.get("/suppliers", response_model=list[schemas.SupplierOut])
def list_suppliers(
    service: InventoryServiceDep,
    include_inactive: bool = Query(False),
):
    """List all suppliers."""
    suppliers = service.list_suppliers(include_inactive=include_inactive)
    return [schemas.SupplierOut.model_validate(s) for s in suppliers]


@router.get("/suppliers/{supplier_id}", response_model=schemas.SupplierOut)
def get_supplier(
    supplier_id: int,
    service: InventoryServiceDep,
):
    """Get a supplier by ID."""
    supplier = service.get_supplier(supplier_id)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return schemas.SupplierOut.model_validate(supplier)


@router.patch("/suppliers/{supplier_id}", response_model=schemas.SupplierOut)
def update_supplier(
    supplier_id: int,
    data: schemas.SupplierUpdate,
    service: InventoryServiceAdminDep,
):
    """Update a supplier."""
    supplier = service.update_supplier(supplier_id, data)
    if not supplier:
        raise HTTPException(status_code=404, detail="Supplier not found")
    return schemas.SupplierOut.model_validate(supplier)


@router.delete("/suppliers/{supplier_id}", status_code=204)
def delete_supplier(
    supplier_id: int,
    service: InventoryServiceAdminDep,
):
    """Delete a supplier (soft delete)."""
    if not service.delete_supplier(supplier_id):
        raise HTTPException(status_code=404, detail="Supplier not found")
