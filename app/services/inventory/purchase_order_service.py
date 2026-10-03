"""
Purchase Order Service.

Handles purchase order operations including auto-generation
for low stock reordering. Follows SRP by focusing solely on
purchase order management.
"""

from __future__ import annotations

import builtins
import datetime as dt
import logging
from decimal import Decimal
from typing import TYPE_CHECKING, cast

from sqlalchemy.orm import Session

from app.models.inventory_models import (
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
)
from app.utils.id_generator import generate_id

from .base import InventoryServiceBase

if TYPE_CHECKING:
    from .stock_service import StockMovementService

logger = logging.getLogger(__name__)


class PurchaseOrderService(InventoryServiceBase):
    """Service for purchase order operations."""

    def __init__(self, db: Session, user_id: int):
        super().__init__(db, user_id)
        # Stock service dependency for receiving orders
        self._stock_service: StockMovementService | None = None

    def set_stock_service(self, stock_service: StockMovementService) -> None:
        """Set the stock service for receiving orders."""
        self._stock_service = stock_service

    def generate_draft(
        self,
        product_ids: list[int],
        trigger_invoice_id: str | None = None,
        *,
        quantities: dict[int, int] | None = None,
        notes: str | None = None,
    ) -> PurchaseOrder | None:
        """
        Generate a draft purchase order for low-stock products.

        Called automatically when stock falls below reorder level,
        or manually for selected products.
        """
        if not product_ids:
            return None

        products = self._get_valid_products(product_ids)
        if not products:
            return None

        po = self._create_purchase_order(trigger_invoice_id, notes=notes)
        self._add_line_items(po, products, quantities=quantities)

        self._db.add(po)
        self._db.commit()
        self._db.refresh(po)

        logger.info(
            f"Created draft purchase order {po.order_number} for user {self._user_id} "
            f"with {len(products)} products, total ₦{po.total_amount:,.2f}"
        )

        return po

    def receive(self, order_id: int) -> PurchaseOrder:
        """
        Mark a purchase order as received and update inventory.

        Adds stock for all products in the order.
        """
        po = self._get_purchase_order(order_id)
        self._validate_can_receive(po)

        if po.status == PurchaseOrderStatus.RECEIVED:
            return po  # Already received

        self._process_received_lines(po)
        self._update_status_to_received(po)

        self._db.commit()
        self._db.refresh(po)

        logger.info("Purchase order %s received, inventory updated", po.order_number)
        return po

    def get(self, order_id: int) -> PurchaseOrder | None:
        """Get a purchase order by ID."""
        return (
            self._db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.id == order_id,
                PurchaseOrder.user_id == self._user_id,
            )
            .first()
        )

    def list(
        self,
        status: PurchaseOrderStatus | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[builtins.list[PurchaseOrder], int]:
        """List purchase orders with pagination."""
        query = self._db.query(PurchaseOrder).filter(
            PurchaseOrder.user_id == self._user_id,
        )

        if status:
            query = query.filter(PurchaseOrder.status == status)

        total = query.count()
        offset = (page - 1) * page_size
        orders = query.order_by(PurchaseOrder.created_at.desc()).offset(offset).limit(page_size).all()

        return orders, total

    def cancel(self, order_id: int) -> PurchaseOrder:
        """Cancel a purchase order."""
        po = self._get_purchase_order(order_id)

        if po.status == PurchaseOrderStatus.RECEIVED:
            raise ValueError("Cannot cancel a received purchase order")

        if po.status == PurchaseOrderStatus.CANCELLED:
            return po  # Already cancelled

        po.status = PurchaseOrderStatus.CANCELLED
        self._db.commit()
        self._db.refresh(po)

        logger.info("Purchase order %s cancelled", po.order_number)
        return po

    # ========================================================================
    # Private Helpers
    # ========================================================================

    def _get_valid_products(self, product_ids: builtins.list[int]) -> builtins.list[Product]:
        """Get valid, active products for the given IDs."""
        return cast(
            builtins.list[Product],
            self._db.query(Product)
            .filter(
                Product.id.in_(product_ids),
                Product.user_id == self._user_id,
                Product.is_active.is_(True),
            )
            .all(),
        )

    def _create_purchase_order(self, trigger_invoice_id: str | None, *, notes: str | None = None) -> PurchaseOrder:
        """Create a new purchase order instance."""
        resolved_notes = notes or (
            f"Auto-generated due to low stock after invoice {trigger_invoice_id}"
            if trigger_invoice_id
            else "Auto-generated due to low stock"
        )

        return PurchaseOrder(
            user_id=self._user_id,
            order_number=generate_id("PO"),
            status=PurchaseOrderStatus.DRAFT,
            auto_generated=True,
            trigger_invoice_id=trigger_invoice_id,
            notes=resolved_notes,
            total_amount=Decimal(0),
        )

    def _add_line_items(
        self,
        po: PurchaseOrder,
        products: builtins.list[Product],
        *,
        quantities: dict[int, int] | None = None,
    ) -> None:
        """Add line items to purchase order."""
        total_amount = Decimal(0)

        for product in products:
            quantity = quantities.get(product.id, product.reorder_quantity) if quantities else product.reorder_quantity
            if quantity <= 0:
                continue
            unit_cost = product.cost_price
            line_total = (unit_cost or Decimal(0)) * quantity

            po.lines.append(
                PurchaseOrderLine(
                    product_id=product.id,
                    quantity=quantity,
                    unit_cost=unit_cost,
                    total_cost=line_total,
                )
            )
            total_amount += line_total

        po.total_amount = total_amount

    def _get_purchase_order(self, order_id: int) -> PurchaseOrder:
        """Get purchase order or raise ValueError."""
        po = self.get(order_id)
        if not po:
            raise ValueError(f"Purchase order {order_id} not found")
        return po

    def _validate_can_receive(self, po: PurchaseOrder) -> None:
        """Validate that purchase order can be received."""
        if po.status == PurchaseOrderStatus.CANCELLED:
            raise ValueError("Cannot receive a cancelled purchase order")

    def _process_received_lines(self, po: PurchaseOrder) -> None:
        """Process all line items when receiving order."""
        if not self._stock_service:
            raise RuntimeError("Stock service not configured for PurchaseOrderService")

        for line in po.lines:
            self._stock_service.record_purchase(
                product_id=line.product_id,
                quantity=line.quantity,
                unit_cost=line.unit_cost or Decimal(0),
                reference_id=po.order_number,
            )
            line.quantity_received = line.quantity

    def _update_status_to_received(self, po: PurchaseOrder) -> None:
        """Update purchase order status to received."""
        po.status = PurchaseOrderStatus.RECEIVED
        po.received_date = dt.datetime.now(dt.timezone.utc)
