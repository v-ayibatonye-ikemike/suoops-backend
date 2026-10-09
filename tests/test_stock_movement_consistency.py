"""Stock writers must use fresh balances and avoid replaying invoice lines."""

from decimal import Decimal

import pytest
from sqlalchemy.orm import Session

from app.models import models
from app.models.inventory_models import Product, StockMovement, StockMovementType
from app.models.inventory_schemas import ProductCreate, StockAdjustmentCreate
from app.services.inventory import InventoryService


@pytest.fixture
def inventory_product(db_session):
    owner = models.User(phone="+2348160000092", name="Seller")
    db_session.add(owner)
    db_session.commit()
    inventory = InventoryService(db_session, owner.id)
    product = inventory.create_product(
        ProductCreate(
            name="Mug",
            quantity_in_stock=10,
            cost_price=Decimal("600"),
            selling_price=Decimal("1000"),
        )
    )
    return inventory, product


@pytest.mark.parametrize("operation,expected", [("sale", 5), ("purchase", 9), ("adjustment", 9)])
def test_stock_writer_refreshes_cached_product(db_session, inventory_product, operation, expected):
    inventory, product = inventory_product
    product_id = product.id
    assert product.quantity_in_stock == 10
    with Session(db_session.get_bind()) as other_session:
        other_product = other_session.get(Product, product_id)
        assert other_product is not None
        other_product.quantity_in_stock = 7
        other_session.commit()

    if operation == "sale":
        movement = inventory.record_sale(product_id, 2, Decimal("1000"))
    elif operation == "purchase":
        movement = inventory.record_purchase(product_id, 2, Decimal("600"))
    else:
        movement = inventory.adjust_stock(
            StockAdjustmentCreate(product_id=product_id, quantity=2, reason="Stock count"),
        )

    assert movement.quantity_before == 7
    assert movement.quantity_after == expected
    db_session.refresh(product)
    assert product.quantity_in_stock == expected


def test_replaying_same_invoice_line_does_not_deduct_stock_twice(db_session, inventory_product):
    inventory, product = inventory_product
    buyer = models.Customer(name="Buyer")
    db_session.add(buyer)
    db_session.flush()
    invoice = models.Invoice(
        invoice_id="INV-STOCK-RETRY",
        issuer_id=product.user_id,
        customer_id=buyer.id,
        amount=Decimal("2000"),
        status="paid",
        lines=[models.InvoiceLine(
            description="Mugs", quantity=2, unit_price=Decimal("1000"), product_id=product.id,
        )],
    )
    db_session.add(invoice)
    db_session.commit()
    first = inventory.record_sale(
        product.id, 2, Decimal("1000"), invoice_line_id=invoice.lines[0].id,
        reference_id=invoice.invoice_id,
    )
    repeated = inventory.record_sale(
        product.id, 2, Decimal("1000"), invoice_line_id=invoice.lines[0].id,
        reference_id=invoice.invoice_id,
    )
    assert repeated.id == first.id
    db_session.refresh(product)
    assert product.quantity_in_stock == 8
    assert db_session.query(StockMovement).filter(
        StockMovement.invoice_line_id == invoice.lines[0].id,
        StockMovement.movement_type == StockMovementType.SALE,
    ).count() == 1


def test_stale_balance_cannot_hide_insufficient_stock(db_session, inventory_product):
    inventory, product = inventory_product
    with Session(db_session.get_bind()) as other_session:
        other_product = other_session.get(Product, product.id)
        assert other_product is not None
        other_product.quantity_in_stock = 1
        other_session.commit()
    with pytest.raises(ValueError, match="Insufficient stock"):
        inventory.record_sale(product.id, 2, Decimal("1000"))
    db_session.refresh(product)
    assert product.quantity_in_stock == 1


def test_lock_refresh_preserves_pending_cost_edits(db_session, inventory_product):
    inventory, product = inventory_product
    product.cost_price = Decimal("700")
    movement = inventory.record_sale(product.id, 2, Decimal("1000"))
    assert movement.unit_cost == Decimal("700")


def test_sales_without_invoice_line_are_still_distinct(db_session, inventory_product):
    inventory, product = inventory_product
    first = inventory.record_sale(product.id, 1, Decimal("1000"))
    second = inventory.record_sale(product.id, 1, Decimal("1000"))
    assert first.id != second.id
    db_session.refresh(product)
    assert product.quantity_in_stock == 8
