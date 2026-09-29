"""Buyer-pays money math: the platform service fee AND any delivery fee are
charged to the BUYER on top, while the seller's invoice.amount and escrow
gross/payout stay the goods value only."""

from decimal import Decimal
from unittest.mock import AsyncMock

from app.core.config import settings
from app.models import models
from app.models.inventory_models import Product
from app.services.shipping.shipbubble import DeliveryOption


def _seed_store(db):
    owner = models.User(
        phone="+2348160000010",
        name="Store",
        business_name="Store Biz",
        bank_name="Opay",
        account_number="0123456789",
        account_name="STORE",
        paystack_subaccount_active=True,
        paystack_subaccount_code="ACCT_x",
        storefront_enabled=True,
        store_status="active",
        storefront_slug="teststore",
        storefront_state="Lagos",
        storefront_city="Ikeja",
    )
    db.add(owner)
    db.commit()
    db.refresh(owner)
    prod = Product(
        user_id=owner.id,
        sku="CAKE-1",
        name="Cake",
        description="Yummy cake",
        image_url="http://img/x.jpg",
        selling_price=Decimal("10000"),
        is_active=True,
        track_stock=False,
    )
    db.add(prod)
    db.commit()
    db.refresh(prod)
    return owner, prod


def test_delivery_fee_added_and_excluded_from_payout(db_session, client, monkeypatch):
    owner, prod = _seed_store(db_session)
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ESCROW_ENABLED", True, raising=False)

    opt = DeliveryOption(
        courier_id="gig",
        service_code="gig",
        name="GIG",
        image=None,
        amount=1500.0,
        wallet_total=1500.0,
        currency="NGN",
        delivery_eta="1-2 days",
        delivery_eta_time=None,
        service_type="pickup",
        dropoff_station=None,
    )
    import app.api.routes_storefront as rs

    monkeypatch.setattr(rs, "_shipbubble_quote", lambda db, owner, payload: {"request_token": "tok", "options": [opt]})
    monkeypatch.setattr("app.services.escrow_service.is_trusted_seller", lambda db, u: False)
    monkeypatch.setattr("app.services.escrow_service.detect_order_collusion", lambda *a, **k: None)
    monkeypatch.setattr("app.services.escrow_service.seller_velocity_hold_reason", lambda *a, **k: None)
    monkeypatch.setattr(
        "app.services.invoice_payment_service.start_invoice_payment",
        AsyncMock(return_value={"authorization_url": "http://pay"}),
    )

    resp = client.post(
        "/public/store/teststore/order",
        json={
            "customer_name": "Buyer",
            "customer_phone": "+2348161111111",
            "items": [{"product_id": prod.id, "quantity": 1}],
            "delivery_note": "12 Test St, Ikeja; blue gate",
            "delivery_courier_id": "gig",
            "delivery_service_code": "gig",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_fee"] == 1500.0
    assert body["service_fee"] == 300.0  # 3% of goods (10,000)

    inv = db_session.query(models.Invoice).filter_by(invoice_id=body["invoice_id"]).one()
    assert inv.amount == Decimal("10000")  # goods only — fee + delivery charged on top

    escrow = db_session.query(models.StorefrontOrderEscrow).filter_by(invoice_id=inv.id).one()
    assert escrow.gross_kobo == 1_000_000  # goods only — delivery excluded
    assert escrow.delivery_fee_kobo == 150_000
    assert escrow.delivery_courier == "GIG"
    assert escrow.delivery_request_token == "tok"


def _courier_option():
    return DeliveryOption(
        courier_id="gig",
        service_code="gig",
        name="GIG",
        image=None,
        amount=1500.0,
        wallet_total=1500.0,
        currency="NGN",
        delivery_eta="1-2 days",
        delivery_eta_time=None,
        service_type="pickup",
        dropoff_station=None,
    )


def test_trusted_seller_courier_order_forces_escrow(db_session, client, monkeypatch):
    """Option A: a physical-courier order ALWAYS escrows — even for a trusted
    seller — so the delivery fee is charged and retained (never split out)."""
    owner, prod = _seed_store(db_session)
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ESCROW_ENABLED", True, raising=False)

    import app.api.routes_storefront as rs

    monkeypatch.setattr(
        rs,
        "_shipbubble_quote",
        lambda db, owner, payload: {"request_token": "tok", "options": [_courier_option()]},
    )
    # Seller IS trusted — without Option A they'd settle instantly with no escrow.
    monkeypatch.setattr("app.services.escrow_service.is_trusted_seller", lambda db, u: True)
    monkeypatch.setattr("app.services.escrow_service.detect_order_collusion", lambda *a, **k: None)
    monkeypatch.setattr("app.services.escrow_service.seller_velocity_hold_reason", lambda *a, **k: None)
    start = AsyncMock(return_value={"authorization_url": "http://pay"})
    monkeypatch.setattr("app.services.invoice_payment_service.start_invoice_payment", start)

    resp = client.post(
        "/public/store/teststore/order",
        json={
            "customer_name": "Buyer",
            "customer_phone": "+2348161111111",
            "items": [{"product_id": prod.id, "quantity": 1}],
            "delivery_note": "12 Test St, Ikeja; blue gate",
            "delivery_courier_id": "gig",
            "delivery_service_code": "gig",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["delivery_fee"] == 1500.0

    inv = db_session.query(models.Invoice).filter_by(invoice_id=body["invoice_id"]).one()
    assert inv.amount == Decimal("10000")  # goods only — fee + delivery charged on top

    # The order was routed through escrow despite the seller being trusted, and
    # the payment was collected on the HOLD rail (fee retained by the platform).
    assert start.await_args.kwargs.get("hold") is True
    # Buyer charged goods + service fee + delivery = 1,000,000 + 30,000 + 150,000.
    assert start.await_args.kwargs.get("charge_amount_kobo") == 1_180_000
    escrow = db_session.query(models.StorefrontOrderEscrow).filter_by(invoice_id=inv.id).one()
    assert escrow.gross_kobo == 1_000_000  # seller paid on goods only
    assert escrow.delivery_fee_kobo == 150_000
    assert escrow.delivery_courier == "GIG"


def test_all_storefront_orders_escrow_no_instant_payout(db_session, client, monkeypatch):
    """No instant payout on storefront: even a trusted seller's non-courier order
    settles through escrow (hold-&-release), not an instant subaccount split."""
    owner, prod = _seed_store(db_session)
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "ESCROW_ENABLED", True, raising=False)

    monkeypatch.setattr("app.services.escrow_service.is_trusted_seller", lambda db, u: True)
    monkeypatch.setattr("app.services.escrow_service.detect_order_collusion", lambda *a, **k: None)
    monkeypatch.setattr("app.services.escrow_service.seller_velocity_hold_reason", lambda *a, **k: None)
    start = AsyncMock(return_value={"authorization_url": "http://pay"})
    monkeypatch.setattr("app.services.invoice_payment_service.start_invoice_payment", start)

    resp = client.post(
        "/public/store/teststore/order",
        json={
            "customer_name": "Buyer",
            "customer_phone": "+2348161111111",
            "items": [{"product_id": prod.id, "quantity": 1}],
            "delivery_note": "12 Test St, Ikeja; blue gate",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "delivery_fee" not in body

    inv = db_session.query(models.Invoice).filter_by(invoice_id=body["invoice_id"]).one()
    assert inv.amount == Decimal("10000")  # goods only, no delivery

    # Held through escrow (no instant split), even though the seller is trusted.
    assert start.await_args.kwargs.get("hold") is True
    # Buyer charged goods + service fee (no delivery) = 1,000,000 + 30,000.
    assert start.await_args.kwargs.get("charge_amount_kobo") == 1_030_000
    escrow = db_session.query(models.StorefrontOrderEscrow).filter_by(invoice_id=inv.id).one()
    assert escrow.gross_kobo == 1_000_000  # goods held for release
    assert not escrow.delivery_fee_kobo  # 0 / None — no courier on this order
