"""Product category endpoints."""

import logging

from fastapi import APIRouter, HTTPException, Query

from app.models import inventory_schemas as schemas

from .dependencies import InventoryServiceAdminDep, InventoryServiceDep

router = APIRouter()
logger = logging.getLogger(__name__)


@router.post("/categories", response_model=schemas.ProductCategoryOut, status_code=201)
def create_category(
    data: schemas.ProductCategoryCreate,
    service: InventoryServiceAdminDep,
):
    """Create a new product category."""
    category = service.create_category(data)
    return schemas.ProductCategoryOut(
        id=category.id,
        name=category.name,
        description=category.description,
        color=category.color,
        is_active=category.is_active,
        pack_price=category.pack_price,
        product_count=len(category.products) if category.products else 0,
    )


@router.get("/categories", response_model=list[schemas.ProductCategoryOut])
def list_categories(
    service: InventoryServiceDep,
    include_inactive: bool = Query(False, description="Include inactive categories"),
):
    """List all product categories."""
    categories = service.list_categories(include_inactive=include_inactive)
    return [
        schemas.ProductCategoryOut(
            id=c.id,
            name=c.name,
            description=c.description,
            color=c.color,
            is_active=c.is_active,
            pack_price=c.pack_price,
            product_count=len(c.products) if c.products else 0,
        )
        for c in categories
    ]


@router.get("/categories/{category_id}", response_model=schemas.ProductCategoryOut)
def get_category(
    category_id: int,
    service: InventoryServiceDep,
):
    """Get a category by ID."""
    category = service.get_category(category_id)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    return schemas.ProductCategoryOut(
        id=category.id,
        name=category.name,
        description=category.description,
        color=category.color,
        is_active=category.is_active,
        pack_price=category.pack_price,
        product_count=len(category.products) if category.products else 0,
    )


@router.patch("/categories/{category_id}", response_model=schemas.ProductCategoryOut)
def update_category(
    category_id: int,
    data: schemas.ProductCategoryUpdate,
    service: InventoryServiceAdminDep,
):
    """Update a category."""
    category = service.update_category(category_id, data)
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    return schemas.ProductCategoryOut(
        id=category.id,
        name=category.name,
        description=category.description,
        color=category.color,
        is_active=category.is_active,
        pack_price=category.pack_price,
        product_count=len(category.products) if category.products else 0,
    )


@router.delete("/categories/{category_id}", status_code=204)
def delete_category(
    category_id: int,
    service: InventoryServiceAdminDep,
):
    """Delete a category (soft delete)."""
    if not service.delete_category(category_id):
        raise HTTPException(status_code=404, detail="Category not found")
