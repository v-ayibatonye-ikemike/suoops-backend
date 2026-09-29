"""
Stock Movement Service.

Handles all stock movement operations including adjustments, sales, purchases,
and COGS calculations. Follows SRP by focusing solely on stock movements.
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Sequence

from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.models.inventory_models import (
    Product,
    StockMovement,
    StockMovementType,
)
from app.models.inventory_schemas import StockAdjustmentCreate

from .base import InventoryServiceBase

logger = logging.getLogger(__name__)


class StockMovementService(InventoryServiceBase):
    """Service for stock movement operations."""

    def __init__(self, db: Session, user_id: int):
        super().__init__(db, user_id)

    # ========================================================================
    # Stock Adjustment
    # ========================================================================

    def adjust_stock(self, data: StockAdjustmentCreate, created_by: str = "user") -> StockMovement:
        """
        Adjust stock for a product.

        Creates a stock movement record and updates the product quantity.
        """
        product = self._get_product_or_raise(data.product_id)
        self._validate_stock_tracking(product)

        quantity_before = product.quantity_in_stock

        try:
            product.adjust_stock(data.quantity)
        except ValueError as e:
            raise ValueError(str(e))

        quantity_after = product.quantity_in_stock

        movement = self._create_movement(
            product=product,
            movement_type=StockMovementType(data.movement_type),
            quantity=data.quantity,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            unit_cost=data.unit_cost or product.cost_price,
            reason=data.reason,
            notes=data.notes,
            created_by=created_by,
        )

        logger.info(
            f"Stock adjusted for {product.name}: {quantity_before} -> {quantity_after} "
            f"(change: {data.quantity}, type: {data.movement_type})"
        )
        self._maybe_notify_restock(product, quantity_before, quantity_after)
        return movement

    # ========================================================================
    # Sales Recording
    # ========================================================================

    def record_sale(
        self,
        product_id: int,
        quantity: int,
        unit_price: Decimal,
        invoice_line_id: int | None = None,
        reference_id: str | None = None,
    ) -> StockMovement | None:
        """
        Record a sale and deduct stock.

        Called when an invoice is paid with line items linked to products.
        Returns None if product doesn't track stock.
        """
        product = self._get_product_for_update(product_id)
        if not product or not product.track_stock:
            return None

        quantity_before = product.quantity_in_stock

        try:
            product.adjust_stock(-quantity)
        except ValueError as err:
            raise ValueError(
                f"Insufficient stock for product '{product.name}'. "
                f"Available: {product.quantity_in_stock}, Requested: {quantity}"
            ) from err

        quantity_after = product.quantity_in_stock

        movement = self._create_movement(
            product=product,
            movement_type=StockMovementType.SALE,
            quantity=-quantity,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            unit_cost=unit_price,
            reference_type="invoice",
            reference_id=reference_id,
            invoice_line_id=invoice_line_id,
            reason="Invoice sale",
            created_by="system",
        )

        logger.info("Sale recorded for %s: %s -> %s", product.name, quantity_before, quantity_after)
        return movement

    # ========================================================================
    # Purchase Recording
    # ========================================================================

    def record_purchase(
        self,
        product_id: int,
        quantity: int,
        unit_cost: Decimal,
        expense_id: int | None = None,
        reference_id: str | None = None,
        supplier_id: int | None = None,
    ) -> StockMovement | None:
        """
        Record a purchase and add stock.

        Called when an expense/receipt with inventory items is created.
        """
        product = self._get_product(product_id)
        if not product or not product.track_stock:
            return None

        quantity_before = product.quantity_in_stock
        product.adjust_stock(quantity)
        quantity_after = product.quantity_in_stock

        if unit_cost and unit_cost > 0:
            product.cost_price = unit_cost

        movement = self._create_movement(
            product=product,
            movement_type=StockMovementType.PURCHASE,
            quantity=quantity,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            unit_cost=unit_cost,
            reference_type="expense" if expense_id else "manual",
            reference_id=reference_id,
            supplier_id=supplier_id,
            reason="Expense purchase" if expense_id else "Manual purchase",
            created_by="system",
        )

        logger.info("Purchase recorded for %s: %s -> %s", product.name, quantity_before, quantity_after)
        self._maybe_notify_restock(product, quantity_before, quantity_after)
        return movement

    def _maybe_notify_restock(self, product, before: int, after: int) -> None:
        """Enqueue back-in-stock texts when stock crosses from 0 to positive."""
        if before <= 0 < after:
            try:
                from app.workers.tasks.messaging_tasks import notify_back_in_stock

                # Small delay so the caller's transaction is committed first.
                notify_back_in_stock.apply_async(args=[product.id], countdown=15)
            except Exception:  # noqa: BLE001
                logger.exception("Failed to enqueue back-in-stock notify for product %s", product.id)

    # ========================================================================
    # Invoice Line Processing
    # ========================================================================

    def process_invoice_lines(
        self,
        invoice_id: int,
        invoice_ref: str,
        lines: list[dict],
        is_expense: bool = False,
    ) -> list[StockMovement]:
        """
        Process invoice lines and update inventory accordingly.

        For revenue invoices: Deduct stock (sales)
        For expense invoices: Add stock (purchases)
        """
        movements = []

        for line in lines:
            product_id = line.get("product_id")
            if not product_id:
                continue

            quantity = line.get("quantity", 1)
            unit_price = Decimal(str(line.get("unit_price", 0)))
            invoice_line_id = line.get("id")

            try:
                movement = self._process_single_line(
                    product_id=product_id,
                    quantity=quantity,
                    unit_price=unit_price,
                    invoice_id=invoice_id,
                    invoice_ref=invoice_ref,
                    invoice_line_id=invoice_line_id,
                    is_expense=is_expense,
                )
                if movement:
                    movements.append(movement)
            except ValueError as e:
                logger.warning("Inventory processing error for product %s: %s", product_id, e)

        return movements

    def _process_single_line(
        self,
        product_id: int,
        quantity: int,
        unit_price: Decimal,
        invoice_id: int,
        invoice_ref: str,
        invoice_line_id: int | None,
        is_expense: bool,
    ) -> StockMovement | None:
        """Process a single invoice line for inventory."""
        if is_expense:
            return self.record_purchase(
                product_id=product_id,
                quantity=quantity,
                unit_cost=unit_price,
                expense_id=invoice_id,
                reference_id=invoice_ref,
            )
        return self.record_sale(
            product_id=product_id,
            quantity=quantity,
            unit_price=unit_price,
            invoice_line_id=invoice_line_id,
            reference_id=invoice_ref,
        )

    # ========================================================================
    # COGS Calculation
    # ========================================================================

    def get_cogs_for_period(self, start_date, end_date) -> dict:
        """
        Calculate Cost of Goods Sold (COGS) for a period.

        COGS = Beginning Inventory + Purchases - Ending Inventory
        """
        cogs_at_cost = self._calculate_cogs_at_cost(start_date, end_date)
        purchases_amount = self._calculate_purchases(start_date, end_date)
        current_inventory = self._get_current_inventory_value()

        return {
            "cogs_amount": cogs_at_cost,
            "purchases_amount": purchases_amount,
            "current_inventory_value": current_inventory,
            "period_start": self._format_date(start_date),
            "period_end": self._format_date(end_date),
        }

    def _calculate_cogs_at_cost(self, start_date, end_date) -> Decimal:
        """Calculate COGS at cost for period."""
        return self._db.query(
            func.sum(func.abs(StockMovement.quantity) * func.coalesce(StockMovement.unit_cost, Decimal(0)))
        ).filter(
            StockMovement.user_id == self._user_id,
            StockMovement.movement_type == StockMovementType.SALE,
            StockMovement.created_at >= start_date,
            StockMovement.created_at <= end_date,
        ).scalar() or Decimal(0)

    def _calculate_purchases(self, start_date, end_date) -> Decimal:
        """Calculate total purchases for period."""
        return self._db.query(func.sum(StockMovement.total_cost)).filter(
            StockMovement.user_id == self._user_id,
            StockMovement.movement_type == StockMovementType.PURCHASE,
            StockMovement.created_at >= start_date,
            StockMovement.created_at <= end_date,
        ).scalar() or Decimal(0)

    def _get_current_inventory_value(self) -> Decimal:
        """Get current total inventory value."""
        return self._db.query(
            func.sum(Product.quantity_in_stock * func.coalesce(Product.cost_price, Decimal(0)))
        ).filter(
            Product.user_id == self._user_id,
            Product.is_active.is_(True),
            Product.track_stock.is_(True),
        ).scalar() or Decimal(0)

    # ========================================================================
    # Movement Listing
    # ========================================================================

    def list_movements(
        self,
        product_id: int | None = None,
        movement_type: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> tuple[Sequence[StockMovement], int]:
        """List stock movements with filtering."""
        query = (
            self._db.query(StockMovement)
            .options(joinedload(StockMovement.product))
            .filter(StockMovement.user_id == self._user_id)
        )

        if product_id:
            query = query.filter(StockMovement.product_id == product_id)

        if movement_type:
            query = query.filter(StockMovement.movement_type == StockMovementType(movement_type))

        total = query.count()
        offset = (page - 1) * page_size
        movements = query.order_by(StockMovement.created_at.desc()).offset(offset).limit(page_size).all()

        return movements, total

    # ========================================================================
    # Private Helpers
    # ========================================================================

    def _get_product(self, product_id: int) -> Product | None:
        """Get product by ID."""
        return (
            self._db.query(Product)
            .filter(
                Product.id == product_id,
                Product.user_id == self._user_id,
            )
            .first()
        )

    def _get_product_for_update(self, product_id: int) -> Product | None:
        """Like _get_product but locks the row (SELECT ... FOR UPDATE) so two paid
        webhooks racing on the same product serialize and can't oversell."""
        return (
            self._db.query(Product)
            .filter(Product.id == product_id, Product.user_id == self._user_id)
            .with_for_update()
            .first()
        )

    def _get_product_or_raise(self, product_id: int) -> Product:
        """Get product by ID or raise ValueError."""
        product = self._get_product(product_id)
        if not product:
            raise ValueError(f"Product with ID {product_id} not found")
        return product

    def _validate_stock_tracking(self, product: Product) -> None:
        """Validate that product tracks stock."""
        if not product.track_stock:
            raise ValueError(f"Stock tracking is disabled for product '{product.name}'")

    def _create_movement(
        self,
        product: Product,
        movement_type: StockMovementType,
        quantity: int,
        quantity_before: int,
        quantity_after: int,
        unit_cost: Decimal | None = None,
        reason: str | None = None,
        notes: str | None = None,
        created_by: str = "user",
        reference_type: str | None = None,
        reference_id: str | None = None,
        invoice_line_id: int | None = None,
        supplier_id: int | None = None,
    ) -> StockMovement:
        """Create and persist a stock movement record."""
        total_cost = (unit_cost or Decimal(0)) * abs(quantity)

        movement = StockMovement(
            user_id=self._user_id,
            product_id=product.id,
            movement_type=movement_type,
            quantity=quantity,
            quantity_before=quantity_before,
            quantity_after=quantity_after,
            unit_cost=unit_cost,
            total_cost=total_cost,
            reason=reason,
            notes=notes,
            created_by=created_by,
            reference_type=reference_type,
            reference_id=reference_id,
            invoice_line_id=invoice_line_id,
            supplier_id=supplier_id,
        )
        self._db.add(movement)
        self._db.commit()
        self._db.refresh(movement)
        return movement

    @staticmethod
    def _format_date(date) -> str:
        """Format date to ISO string."""
        return date.isoformat() if hasattr(date, "isoformat") else str(date)
