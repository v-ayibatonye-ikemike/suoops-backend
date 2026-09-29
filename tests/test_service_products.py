"""Service/digital storefront orders: skip delivery, use the fast escrow window."""

from decimal import Decimal
from unittest.mock import AsyncMock

from app.core.config import settings
from app.models import models
from app.models.inventory_models import Product


def _seed_service_store(db):
    owner = models.User(
        phone="+2348160000030",
        name="Coach",
        business_name="Coaching Co",
        bank_name="Opay",
        account_number="0123456789",
        account_name="COACH",
        paystack_subaccount_active=True,
        paystack_subaccount_code="ACCT_svc",
        storefront_enabled=True,
        store_status="active",
        storefront_slug="coachstore",
        storefront_state="Lagos",
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
    prod = Product(
        user_id=owner.id,
        sku="SESSION-1",
        name="1hr Consultation",
        description="A one-hour coaching session",
        image_url="http://img/x.jpg",
        selling_price=Decimal("20000"),
        is_active=True,
        track_stock=False,
        fulfilment_type="service",
    )
    db.add(prod)
    db.commit()
    db.refresh(prod)
    return owner, prod


def test_service_order_skips_delivery_and_uses_fast_window(db_session, client, monkeypatch):
    owner, prod = _seed_service_store(db_session)
    monkeypatch.setattr(settings, "ESCROW_ENABLED", True, raising=False)
    monkeypatch.setattr("app.services.escrow_service.is_trusted_seller", lambda db, u: False)
    monkeypatch.setattr("app.services.escrow_service.detect_order_collusion", lambda *a, **k: None)
    monkeypatch.setattr("app.services.escrow_service.seller_velocity_hold_reason", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.invoice_payment_service.start_invoice_payment",
        AsyncMock(return_value={"authorization_url": "http://pay"}),
    )

    # No delivery_note, no GPS — a service order must still go through.
    resp = client.post(
        "/public/store/coachstore/order",
        json={
            "customer_name": "Buyer",
            "customer_phone": "+2348161111111",
            "items": [{"product_id": prod.id, "quantity": 1}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "delivery_fee" not in body

    inv = db_session.query(models.Invoice).filter_by(invoice_id=body["invoice_id"]).one()
    assert inv.amount == Decimal("20000")  # goods only
    escrow = db_session.query(models.StorefrontOrderEscrow).filter_by(invoice_id=inv.id).one()
    # Nothing ships → fast (same-state) buyer-protection window + no delivery.
    assert escrow.same_state is True
    assert escrow.requires_delivery is False
    assert not escrow.delivery_fee_kobo


def test_physical_order_still_requires_delivery_address(db_session, client, monkeypatch):
    owner, _svc = _seed_service_store(db_session)
    physical = Product(
        user_id=owner.id,
        sku="MUG-1",
        name="Branded Mug",
        description="A ceramic mug",
        image_url="http://img/m.jpg",
        selling_price=Decimal("3000"),
        is_active=True,
        track_stock=False,
        fulfilment_type="physical",
    )
    db_session.add(physical)
    db_session.commit()
    db_session.refresh(physical)

    resp = client.post(
        "/public/store/coachstore/order",
        json={
            "customer_name": "Buyer",
            "customer_phone": "+2348161111111",
            "items": [{"product_id": physical.id, "quantity": 1}],
        },
    )
    assert resp.status_code == 400
    assert "delivery address" in resp.json()["detail"].lower()


def test_public_storefront_exposes_fulfilment_type_and_cover(db_session, client, monkeypatch):
    owner, _product = _seed_service_store(db_session)
    owner.storefront_cover_url = "https://cdn.example.com/storefront-cover.webp"
    db_session.commit()
    monkeypatch.setattr("app.api.routes_storefront._presign", lambda url, **kwargs: url)

    resp = client.get("/public/store/coachstore")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["storefront_cover_url"] == owner.storefront_cover_url
    products = body["products"]
    assert products and products[0]["fulfilment_type"] == "service"
