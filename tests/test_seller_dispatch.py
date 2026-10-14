"""Seller dispatch (send-out) proof endpoint tests.

Covers the seller-protection handoff step: a seller marks a held storefront
order 'sent out' with a courier tracking code + note, which persists dispatch
fields, posts a system notice to the order thread, and surfaces the 'sent out'
status to the buyer via the delivery-code thread view.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from app.api import routes_auth, routes_storefront
from app.api.main import app
from app.core.config import settings
from app.db.session import get_db
from app.models import models
from app.services.shipping import shipbubble


def _make_held_order(db):
    seller = models.User(
        name="Dispatch Seller",
        phone="+2349000111222",
        storefront_slug="dispatch-shop",
        storefront_enabled=True,
        store_status="active",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)

    customer = models.Customer(name="Dispatch Buyer", phone="+2348123456789")
    db.add(customer)
    db.commit()
    db.refresh(customer)

    inv = models.Invoice(
        invoice_id="INV-DISPATCH-1",
        issuer_id=seller.id,
        customer_id=customer.id,
        amount=Decimal("5000"),
        status="paid",
        invoice_type="revenue",
        channel="storefront",
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)

    esc = models.StorefrontOrderEscrow(
        invoice_id=inv.id,
        seller_id=seller.id,
        status="held",
        same_state=True,
        gross_kobo=500000,
        fee_kobo=15000,
        payout_kobo=485000,
        confirmation_code="654321",
    )
    db.add(esc)
    db.commit()
    db.refresh(esc)
    return seller, inv, esc


def test_mark_sent_persists_dispatch_and_notifies_buyer(monkeypatch):
    client = TestClient(app)
    db = next(get_db())
    seller, inv, esc = _make_held_order(db)

    # A photo is required — stub the S3 upload so the test doesn't hit the network.
    import app.api.routes_storefront as rs

    async def _fake_save(escrow, file, *, prefix):  # noqa: ANN001
        return f"https://s3.example/{prefix}/{escrow.id}.jpg"

    monkeypatch.setattr(rs, "_save_proof_photo", _fake_save)

    app.dependency_overrides[routes_auth.get_current_user_id] = lambda: seller.id
    try:
        r = client.post(
            f"/inventory/storefront/orders/{inv.invoice_id}/mark-sent",
            data={"tracking": "GIG-ABC123", "note": "Sealed blue box, 2 items"},
            files={"file": ("packed.jpg", b"\xff\xd8\xff\xe0stub", "image/jpeg")},
            headers={"Authorization": "Bearer test"},
        )
        assert r.status_code == 200, r.text
        summary = r.json()["escrow"]
        assert summary["dispatched_at"] is not None
        assert summary["dispatch_tracking"] == "GIG-ABC123"
        assert summary["dispatch_note"] == "Sealed blue box, 2 items"
        assert summary["dispatch_proof_url"]

        db.refresh(esc)
        assert esc.seller_dispatched_at is not None
        assert esc.dispatch_tracking == "GIG-ABC123"

        # A system notice was posted to the order thread (never redacted).
        sys_msgs = (
            db.query(models.OrderMessage)
            .filter(
                models.OrderMessage.escrow_id == esc.id,
                models.OrderMessage.sender_role == "system",
            )
            .all()
        )
        assert len(sys_msgs) == 1
        assert "sent out" in sys_msgs[0].body_redacted.lower()
        assert "GIG-ABC123" in sys_msgs[0].body_redacted
        assert sys_msgs[0].blocked is False
    finally:
        app.dependency_overrides.pop(routes_auth.get_current_user_id, None)
        db.close()


def test_buyer_thread_shows_dispatch_status():
    client = TestClient(app)
    db = next(get_db())
    seller, inv, esc = _make_held_order(db)
    esc.seller_dispatched_at = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
    esc.dispatch_tracking = "RIDER-77"
    db.commit()
    try:
        # Buyer opens their thread with the delivery code → sees 'sent out'.
        r = client.post(
            "/public/store/dispatch-shop/messages/list",
            json={"code": "654321"},
        )
        assert r.status_code == 200, r.text
        order = r.json()["order"]
        assert order["dispatched_at"] is not None
        assert order["dispatch_tracking"] == "RIDER-77"
    finally:
        db.close()


@pytest.fixture
def courier_order(db_session, monkeypatch):
    seller, inv, escrow = _make_held_order(db_session)
    escrow.delivery_courier = "Test Courier"
    escrow.delivery_courier_id = "courier-test"
    escrow.delivery_service_code = "pickup-test"
    escrow.delivery_service_type = "pickup"
    escrow.delivery_request_token = "request-test"
    db_session.commit()
    monkeypatch.setitem(app.dependency_overrides, routes_auth.get_current_user_id, lambda: seller.id)
    monkeypatch.setattr(settings, "SHIPBUBBLE_ENABLED", True)
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", True)
    monkeypatch.setattr(settings, "SHIPBUBBLE_API_KEY", "test-key")
    monkeypatch.setattr(routes_storefront, "_presign", lambda url: url)

    async def save_photo(escrow, file, *, prefix):
        return f"https://example.test/{prefix}/{escrow.id}.jpg"

    monkeypatch.setattr(routes_storefront, "_save_proof_photo", save_photo)
    return inv, escrow


def request_booking(client, inv):
    return client.post(
        f"/inventory/storefront/orders/{inv.invoice_id}/mark-sent",
        files={"file": ("packed.jpg", b"test-photo", "image/jpeg")},
    )


@pytest.mark.parametrize("service_type", ["pickup", "dropoff"])
def test_booking_is_confirmed_without_pretending_dispatch(db_session, client, courier_order, monkeypatch, service_type):
    inv, escrow = courier_order
    escrow.delivery_service_type = service_type
    db_session.commit()
    book = Mock(return_value={"order_id": "SHIP-1", "tracking_url": "https://example.test/track"})
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    response = request_booking(client, inv)
    assert response.status_code == 200
    assert response.json()["escrow"]["delivery_status"] == "booking_pending"
    assert response.json()["escrow"]["dispatched_at"] is None
    db_session.refresh(escrow)
    assert escrow.delivery_status == "booked"
    assert escrow.shipbubble_order_id == "SHIP-1"
    assert escrow.delivery_booked_at is not None
    assert escrow.seller_dispatched_at is None
    summary = client.get(f"/inventory/storefront/orders/{inv.invoice_id}").json()["escrow"]
    assert ("drop-off" if service_type == "dropoff" else "pickup") in summary["delivery_status_label"]
    request_booking(client, inv)
    routes_storefront._book_courier_pickup(escrow.id, inv.invoice_id)
    assert book.call_count == 1
    messages = db_session.query(models.OrderMessage).filter_by(escrow_id=escrow.id).all()
    assert len(messages) == 1
    assert "booking confirmed" in messages[0].body_redacted
    assert "sent out" not in messages[0].body_redacted


@pytest.mark.parametrize(
    ("outcome", "status", "retry"),
    [
        (shipbubble.BookingRejected("Rejected"), "booking_failed", True),
        (shipbubble.BookingOutcomeUnknown("Timeout"), "booking_unknown", False),
        (None, "booking_unknown", False),
    ],
)
def test_booking_failures_are_visible_and_only_known_rejections_can_retry(
    db_session, client, courier_order, monkeypatch, outcome, status, retry,
):
    inv, escrow = courier_order
    book = Mock(side_effect=outcome) if isinstance(outcome, Exception) else Mock(return_value=outcome)
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert request_booking(client, inv).status_code == 200
    db_session.refresh(escrow)
    assert escrow.delivery_status == status
    assert escrow.seller_dispatched_at is None
    assert escrow.shipbubble_order_id is None
    assert escrow.dispatch_proof_url
    buyer = client.post("/public/store/dispatch-shop/messages/list", json={"code": "654321"}).json()["order"]
    assert buyer["delivery_status"] == status
    assert buyer["dispatched_at"] is None
    book.side_effect = None
    book.return_value = {"order_id": "SHIP-RETRY"}
    # Retrying a known rejection can reuse the saved proof; unknowns cannot rebook.
    response = client.post(f"/inventory/storefront/orders/{inv.invoice_id}/mark-sent")
    assert response.status_code == 200
    assert book.call_count == (2 if retry else 1)
    db_session.refresh(escrow)
    assert escrow.delivery_status == ("booked" if retry else "booking_unknown")


@pytest.mark.parametrize("status", ["booking_pending", "booking_in_progress", "booking_unknown"])
def test_repeated_pending_requests_do_not_schedule_another_booking(
    db_session, client, courier_order, monkeypatch, status,
):
    inv, escrow = courier_order
    escrow.delivery_status = status
    db_session.commit()
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert request_booking(client, inv).json()["escrow"]["delivery_status"] == status
    book.assert_not_called()


@pytest.mark.parametrize("state", ["released", "refunded", "disputed", "pending"])
def test_nonheld_order_cannot_book(db_session, client, courier_order, monkeypatch, state):
    inv, escrow = courier_order
    escrow.status = state
    db_session.commit()
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert request_booking(client, inv).status_code == 409
    book.assert_not_called()


def test_booking_requires_photo_configuration_and_complete_selection(db_session, client, courier_order, monkeypatch):
    inv, escrow = courier_order
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert client.post(f"/inventory/storefront/orders/{inv.invoice_id}/mark-sent").status_code == 400
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", False)
    assert request_booking(client, inv).status_code == 503
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", True)
    escrow.delivery_service_code = None
    db_session.commit()
    assert request_booking(client, inv).status_code == 409
    book.assert_not_called()


@pytest.mark.parametrize("field", ["held_for_review", "confirmed_at"])
def test_reviewed_or_buyer_confirmed_orders_cannot_book(db_session, client, courier_order, monkeypatch, field):
    inv, escrow = courier_order
    setattr(escrow, field, True if field == "held_for_review" else dt.datetime.now(dt.timezone.utc))
    db_session.commit()
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert request_booking(client, inv).status_code == 409
    escrow.delivery_status = "booking_pending"
    db_session.commit()
    routes_storefront._book_courier_pickup(escrow.id, inv.invoice_id)
    book.assert_not_called()


def test_legacy_unconfirmed_booking_is_not_blindly_retried(db_session, client, courier_order, monkeypatch):
    from app.api.routes_invoice import _fulfilment

    inv, escrow = courier_order
    escrow.seller_dispatched_at = dt.datetime.now(dt.timezone.utc)
    db_session.commit()
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    summary = client.get(f"/inventory/storefront/orders/{inv.invoice_id}").json()["escrow"]
    assert summary["delivery_status"] == "booking_unknown"
    assert routes_storefront._buyer_order_view(escrow)["delivery_status"] == "booking_unknown"
    assert _fulfilment(escrow)[0] == "booking_unknown"
    assert request_booking(client, inv).json()["escrow"]["delivery_status"] == "booking_unknown"
    book.assert_not_called()


def test_courier_webhooks_establish_dispatch_and_cannot_regress(db_session, courier_order):
    from app.api.routes_webhooks import _apply_shipbubble_update

    _, escrow = courier_order
    escrow.shipbubble_order_id, escrow.delivery_status = "SHIP-1", "booked"
    db_session.commit()
    _apply_shipbubble_update(db_session, "SHIP-1", "confirmed", {}, {})
    assert escrow.delivery_status == "booked"
    assert escrow.seller_dispatched_at is None
    _apply_shipbubble_update(db_session, "SHIP-1", "in_transit", {}, {})
    dispatched_at = escrow.seller_dispatched_at
    assert dispatched_at is not None
    _apply_shipbubble_update(db_session, "SHIP-1", "pending", {}, {})
    assert escrow.delivery_status == "in_transit"
    _apply_shipbubble_update(db_session, "SHIP-1", "delivered", {}, {})
    assert escrow.courier_delivered_at is not None
    _apply_shipbubble_update(db_session, "SHIP-1", "picked_up", {}, {})
    assert escrow.delivery_status == "delivered"
    assert escrow.seller_dispatched_at == dispatched_at


@pytest.mark.parametrize("status", [None, "booking_pending", "booking_failed", "booking_unknown", "booked"])
def test_unconfirmed_or_undelivered_courier_orders_cannot_auto_pay_out(
    db_session, courier_order, monkeypatch, status,
):
    from app.services import escrow_service
    from app.workers.tasks.escrow_tasks import release_due_escrow_orders

    _, escrow = courier_order
    escrow.delivery_status = status
    escrow.release_due_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    if status == "booked":
        escrow.shipbubble_order_id = "SHIP-1"
    db_session.commit()
    release = Mock(return_value=True)
    monkeypatch.setattr(settings, "ESCROW_BATCH_PAYOUTS", False)
    monkeypatch.setattr(escrow_service, "release_escrow", release)
    assert release_due_escrow_orders.run()["released"] == 0
    release.assert_not_called()
    db_session.refresh(escrow)
    assert escrow.held_for_review


@pytest.mark.parametrize("courier_confirmed", [False, True])
def test_buyer_confirmed_and_manual_orders_keep_existing_payout_path(
    db_session, courier_order, monkeypatch, courier_confirmed,
):
    from app.services import escrow_service
    from app.workers.tasks.escrow_tasks import release_due_escrow_orders

    _, escrow = courier_order
    escrow.release_due_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    if courier_confirmed:
        escrow.confirmed_at = dt.datetime.now(dt.timezone.utc)
    else:
        escrow.delivery_courier_id = escrow.delivery_courier = None
    db_session.commit()
    release = Mock(return_value=True)
    monkeypatch.setattr(settings, "ESCROW_BATCH_PAYOUTS", False)
    monkeypatch.setattr(escrow_service, "release_escrow", release)
    assert release_due_escrow_orders.run()["released"] == 1
    release.assert_called_once()
