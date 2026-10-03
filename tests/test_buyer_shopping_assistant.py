from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.config import settings
from app.models import models
from app.models.inventory_models import Product, ProductCategory
from app.services.ai.buyer import BuyerAIRanking, BuyerShoppingAssistantService


@pytest.fixture
def buyer_store(db_session):
    owner = models.User(
        name="Buyer Assistant Merchant",
        business_name="Helpful Store",
        email="buyer-assistant@example.com",
        phone="+2348160000600",
        storefront_enabled=True,
        storefront_slug="helpful-store",
        storefront_description="A verified public catalog",
        store_status="active",
    )
    db_session.add(owner)
    db_session.flush()
    category = ProductCategory(user_id=owner.id, name="Home care")
    db_session.add(category)
    db_session.flush()

    products = [
        Product(
            user_id=owner.id,
            category_id=category.id,
            name="Gentle Soap",
            sku="SOAP",
            description="Everyday soap for household cleaning.",
            image_url="https://example.com/soap.png",
            selling_price=Decimal("4000"),
            quantity_in_stock=5,
            track_stock=True,
            fulfilment_type="physical",
        ),
        Product(
            user_id=owner.id,
            category_id=category.id,
            name="Cleaning Service",
            sku="SERVICE",
            description="A bookable home cleaning service.",
            image_url="https://example.com/service.png",
            selling_price=Decimal("12000"),
            quantity_in_stock=0,
            track_stock=False,
            fulfilment_type="service",
        ),
        Product(
            user_id=owner.id,
            category_id=category.id,
            name="Sold Out Sponge",
            sku="SPONGE",
            description="A household cleaning sponge.",
            image_url="https://example.com/sponge.png",
            selling_price=Decimal("2000"),
            quantity_in_stock=0,
            track_stock=True,
            fulfilment_type="physical",
        ),
    ]
    db_session.add_all(products)
    db_session.commit()
    for product in products:
        db_session.refresh(product)
    return owner, products


@pytest.mark.asyncio
async def test_buyer_assistant_enforces_budget_stock_and_cart(db_session, buyer_store, monkeypatch):
    owner, products = buyer_store
    monkeypatch.setattr(settings, "AI_BUYER_ASSISTANT_ENABLED", False)

    result = await BuyerShoppingAssistantService(db_session).recommend(
        owner_id=owner.id,
        query="I need household cleaning under ₦5k",
        products=products,
        cart_product_ids=[],
    )

    assert result["detected_budget"] == 5000
    assert [match["product_id"] for match in result["matches"]] == [products[0].id]
    assert result["matches"][0]["price"] == 4000
    assert result["ai_ranked"] is False

    cart_result = await BuyerShoppingAssistantService(db_session).recommend(
        owner_id=owner.id,
        query="What do you sell?",
        products=products,
        cart_product_ids=[products[0].id],
    )
    assert products[0].id not in {match["product_id"] for match in cart_result["matches"]}
    assert products[2].id not in {match["product_id"] for match in cart_result["matches"]}


@pytest.mark.asyncio
async def test_buyer_ai_can_only_select_verified_candidates(db_session, buyer_store, monkeypatch):
    owner, products = buyer_store
    monkeypatch.setattr(settings, "AI_BUYER_ASSISTANT_ENABLED", True)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(
        return_value=BuyerAIRanking(product_ids=[999999, products[0].id, products[2].id])
    )

    result = await BuyerShoppingAssistantService(db_session, gateway=gateway).recommend(
        owner_id=owner.id,
        query="Recommend a household item",
        products=products,
        cart_product_ids=[],
    )

    assert [match["product_id"] for match in result["matches"]] == [products[0].id]
    assert result["ai_ranked"] is True

    gateway.generate_structured.reset_mock()
    monkeypatch.setattr(settings, "AI_BUYER_DAILY_OPERATIONS_PER_STORE", 0)
    limited = await BuyerShoppingAssistantService(db_session, gateway=gateway).recommend(
        owner_id=owner.id,
        query="Recommend a household item",
        products=products,
        cart_product_ids=[],
    )
    gateway.generate_structured.assert_not_awaited()
    assert limited["ai_ranked"] is False
    assert "daily AI ranking limit" in limited["notice"]


def test_public_buyer_assistant_is_store_scoped_and_uses_current_prices(
    client, db_session, buyer_store, monkeypatch
):
    owner, products = buyer_store
    products[0].storefront_discount_percent = 10
    db_session.commit()
    monkeypatch.setattr(settings, "AI_BUYER_ASSISTANT_ENABLED", False)

    response = client.post(
        "/public/store/helpful-store/shopping-assistant",
        json={"query": "soap under 5000", "cart_product_ids": []},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["matches"] == [
        {
            "product_id": products[0].id,
            "name": "Gentle Soap",
            "price": 3600.0,
            "original_price": 4000.0,
            "discount_percent": 10,
            "category": "Home care",
            "fulfilment_type": "physical",
            "reason": "Matches: soap.",
        }
    ]
    assert payload["detected_budget"] == 5000.0

    owner.storefront_enabled = False
    db_session.commit()
    offline = client.post(
        "/public/store/helpful-store/shopping-assistant",
        json={"query": "soap", "cart_product_ids": []},
    )
    assert offline.status_code == 404
