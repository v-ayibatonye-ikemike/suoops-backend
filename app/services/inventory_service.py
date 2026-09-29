"""
Backward Compatibility Redirect.

This module redirects imports from the old inventory_service location
to the new modular inventory package at app.services.inventory.

DEPRECATED: Import directly from app.services.inventory instead:
    from app.services.inventory import InventoryService, build_inventory_service
"""

from app.services.inventory import (
    CategoryService,
    InventoryAnalyticsService,
    InventoryService,
    ProductService,
    PurchaseOrderService,
    StockMovementService,
    SupplierService,
    build_inventory_service,
)

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
