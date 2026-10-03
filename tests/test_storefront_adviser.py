from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.security import create_access_token
from app.models import models
from app.models.inventory_models import Product, ProductCategory
from app.services.ai.storefront import (
    ProductCopyOut,
    StorefrontAdviceConflictError,
    StorefrontAdviserService,
)


@pytest.fixture
def store_owner(db_session):
    owner = models.User(
        name="Store Merchant",
        business_name="The Good Store",
        email="store-owner@example.com",
        phone="+2348160000500",
        phone_verified=True,
        plan=models.SubscriptionPlan.PRO,
        storefront_enabled=True,
        storefront_slug="good-store",
        storefront_description="Useful everyday products",
        storefront_state="Lagos",
        storefront_hours={"0": {"open": "09:00", "close": "18:00"}},
        logo_url="https://example.com/logo.png",
        paystack_subaccount_active=True,
        paystack_subaccount_code="ACCT_test",
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
    price: str = "10000",
    cost: str | None = "6000",
    description: str | None = "A clear everyday product with verified information for buyers.",
    image: str | None = "https://example.com/product.png",
    stock: int = 20,
) -> Product:
    product = Product(
        user_id=owner_id,
        name=name,
        sku=sku,
        description=description,
        image_url=image,
        selling_price=Decimal(price),
        cost_price=Decimal(cost) if cost is not None else None,
        quantity_in_stock=stock,
        reorder_level=5,
        track_stock=True,
        fulfilment_type="physical",
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=100),
    )
    db.add(product)
    db.commit()
    db.refresh(product)
    return product


def _paid_order(db, owner, products: list[Product], suffix: str) -> None:
    customer = models.Customer(name=f"Buyer {suffix}", phone=f"+2348161{suffix.zfill(6)}")
    db.add(customer)
    db.flush()
    invoice = models.Invoice(
        invoice_id=f"INV-STORE-{suffix}",
        issuer_id=owner.id,
        customer_id=customer.id,
        amount=sum((product.selling_price for product in products), Decimal("0")),
        status="paid",
        invoice_type="revenue",
        channel="storefront",
        paid_at=dt.datetime.now(dt.timezone.utc),
    )
    db.add(invoice)
    db.flush()
    for product in products:
        db.add(
            models.InvoiceLine(
                invoice_id=invoice.id,
                product_id=product.id,
                description=product.name,
                quantity=1,
                unit_price=product.selling_price,
            )
        )
    db.add(
        models.StorefrontOrderEscrow(
            invoice_id=invoice.id,
            seller_id=owner.id,
            status="held",
            gross_kobo=int(invoice.amount * 100),
            fee_kobo=0,
            payout_kobo=int(invoice.amount * 100),
        )
    )
    db.commit()


def test_storefront_advice_scores_listings_funnel_promotions_and_bundles(db_session, store_owner):
    category = ProductCategory(user_id=store_owner.id, name="Home")
    db_session.add(category)
    db_session.commit()
    featured = _product(db_session, store_owner.id, name="Soap", sku="SOAP")
    featured.category_id = category.id
    companion = _product(db_session, store_owner.id, name="Sponge", sku="SPONGE")
    companion.category_id = category.id
    _product(
        db_session,
        store_owner.id,
        name="Mystery",
        sku="MYSTERY",
        description=None,
        image=None,
        cost=None,
    )
    db_session.commit()
    _paid_order(db_session, store_owner, [featured, companion], "1")
    _paid_order(db_session, store_owner, [featured, companion], "2")

    result = StorefrontAdviserService(db_session).advice(store_owner.id)
    by_sku = {
        db_session.get(Product, item["product_id"]).sku: item
        for item in result["listings"]
    }

    assert result["quality_score"] > 0
    assert by_sku["SOAP"]["recommendation"] == "feature"
    assert by_sku["SOAP"]["units_sold_30_days"] == 2
    assert by_sku["MYSTERY"]["recommendation"] == "improve_listing"
    assert by_sku["MYSTERY"]["quality_score"] < by_sku["SOAP"]["quality_score"]
    assert result["funnel"]["paid_orders_30_days"] == 2
    assert result["bundle_suggestions"][0]["product_ids"] == [featured.id, companion.id]
    assert result["bundle_suggestions"][0]["supporting_orders"] == 2


def test_storefront_actions_require_safe_current_facts(db_session, store_owner):
    first = _product(db_session, store_owner.id, name="Soap", sku="SOAP")
    second = _product(db_session, store_owner.id, name="Sponge", sku="SPONGE")
    _paid_order(db_session, store_owner, [first, second], "1")
    _paid_order(db_session, store_owner, [first, second], "2")
    service = StorefrontAdviserService(db_session)

    with pytest.raises(StorefrontAdviceConflictError):
        service.apply_promotion(first.id, 21, data_owner_id=store_owner.id)

    promoted = service.apply_promotion(first.id, 10, data_owner_id=store_owner.id)
    featured = service.apply_merchandising([first.id], data_owner_id=store_owner.id)
    bundled = service.apply_bundle(
        [first.id, second.id],
        "Cleaning pair",
        active=True,
        data_owner_id=store_owner.id,
    )

    assert promoted["discount_percent"] == 10
    assert featured["products"][0]["featured"] is True
    assert {item["bundle_label"] for item in bundled["products"]} == {"Cleaning pair"}


def test_storefront_reengagement_draft_requires_restock_request_and_open_window(
    db_session, store_owner, monkeypatch
):
    product = _product(db_session, store_owner.id, name="Soap", sku="SOAP", stock=10)
    notification = models.StorefrontStockNotification(
        user_id=store_owner.id,
        product_id=product.id,
        phone="+2348160000999",
        notified=False,
    )
    db_session.add(notification)
    db_session.commit()
    monkeypatch.setattr("app.services.ai.storefront.is_window_open", lambda phone: True)

    result = StorefrontAdviserService(db_session).advice(store_owner.id)

    assert len(result["reengagement_drafts"]) == 1
    draft = result["reengagement_drafts"][0]
    assert draft["notification_id"] == notification.id
    assert draft["recipient_masked"] == "***0999"
    assert "Soap is back in stock at The Good Store." in draft["message"]
    assert "You asked to be notified when it returned." in draft["message"]


@pytest.mark.asyncio
async def test_storefront_ai_copy_is_draft_only_until_merchant_applies_it(db_session, store_owner):
    product = _product(db_session, store_owner.id, name="Soap", sku="SOAP", description=None)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(
        return_value=ProductCopyOut(description="Soap available per piece from The Good Store.")
    )
    service = StorefrontAdviserService(db_session, gateway=gateway)

    draft = await service.draft_copy(
        product.id,
        actor_user_id=store_owner.id,
        data_owner_id=store_owner.id,
    )

    assert draft["ai_generated"] is True
    assert db_session.get(Product, product.id).description is None
    applied = service.apply_copy(product.id, draft["description"], data_owner_id=store_owner.id)
    assert applied["description"] == draft["description"]


def test_storefront_advice_api_and_public_discounted_merchandising(
    client, db_session, store_owner, monkeypatch
):
    product = _product(db_session, store_owner.id, name="Soap", sku="SOAP")
    product.fulfilment_type = "service"
    product.track_stock = False
    db_session.commit()
    token = create_access_token(str(store_owner.id))
    headers = {"Authorization": f"Bearer {token}"}

    advice = client.get("/ai/storefront/advice", headers=headers)
    assert advice.status_code == 200

    promotion = client.post(
        f"/ai/storefront/products/{product.id}/promotion",
        headers=headers,
        json={"discount_percent": 10},
    )
    assert promotion.status_code == 200

    feature = client.post(
        "/ai/storefront/merchandising",
        headers=headers,
        json={"product_ids": [product.id]},
    )
    assert feature.status_code == 200

    public = client.get("/public/store/good-store")
    assert public.status_code == 200
    listed = public.json()["products"][0]
    assert listed["price"] == 9000
    assert listed["original_price"] == 10000
    assert listed["discount_percent"] == 10
    assert listed["featured"] is True

    start_payment = AsyncMock(return_value={"authorization_url": "https://pay.example/discounted"})
    monkeypatch.setattr(
        "app.services.invoice_payment_service.start_invoice_payment",
        start_payment,
    )
    order = client.post(
        "/public/store/good-store/order",
        json={
            "customer_name": "Buyer",
            "customer_phone": "+2348160000888",
            "items": [{"product_id": product.id, "quantity": 1}],
        },
    )
    assert order.status_code == 200, order.text
    invoice = db_session.query(models.Invoice).filter_by(invoice_id=order.json()["invoice_id"]).one()
    assert invoice.amount == Decimal("9000")
    assert invoice.lines[0].unit_price == Decimal("9000")
    assert invoice.platform_fee_kobo == 27_000
    assert start_payment.await_args.kwargs["charge_amount_kobo"] == 927_000
    escrow = db_session.query(models.StorefrontOrderEscrow).filter_by(invoice_id=invoice.id).one()
    assert escrow.gross_kobo == 900_000
    assert escrow.fee_kobo == 27_000
    assert escrow.payout_kobo == 900_000
