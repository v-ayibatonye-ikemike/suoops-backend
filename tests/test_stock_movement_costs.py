"""Inventory movements must preserve acquisition costs, not sale revenue."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.models import models
from app.models.inventory_schemas import ProductCreate, StockAdjustmentCreate
from app.services.inventory import InventoryService


@pytest.fixture
def inventory(db_session):
    owner = models.User(phone="+2348160000090", name="Owner", business_name="Cost test")
    db_session.add(owner)
    db_session.commit()
    return InventoryService(db_session, owner.id)


def test_sale_snapshots_cost_instead_of_selling_price(inventory, db_session):
    product = inventory.create_product(
        ProductCreate(
            name="Mug",
            cost_price=Decimal("600"),
            selling_price=Decimal("1000"),
            quantity_in_stock=10,
        )
    )
    movement = inventory.record_sale(product.id, 2, Decimal("1000"))

    assert movement.unit_cost == Decimal("600")
    assert movement.total_cost == Decimal("1200")
    assert product.quantity_in_stock == 8

    product.cost_price = Decimal("700")
    db_session.commit()
    now = datetime.now(timezone.utc)
    report = inventory.get_cogs_for_period(now - timedelta(days=1), now + timedelta(days=1))
    assert report["cogs_amount"] == Decimal("1200")
    assert report["current_inventory_value"] == Decimal("5600")


def test_explicit_zero_adjustment_cost_is_not_replaced(inventory):
    product = inventory.create_product(
        ProductCreate(
            name="Sample",
            cost_price=Decimal("600"),
            selling_price=Decimal("1000"),
            quantity_in_stock=10,
        )
    )
    movement = inventory.adjust_stock(
        StockAdjustmentCreate(
            product_id=product.id,
            quantity=1,
            movement_type="adjustment",
            unit_cost=Decimal("0"),
            reason="Free supplier sample",
        )
    )
    assert movement.unit_cost == Decimal("0")
    assert movement.total_cost == Decimal("0")


@pytest.mark.parametrize("cost_price", [Decimal("0"), None])
def test_sale_does_not_substitute_revenue_for_zero_or_unknown_cost(inventory, cost_price):
    product = inventory.create_product(
        ProductCreate(
            name="Gift",
            cost_price=cost_price,
            selling_price=Decimal("1000"),
            quantity_in_stock=10,
        )
    )
    movement = inventory.record_sale(product.id, 1, Decimal("1000"))
    assert movement.unit_cost == cost_price
    assert movement.total_cost == Decimal("0")
