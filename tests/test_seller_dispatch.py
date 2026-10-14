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
    from app.workers.tasks import escrow_tasks

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
    monkeypatch.setattr(
        escrow_tasks.book_courier, "delay",
        lambda escrow_id, invoice_id: routes_storefront._book_courier_pickup(escrow_id, invoice_id),
    )
    return inv, escrow


def request_booking(client, inv):
    return client.post(
        f"/inventory/storefront/orders/{inv.invoice_id}/mark-sent",
        files={"file": ("packed.jpg", b"test-photo", "image/jpeg")},
    )


def test_unqueued_ready_order_is_recovered_without_duplicate_booking(db_session, courier_order, monkeypatch):
    from app.workers.tasks.escrow_tasks import recover_courier_bookings

    inv, escrow = courier_order
    escrow.delivery_status = "booking_pending"
    escrow.delivery_status_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    escrow.dispatch_proof_url = "https://example.test/package.jpg"
    db_session.commit()
    book = Mock(return_value={"order_id": "RECOVERED-1"})
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    result = recover_courier_bookings()
    assert result == {"queued": 1, "queue_failed": 0, "unknown": 0}
    recover_courier_bookings()
    book.assert_called_once()
    db_session.refresh(escrow)
    assert escrow.delivery_status == "booked"
    assert escrow.seller_dispatched_at is None


def test_interrupted_booking_becomes_unknown_and_is_never_rebooked(db_session, courier_order, monkeypatch):
    from app.workers.tasks.escrow_tasks import recover_courier_bookings

    _, escrow = courier_order
    escrow.delivery_status = "booking_in_progress"
    escrow.delivery_status_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=11)
    db_session.commit()
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert recover_courier_bookings()["unknown"] == 1
    recover_courier_bookings()
    db_session.refresh(escrow)
    assert escrow.delivery_status == "booking_unknown"
    book.assert_not_called()


def test_courier_queue_failure_keeps_request_pending(db_session, client, courier_order, monkeypatch):
    from kombu.exceptions import OperationalError

    from app.workers.tasks.escrow_tasks import book_courier

    inv, escrow = courier_order
    monkeypatch.setattr(book_courier, "delay", Mock(side_effect=OperationalError("broker offline")))
    response = request_booking(client, inv)
    assert response.status_code == 200
    db_session.refresh(escrow)
    assert escrow.delivery_status == "booking_pending"
    assert escrow.shipbubble_order_id is None
    assert "processing" in response.json()["escrow"]["next_step"]


@pytest.mark.parametrize("ready", [False, True])
def test_recovery_needs_explicit_readiness_and_package_photo(db_session, courier_order, monkeypatch, ready):
    from app.workers.tasks.escrow_tasks import recover_courier_bookings

    _, escrow = courier_order
    escrow.delivery_status = "booking_pending" if ready else None
    escrow.dispatch_proof_url = None if ready else "https://example.test/package.jpg"
    escrow.delivery_status_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2)
    db_session.commit()
    book = Mock()
    monkeypatch.setattr(shipbubble, "create_shipment", book)
    assert recover_courier_bookings()["queued"] == 0
    book.assert_not_called()


def test_whatsapp_order_status_uses_verified_next_step_and_private_web_link(db_session, courier_order):
    from app.bot.whatsapp_adapter import WhatsAppHandler

    inv, escrow = courier_order
    handler = WhatsAppHandler(client=Mock(), nlp=Mock(), db=db_session)
    handler._send_order_status("234801", escrow.seller_id)
    message = handler.client.send_text.call_args.args[1]
    assert inv.invoice_id in message
    assert "package photo" in message
    assert f"/dashboard/invoices/{inv.invoice_id}" in message
    assert escrow.confirmation_code not in message
    assert "2348123456789" not in message
    handler._send_order_status("234802", escrow.seller_id + 1)
    assert inv.invoice_id not in handler.client.send_text.call_args.args[1]
    db_session.refresh(escrow)
    assert escrow.delivery_status is None


@pytest.mark.asyncio
async def test_whatsapp_orders_does_not_consume_a_product_draft(db_session, courier_order, monkeypatch):
    from app.bot.product_invoice_flow import CartSession, get_cart, save_cart
    from app.bot.whatsapp_adapter import WhatsAppHandler

    inv, escrow = courier_order
    phone = "2349000111222"
    save_cart(phone, CartSession(user_id=escrow.seller_id, step="awaiting_customer"))
    handler = WhatsAppHandler(client=Mock(), nlp=Mock(), db=db_session)
    monkeypatch.setattr(handler.invoice_processor, "_resolve_issuer_id", lambda sender: escrow.seller_id)
    await handler._handle_text_message(phone, {"text": "my orders"})
    assert inv.invoice_id in handler.client.send_text.call_args.args[1]
    assert get_cart(phone).step == "awaiting_customer"


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"requires_delivery": False}, "Complete the service"),
        ({"held_for_review": True}, "Order under review"),
        ({"delivery_status": "booking_unknown"}, "Contact support"),
        ({"delivery_status": "booked", "delivery_service_type": "dropoff"}, "drop-off station"),
        ({"delivery_status": "booked", "delivery_service_type": "pickup"}, "courier collection"),
        ({"status": "pending"}, "Do not dispatch"),
    ],
)
def test_next_step_respects_verified_order_state(courier_order, values, expected):
    from app.services.escrow_service import order_next_step

    _, escrow = courier_order
    for name, value in values.items():
        setattr(escrow, name, value)
    assert expected in order_next_step(escrow)


def test_booking_worker_and_recovery_schedule_are_registered(monkeypatch):
    from app.workers.celery_app import _create_celery, celery_app

    assert "escrow.book_courier" in celery_app.tasks
    assert "escrow.recover_courier_bookings" in celery_app.tasks
    monkeypatch.setattr(settings, "ENV", "production")
    scheduled_app = _create_celery()
    try:
        job = scheduled_app.conf.beat_schedule["escrow-recover-courier-bookings"]
        assert job["task"] == "escrow.recover_courier_bookings"
        assert job["schedule"].minute == set(range(60))
    finally:
        scheduled_app.close()


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
    assert escrow.seller_dispatched_at.replace(tzinfo=dt.timezone.utc) == dispatched_at.replace(tzinfo=dt.timezone.utc)


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
