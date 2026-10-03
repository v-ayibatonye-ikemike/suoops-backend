from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.security import create_access_token
from app.models.inventory_models import (
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    StockMovement,
    StockMovementType,
)
from app.models.models import SubscriptionPlan, User
from app.services.ai.inventory import (
    InventoryAdviceConflictError,
    InventoryAdviserService,
    InventoryNarrative,
)


@pytest.fixture
def inventory_owner(db_session):
    owner = User(
        name="Inventory Merchant",
        business_name="Inventory Shop",
        email="inventory@example.com",
        phone="+2348160000400",
        plan=SubscriptionPlan.PRO,
    )
    db_session.add(owner)
    db_session.commit()
    db_session.refresh(owner)
    return owner


def _product(
    db,
    owner_id: int,
    *,
    name: str,
    sku: str,
    stock: int,
    reorder_level: int = 5,
    reorder_quantity: int = 20,
    cost: str = "1000",
    age_days: int = 100,
) -> Product:
    product = Product(
        user_id=owner_id,
        name=name,
        sku=sku,
        selling_price=Decimal("2000"),
        cost_price=Decimal(cost),
        quantity_in_stock=stock,
        reorder_level=reorder_level,
        reorder_quantity=reorder_quantity,
        track_stock=True,
        fulfilment_type="physical",
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=age_days),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _sale(db, owner_id: int, product: Product, quantity: int, days_ago: int) -> None:
    db.add(
        StockMovement(
            user_id=owner_id,
            product_id=product.id,
            movement_type=StockMovementType.SALE,
            quantity=-quantity,
            quantity_before=product.quantity_in_stock + quantity,
            quantity_after=product.quantity_in_stock,
            unit_cost=product.cost_price,
            total_cost=(product.cost_price or Decimal("0")) * quantity,
            reason="Invoice sale",
            created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days_ago),
        )
    )
    db.commit()


@pytest.mark.asyncio
async def test_inventory_adviser_calculates_velocity_cover_reorder_and_slow_stock(
    db_session, inventory_owner
):
    reorder = _product(db_session, inventory_owner.id, name="Fast Soap", sku="SOAP", stock=5)
    _product(db_session, inventory_owner.id, name="Old Mug", sku="MUG", stock=20)
    healthy = _product(db_session, inventory_owner.id, name="Rice Bag", sku="RICE", stock=100)
    _sale(db_session, inventory_owner.id, reorder, 15, 4)
    _sale(db_session, inventory_owner.id, healthy, 10, 10)

    result = await InventoryAdviserService(db_session).advice(
        actor_user_id=inventory_owner.id,
        data_owner_id=inventory_owner.id,
    )
    by_sku = {item["sku"]: item for item in result["recommendations"]}

    assert result["reorder_count"] == 1
    assert result["slow_stock_count"] == 1
    assert by_sku["SOAP"]["daily_sales_velocity"] == 0.5
    assert by_sku["SOAP"]["days_of_stock"] == 10
    assert by_sku["SOAP"]["demand_trend"] == "rising"
    assert by_sku["SOAP"]["recommended_order_quantity"] == 14
    assert by_sku["SOAP"]["estimated_order_cost"] == 14000
    assert by_sku["MUG"]["recommendation"] == "slow_stock"
    assert by_sku["MUG"]["recommended_order_quantity"] == 0
    assert by_sku["RICE"]["recommendation"] == "healthy"
    assert result["ai_generated"] is False
    assert "verified" not in result["summary"].lower() or result["generation_notice"] is None


@pytest.mark.asyncio
async def test_inventory_adviser_subtracts_open_purchase_order_stock(db_session, inventory_owner):
    product = _product(db_session, inventory_owner.id, name="Fast Soap", sku="SOAP", stock=5)
    _sale(db_session, inventory_owner.id, product, 15, 4)
    order = PurchaseOrder(
        user_id=inventory_owner.id,
        order_number="PO-OPEN-1",
        status=PurchaseOrderStatus.DRAFT,
        total_amount=Decimal("10000"),
        notes="Manual draft",
    )
    order.lines.append(
        PurchaseOrderLine(
            product_id=product.id,
            quantity=10,
            quantity_received=0,
            unit_cost=Decimal("1000"),
            total_cost=Decimal("10000"),
        )
    )
    db_session.add(order)
    db_session.commit()

    result = await InventoryAdviserService(db_session).advice(
        actor_user_id=inventory_owner.id,
        data_owner_id=inventory_owner.id,
    )
    recommendation = result["recommendations"][0]

    assert recommendation["incoming_stock"] == 10
    assert recommendation["recommended_order_quantity"] == 4
    assert "open_purchase_order_stock" in recommendation["reason_codes"]


@pytest.mark.asyncio
async def test_inventory_ai_explains_without_changing_verified_recommendations(
    db_session, inventory_owner, monkeypatch
):
    monkeypatch.setattr("app.services.ai.inventory.settings.AI_INVENTORY_ENHANCEMENT_ENABLED", True)
    product = _product(db_session, inventory_owner.id, name="Fast Soap", sku="SOAP", stock=5)
    _sale(db_session, inventory_owner.id, product, 15, 4)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(
        return_value=InventoryNarrative(
            headline="Restock Fast Soap soon",
            summary="Recorded sales show that the remaining stock may run out soon.",
        )
    )

    result = await InventoryAdviserService(db_session, gateway=gateway).advice(
        actor_user_id=inventory_owner.id,
        data_owner_id=inventory_owner.id,
        enhance=True,
    )

    assert result["ai_generated"] is True
    assert result["headline"] == "Restock Fast Soap soon"
    assert result["recommendations"][0]["recommended_order_quantity"] == 14
    request = gateway.generate_structured.await_args.args[0]
    assert request.feature == "inventory_advice_explanation"
    assert "recommended_order_quantity" in request.messages[1].content


def test_inventory_purchase_order_requires_current_recommendation_and_is_idempotent(
    db_session, inventory_owner
):
    product = _product(db_session, inventory_owner.id, name="Fast Soap", sku="SOAP", stock=5)
    healthy = _product(db_session, inventory_owner.id, name="Healthy Rice", sku="RICE", stock=100)
    _sale(db_session, inventory_owner.id, product, 15, 4)
    _sale(db_session, inventory_owner.id, healthy, 10, 10)
    service = InventoryAdviserService(db_session)

    with pytest.raises(InventoryAdviceConflictError):
        service.create_purchase_order([healthy.id], data_owner_id=inventory_owner.id)

    first = service.create_purchase_order([product.id], data_owner_id=inventory_owner.id)
    second = service.create_purchase_order([product.id], data_owner_id=inventory_owner.id)

    assert first["created"] is True
    assert first["status"] == "draft"
    assert first["lines"][0]["quantity"] == 14
    assert first["total_amount"] == 14000
    assert second["created"] is False
    assert second["id"] == first["id"]
    assert db_session.query(PurchaseOrder).count() == 1


def test_inventory_advice_api_and_merchant_approved_draft(client, db_session, inventory_owner):
    product = _product(db_session, inventory_owner.id, name="Fast Soap", sku="SOAP", stock=5)
    _sale(db_session, inventory_owner.id, product, 15, 4)
    token = create_access_token(str(inventory_owner.id))
    headers = {"Authorization": f"Bearer {token}"}

    advice = client.get("/ai/inventory/advice", headers=headers)
    assert advice.status_code == 200
    assert advice.json()["recommendations"][0]["product_id"] == product.id

    approved = client.post(
        "/ai/inventory/purchase-orders",
        headers=headers,
        json={"product_ids": [product.id]},
    )
    assert approved.status_code == 200
    assert approved.json()["created"] is True
    assert approved.json()["status"] == "draft"
    assert "Review supplier" in approved.json()["notice"]
