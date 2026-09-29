"""
Inventory Analytics Service.

Handles inventory analytics, reporting, and low stock alerts.
Follows SRP by focusing solely on analytics and reporting.
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.inventory_models import Product, ProductCategory
from app.models.inventory_schemas import InventorySummary, LowStockAlert

from .base import InventoryServiceBase

logger = logging.getLogger(__name__)


class InventoryAnalyticsService(InventoryServiceBase):
    """Service for inventory analytics and reporting."""

    def __init__(self, db: Session, user_id: int):
        super().__init__(db, user_id)

    def get_inventory_summary(self) -> InventorySummary:
        """Get summary statistics for inventory dashboard."""
        total_products = self._count_products(active_only=False)
        active_products = self._count_products(active_only=True)
        low_stock_count = self._count_low_stock()
        out_of_stock_count = self._count_out_of_stock()
        total_stock_value, total_potential_revenue = self._calculate_stock_values()
        categories_count = self._count_categories()

        return InventorySummary(
            total_products=total_products,
            active_products=active_products,
            low_stock_count=low_stock_count,
            out_of_stock_count=out_of_stock_count,
            total_stock_value=total_stock_value,
            total_potential_revenue=total_potential_revenue,
            categories_count=categories_count,
        )

    def get_low_stock_alerts(self) -> list[LowStockAlert]:
        """Get list of products that need restocking."""
        products = (
            self._db.query(Product)
            .filter(
                Product.user_id == self._user_id,
                Product.is_active.is_(True),
                Product.track_stock.is_(True),
                Product.quantity_in_stock <= Product.reorder_level,
            )
            .order_by(Product.quantity_in_stock)
            .all()
        )

        return [self._build_low_stock_alert(p) for p in products]

    # ========================================================================
    # Private Helpers
    # ========================================================================

    def _count_products(self, active_only: bool = True) -> int:
        """Count products for user."""
        query = self._db.query(func.count(Product.id)).filter(
            Product.user_id == self._user_id,
        )
        if active_only:
            query = query.filter(Product.is_active.is_(True))
        return query.scalar() or 0

    def _count_low_stock(self) -> int:
        """Count products with low stock."""
        return (
            self._db.query(func.count(Product.id))
            .filter(
                Product.user_id == self._user_id,
                Product.is_active.is_(True),
                Product.track_stock.is_(True),
                Product.quantity_in_stock <= Product.reorder_level,
                Product.quantity_in_stock > 0,
            )
            .scalar()
            or 0
        )

    def _count_out_of_stock(self) -> int:
        """Count products out of stock."""
        return (
            self._db.query(func.count(Product.id))
            .filter(
                Product.user_id == self._user_id,
                Product.is_active.is_(True),
                Product.track_stock.is_(True),
                Product.quantity_in_stock <= 0,
            )
            .scalar()
            or 0
        )

    def _calculate_stock_values(self) -> tuple[Decimal, Decimal]:
        """Calculate total stock value and potential revenue."""
        products = (
            self._db.query(Product)
            .filter(
                Product.user_id == self._user_id,
                Product.is_active.is_(True),
                Product.track_stock.is_(True),
            )
            .all()
        )

        total_stock_value = Decimal("0")
        total_potential_revenue = Decimal("0")

        for p in products:
            total_stock_value += p.stock_value
            total_potential_revenue += p.potential_revenue

        return total_stock_value, total_potential_revenue

    def _count_categories(self) -> int:
        """Count active categories."""
        return (
            self._db.query(func.count(ProductCategory.id))
            .filter(
                ProductCategory.user_id == self._user_id,
                ProductCategory.is_active.is_(True),
            )
            .scalar()
            or 0
        )

    @staticmethod
    def _build_low_stock_alert(product: Product) -> LowStockAlert:
        """Build a LowStockAlert from a Product."""
        return LowStockAlert(
            product_id=product.id,
            product_name=product.name,
            sku=product.sku,
            current_stock=product.quantity_in_stock,
            reorder_level=product.reorder_level,
            reorder_quantity=product.reorder_quantity,
            unit=product.unit,
        )
