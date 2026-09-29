"""Inventory integration mixin for automatic stock management.

This mixin provides seamless integration between invoices and inventory:
- Revenue invoices automatically deduct stock
- Expense invoices automatically add stock
- Stock movements are recorded for audit trail
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

if TYPE_CHECKING:
    from app.models.models import Invoice

logger = logging.getLogger(__name__)


class InventoryIntegrationMixin:
    """
    Mixin that integrates invoice creation with inventory management.

    OOP Principles:
    - Single Responsibility: Only handles inventory-invoice integration
    - Open/Closed: Extends invoice service without modifying core logic
    - Dependency Inversion: Uses abstract inventory service interface
    """

    db: Session

    def process_inventory_for_invoice(
        self,
        invoice: Invoice,
        lines_data: list[dict[str, Any]],
    ) -> None:
        """
        Process inventory updates for a newly created invoice.

        For revenue invoices: Deduct stock for sold items
        For expense invoices: Add stock for purchased items

        Args:
            invoice: The created invoice object
            lines_data: Original line item data (with product_id if linked)
        """
        from app.services.inventory import build_inventory_service

        # Check if any lines have product_id
        has_inventory_items = any(line.get("product_id") for line in lines_data)
        if not has_inventory_items:
            return  # No inventory items to process

        try:
            inventory_service = build_inventory_service(self.db, invoice.issuer_id)
            is_expense = invoice.invoice_type == "expense"

            # Prepare lines with invoice line IDs
            lines_with_ids = []
            for i, line_data in enumerate(lines_data):
                if line_data.get("product_id"):
                    line_dict = {
                        "product_id": line_data["product_id"],
                        "quantity": line_data.get("quantity", 1),
                        "unit_price": line_data.get("unit_price", 0),
                    }
                    # Try to get the created line ID
                    if i < len(invoice.lines):
                        line_dict["id"] = invoice.lines[i].id
                    lines_with_ids.append(line_dict)

            movements = inventory_service.process_invoice_lines(
                invoice_id=invoice.id,
                invoice_ref=invoice.invoice_id,
                lines=lines_with_ids,
                is_expense=is_expense,
            )

            if movements:
                action = "restocked" if is_expense else "deducted"
                logger.info(
                    "Inventory %s for invoice %s: %s product(s) updated",
                    action,
                    invoice.invoice_id,
                    len(movements),
                )

        except Exception as e:
            # Log but don't fail invoice creation
            logger.error("Inventory processing error for invoice %s: %s", invoice.invoice_id, e)

    def reverse_inventory_for_invoice(
        self,
        invoice: Invoice,
    ) -> None:
        """
        Reverse inventory changes when an invoice is cancelled/deleted.

        For cancelled sales: Add stock back
        For cancelled purchases: Remove stock

        Args:
            invoice: The invoice being cancelled
        """
        from app.models.inventory_models import StockMovement
        from app.services.inventory import build_inventory_service

        try:
            inventory_service = build_inventory_service(self.db, invoice.issuer_id)

            # Find all stock movements for this invoice
            movements = (
                self.db.query(StockMovement)
                .filter(
                    StockMovement.reference_id == invoice.invoice_id,
                )
                .all()
            )

            for movement in movements:
                # Create reversal movement
                reversal_qty = -movement.quantity  # Opposite of original

                if reversal_qty > 0:
                    # Was a sale, now adding back
                    inventory_service.record_purchase(
                        product_id=movement.product_id,
                        quantity=reversal_qty,
                        unit_cost=movement.unit_cost or Decimal(0),
                        reference_id=f"CANCEL-{invoice.invoice_id}",
                    )
                else:
                    # Was a purchase, now removing
                    inventory_service.record_sale(
                        product_id=movement.product_id,
                        quantity=abs(reversal_qty),
                        unit_price=movement.unit_cost or Decimal(0),
                        reference_id=f"CANCEL-{invoice.invoice_id}",
                    )

            logger.info("Reversed %s inventory movements for invoice %s", len(movements), invoice.invoice_id)

        except Exception as e:
            logger.error("Error reversing inventory for invoice %s: %s", invoice.invoice_id, e)
