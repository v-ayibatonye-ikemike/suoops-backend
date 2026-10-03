"""
Inventory Service Module.

This module provides a modular, SRP-compliant inventory management system.
The InventoryService class acts as a facade that composes all specialized
services for a unified API.

Usage:
    from app.services.inventory import InventoryService, build_inventory_service

    # Using factory function
    service = build_inventory_service(db, user_id)

    # Direct instantiation
    service = InventoryService(db, user_id)

    # Category operations
    category = service.create_category(data)
    categories = service.list_categories()

    # Product operations
    product = service.create_product(data)
    products = service.list_products()

    # Stock operations
    movement = service.adjust_stock(data)
    service.record_sale(product_id, quantity, unit_price)

    # Analytics
    summary = service.get_inventory_summary()
    alerts = service.get_low_stock_alerts()
"""

from __future__ import annotations

from typing import Sequence

from sqlalchemy.orm import Session

from app.models.inventory_models import (
    Product,
    ProductCategory,
    PurchaseOrder,
    PurchaseOrderStatus,
    StockMovement,
    Supplier,
)
from app.models.inventory_schemas import (
    InventorySummary,
    LowStockAlert,
    ProductCreate,
    ProductUpdate,
    StockAdjustmentCreate,
    SupplierCreate,
    SupplierUpdate,
)
from app.models.inventory_schemas import (
    ProductCategoryCreate as CategoryCreate,
)
from app.models.inventory_schemas import (
    ProductCategoryUpdate as CategoryUpdate,
)

from .analytics_service import InventoryAnalyticsService
from .category_service import CategoryService
from .product_service import ProductService
from .purchase_order_service import PurchaseOrderService
from .stock_service import StockMovementService
from .supplier_service import SupplierService


class InventoryService:
    """
    Facade for inventory management operations.

    Composes specialized services to provide a unified API while
    maintaining separation of concerns internally.
    """

    def __init__(self, db: Session, user_id: int):
        """Initialize all sub-services."""
        self._db = db
        self._user_id = user_id

        # Initialize specialized services
        self._categories = CategoryService(db, user_id)
        self._products = ProductService(db, user_id)
        self._stock = StockMovementService(db, user_id)
        self._suppliers = SupplierService(db, user_id)
        self._analytics = InventoryAnalyticsService(db, user_id)
        self._purchase_orders = PurchaseOrderService(db, user_id)

        # Wire up dependencies
        self._purchase_orders.set_stock_service(self._stock)

    # ========================================================================
    # Category Operations (delegated to CategoryService)
    # ========================================================================

    def create_category(self, data: CategoryCreate) -> ProductCategory:
        """Create a new product category."""
        return self._categories.create_category(data)

    def get_category(self, category_id: int) -> ProductCategory | None:
        """Get a category by ID."""
        return self._categories.get_category(category_id)

    def list_categories(self, include_inactive: bool = False) -> Sequence[ProductCategory]:
        """List all categories."""
        return self._categories.list_categories(include_inactive)

    def update_category(self, category_id: int, data: CategoryUpdate) -> ProductCategory | None:
        """Update a category."""
        return self._categories.update_category(category_id, data)

    def delete_category(self, category_id: int) -> bool:
        """Soft delete a category."""
        return self._categories.delete_category(category_id)

    # ========================================================================
    # Product Operations (delegated to ProductService)
    # ========================================================================

    def create_product(self, data: ProductCreate) -> Product:
        """Create a new product."""
        return self._products.create_product(data)

    def get_product(self, product_id: int) -> Product | None:
        """Get a product by ID."""
        return self._products.get_product(product_id)

    def get_product_by_sku(self, sku: str) -> Product | None:
        """Get a product by SKU."""
        return self._products.get_product_by_sku(sku)

    def list_products(
        self,
        category_id: int | None = None,
        include_inactive: bool = False,
        search: str | None = None,
        low_stock_only: bool = False,
        out_of_stock_only: bool = False,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[Product], int]:
        """List products with filtering and pagination."""
        return self._products.list_products(
            category_id=category_id,
            include_inactive=include_inactive,
            search=search,
            low_stock_only=low_stock_only,
            out_of_stock_only=out_of_stock_only,
            page=page,
            page_size=page_size,
        )

    def update_product(self, product_id: int, data: ProductUpdate) -> Product | None:
        """Update a product."""
        return self._products.update_product(product_id, data)

    def delete_product(self, product_id: int) -> bool:
        """Soft delete a product."""
        return self._products.delete_product(product_id)

    # ========================================================================
    # Stock Movement Operations (delegated to StockMovementService)
    # ========================================================================

    def adjust_stock(self, data: StockAdjustmentCreate, created_by: str = "user") -> StockMovement:
        """Adjust stock for a product."""
        return self._stock.adjust_stock(data, created_by)

    def record_sale(
        self,
        product_id: int,
        quantity: int,
        unit_price,
        invoice_line_id: int | None = None,
        reference_id: str | None = None,
    ) -> StockMovement | None:
        """Record a sale and deduct stock."""
        return self._stock.record_sale(
            product_id=product_id,
            quantity=quantity,
            unit_price=unit_price,
            invoice_line_id=invoice_line_id,
            reference_id=reference_id,
        )

    def record_purchase(
        self,
        product_id: int,
        quantity: int,
        unit_cost,
        expense_id: int | None = None,
        reference_id: str | None = None,
        supplier_id: int | None = None,
    ) -> StockMovement | None:
        """Record a purchase and add stock."""
        return self._stock.record_purchase(
            product_id=product_id,
            quantity=quantity,
            unit_cost=unit_cost,
            expense_id=expense_id,
            reference_id=reference_id,
            supplier_id=supplier_id,
        )

    def process_invoice_lines(
        self,
        invoice_id: int,
        invoice_ref: str,
        lines: list[dict],
        is_expense: bool = False,
    ) -> list[StockMovement]:
        """Process invoice lines and update inventory."""
        return self._stock.process_invoice_lines(
            invoice_id=invoice_id,
            invoice_ref=invoice_ref,
            lines=lines,
            is_expense=is_expense,
        )

    def get_cogs_for_period(self, start_date, end_date) -> dict:
        """Calculate Cost of Goods Sold (COGS) for a period."""
        return self._stock.get_cogs_for_period(start_date, end_date)

    def list_movements(
        self,
        product_id: int | None = None,
        movement_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[StockMovement], int]:
        """List stock movements with filtering."""
        return self._stock.list_movements(
            product_id=product_id,
            movement_type=movement_type,
            page=page,
            page_size=page_size,
        )

    # ========================================================================
    # Supplier Operations (delegated to SupplierService)
    # ========================================================================

    def create_supplier(self, data: SupplierCreate) -> Supplier:
        """Create a new supplier."""
        return self._suppliers.create(data)

    def get_supplier(self, supplier_id: int) -> Supplier | None:
        """Get a supplier by ID."""
        return self._suppliers.get(supplier_id)

    def list_suppliers(self, include_inactive: bool = False) -> Sequence[Supplier]:
        """List all suppliers."""
        return self._suppliers.list(include_inactive)

    def update_supplier(self, supplier_id: int, data: SupplierUpdate) -> Supplier | None:
        """Update a supplier."""
        return self._suppliers.update(supplier_id, data)

    def delete_supplier(self, supplier_id: int) -> bool:
        """Soft delete a supplier."""
        return self._suppliers.delete(supplier_id)

    # ========================================================================
    # Analytics Operations (delegated to InventoryAnalyticsService)
    # ========================================================================

    def get_inventory_summary(self) -> InventorySummary:
        """Get summary statistics for inventory dashboard."""
        return self._analytics.get_inventory_summary()

    def get_low_stock_alerts(self) -> list[LowStockAlert]:
        """Get list of products that need restocking."""
        return self._analytics.get_low_stock_alerts()

    # ========================================================================
    # Purchase Order Operations (delegated to PurchaseOrderService)
    # ========================================================================

    def generate_draft_purchase_order(
        self,
        product_ids: list[int],
        trigger_invoice_id: str | None = None,
        *,
        quantities: dict[int, int] | None = None,
        notes: str | None = None,
    ) -> PurchaseOrder | None:
        """Generate a draft purchase order for low-stock products."""
        return self._purchase_orders.generate_draft(
            product_ids=product_ids,
            trigger_invoice_id=trigger_invoice_id,
            quantities=quantities,
            notes=notes,
        )

    def receive_purchase_order(self, order_id: int) -> PurchaseOrder:
        """Mark a purchase order as received and update inventory."""
        return self._purchase_orders.receive(order_id)

    def get_purchase_order(self, order_id: int) -> PurchaseOrder | None:
        """Get a purchase order by ID."""
        return self._purchase_orders.get(order_id)

    def list_purchase_orders(
        self,
        status: PurchaseOrderStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[PurchaseOrder], int]:
        """List purchase orders with pagination."""
        return self._purchase_orders.list(status, page, page_size)

    def cancel_purchase_order(self, order_id: int) -> PurchaseOrder:
        """Cancel a purchase order."""
        return self._purchase_orders.cancel(order_id)


def build_inventory_service(db: Session, user_id: int) -> InventoryService:
    """Factory function to create an InventoryService instance."""
    return InventoryService(db=db, user_id=user_id)


# Re-export sub-services for direct access if needed
__all__ = [
    "InventoryService",
    "build_inventory_service",
    "CategoryService",
    "ProductService",
    "StockMovementService",
    "SupplierService",
    "InventoryAnalyticsService",
    "PurchaseOrderService",
]
