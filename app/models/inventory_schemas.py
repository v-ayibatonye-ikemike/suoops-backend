"""
Pydantic schemas for Inventory API.

Following the same patterns as schemas.py for consistency.
"""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ============================================================================
# Product Category Schemas
# ============================================================================

class ProductCategoryCreate(BaseModel):
    """Schema for creating a product category."""
    name: str = Field(..., min_length=1, max_length=100)
    description: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")  # Hex color
    # Optional packaging fee (₦) added once to a storefront order containing any
    # product in this category. None/0 = no packaging.
    pack_price: Decimal | None = Field(None, ge=0)


class ProductCategoryUpdate(BaseModel):
    """Schema for updating a product category."""
    name: str | None = Field(None, min_length=1, max_length=100)
    description: str | None = None
    color: str | None = Field(None, pattern=r"^#[0-9A-Fa-f]{6}$")
    is_active: bool | None = None
    pack_price: Decimal | None = Field(None, ge=0)


class ProductCategoryOut(BaseModel):
    """Schema for category API response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    description: str | None = None
    color: str | None = None
    is_active: bool = True
    pack_price: Decimal | None = None
    product_count: int = 0  # Computed field


# ============================================================================
# Product Schemas
# ============================================================================

class ProductCreate(BaseModel):
    """Schema for creating a product."""
    # Optional — auto-generated from the name when blank, so non-technical users
    # (freelancers, services, marketers, etc.) never have to think about a "SKU".
    sku: str | None = Field(None, max_length=50)
    name: str = Field(..., min_length=1, max_length=200)
    description: str | None = None
    barcode: str | None = Field(None, max_length=50)
    category_id: int | None = None
    
    # Pricing
    cost_price: Decimal | None = Field(None, ge=0)
    selling_price: Decimal = Field(..., ge=0)
    
    # Stock
    quantity_in_stock: int = Field(default=0, ge=0)
    reorder_level: int = Field(default=10, ge=0)
    reorder_quantity: int = Field(default=20, ge=1)
    unit: str = Field(default="pcs", max_length=20)
    
    # Flags
    track_stock: bool = True
    # Fulfilment: physical items are delivered; service/digital are not.
    fulfilment_type: Literal["physical", "service", "digital"] = "physical"
    
    # Media
    image_url: str | None = None


class ProductUpdate(BaseModel):
    """Schema for updating a product."""
    sku: str | None = Field(None, min_length=1, max_length=50)
    name: str | None = Field(None, min_length=1, max_length=200)
    description: str | None = None
    barcode: str | None = Field(None, max_length=50)
    category_id: int | None = None
    
    cost_price: Decimal | None = Field(None, ge=0)
    selling_price: Decimal | None = Field(None, ge=0)
    
    # Direct quantity edits apply to services/digital only (physical stock is
    # adjusted via stock movements to keep an audit trail).
    quantity_in_stock: int | None = Field(None, ge=0)
    reorder_level: int | None = Field(None, ge=0)
    reorder_quantity: int | None = Field(None, ge=1)
    unit: str | None = Field(None, max_length=20)
    
    track_stock: bool | None = None
    is_active: bool | None = None
    fulfilment_type: Literal["physical", "service", "digital"] | None = None
    image_url: str | None = None
    # Opt out of the storefront-wide social promotion opt-in for this one item.
    exclude_from_social: bool | None = None


class ProductOut(BaseModel):
    """Schema for product API response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    sku: str
    name: str
    description: str | None = None
    barcode: str | None = None
    
    category_id: int | None = None
    category_name: str | None = None  # Populated from relationship
    
    cost_price: Decimal | None = None
    selling_price: Decimal
    
    quantity_in_stock: int = 0
    reorder_level: int = 10
    reorder_quantity: int = 20
    unit: str = "pcs"
    
    is_active: bool = True
    track_stock: bool = True
    fulfilment_type: str = "physical"
    exclude_from_social: bool = False
    
    image_url: str | None = None
    
    # Computed fields
    is_low_stock: bool = False
    is_out_of_stock: bool = False
    stock_value: Decimal | None = None
    profit_margin: Decimal | None = None
    
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None


class ProductListOut(BaseModel):
    """Schema for paginated product list."""
    products: list[ProductOut]
    total: int
    page: int
    page_size: int
    total_pages: int


# ============================================================================
# Stock Movement Schemas
# ============================================================================

StockMovementTypeEnum = Literal[
    "purchase", "sale", "adjustment", "return_in", "return_out", "transfer", "opening"
]


class StockAdjustmentCreate(BaseModel):
    """Schema for manual stock adjustment."""
    product_id: int
    quantity: int  # Positive to add, negative to remove
    movement_type: StockMovementTypeEnum = "adjustment"
    reason: str | None = None
    notes: str | None = None
    unit_cost: Decimal | None = Field(None, ge=0)

    @field_validator("quantity")
    @classmethod
    def quantity_not_zero(cls, v: int) -> int:
        if v == 0:
            raise ValueError("Quantity cannot be zero")
        return v


class StockMovementOut(BaseModel):
    """Schema for stock movement API response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    product_id: int
    product_name: str | None = None  # Populated from relationship
    product_sku: str | None = None
    
    movement_type: str
    quantity: int
    quantity_before: int
    quantity_after: int
    
    unit_cost: Decimal | None = None
    total_cost: Decimal | None = None
    
    reference_type: str | None = None
    reference_id: str | None = None
    
    reason: str | None = None
    notes: str | None = None
    
    created_at: dt.datetime
    created_by: str | None = None


class StockMovementListOut(BaseModel):
    """Schema for paginated stock movement list."""
    movements: list[StockMovementOut]
    total: int
    page: int
    page_size: int


# ============================================================================
# Supplier Schemas
# ============================================================================

class SupplierCreate(BaseModel):
    """Schema for creating a supplier."""
    name: str = Field(..., min_length=1, max_length=200)
    contact_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=32)
    address: str | None = None
    notes: str | None = None


class SupplierUpdate(BaseModel):
    """Schema for updating a supplier."""
    name: str | None = Field(None, min_length=1, max_length=200)
    contact_name: str | None = Field(None, max_length=100)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=32)
    address: str | None = None
    notes: str | None = None
    is_active: bool | None = None


class SupplierOut(BaseModel):
    """Schema for supplier API response."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    contact_name: str | None = None
    email: str | None = None
    phone: str | None = None
    address: str | None = None
    notes: str | None = None
    is_active: bool = True
    created_at: dt.datetime | None = None


# ============================================================================
# Inventory Analytics Schemas
# ============================================================================

class InventorySummary(BaseModel):
    """Summary stats for inventory dashboard."""
    total_products: int = 0
    active_products: int = 0
    low_stock_count: int = 0
    out_of_stock_count: int = 0
    total_stock_value: Decimal = Decimal("0")
    total_potential_revenue: Decimal = Decimal("0")
    categories_count: int = 0


class LowStockAlert(BaseModel):
    """Alert for products with low stock."""
    product_id: int
    product_name: str
    sku: str
    current_stock: int
    reorder_level: int
    reorder_quantity: int
    unit: str
