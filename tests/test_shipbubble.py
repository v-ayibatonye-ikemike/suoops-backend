"""Shipbubble courier integration — disabled-path (default) tests.

The integration ships OFF (no key). These lock in the fail-soft behaviour so the
manual dispatch flow is never affected until a key + wallet are configured.
"""

from unittest.mock import Mock

import httpx
import pytest

from app.services.shipping import shipbubble


def test_clean_name_strips_symbols_and_pads():
    # Business names with symbols/digits → clean two-word person name.
    assert shipbubble.clean_name("Family's Saloon") == "Familys Saloon"
    assert shipbubble.clean_name("IKEMIKE CREATIVE HUB LTd") == "IKEMIKE CREATIVE HUB LTd"
    assert shipbubble.clean_name("Shop 24/7", pad="Store") == "Shop Store"
    # Single word → padded to two words.
    assert shipbubble.clean_name("Tonye", pad="Buyer") == "Tonye Buyer"
    # Empty / all-symbols → safe fallback.
    assert shipbubble.clean_name("", pad="Store") == "Suoops Store"
    assert shipbubble.clean_name("123 456", pad="Buyer") == "Suoops Buyer"


def test_disabled_by_default():
    assert shipbubble.enabled() is False


def test_calls_return_none_when_disabled():
    assert shipbubble.validate_address(name="A", email="a@b.com", phone="080", address="Lagos") is None
    assert shipbubble.fetch_rates(sender_address_code=1, receiver_address_code=2, package_items=[]) is None
    with pytest.raises(shipbubble.BookingRejected, match="not configured"):
        shipbubble.create_shipment(request_token="x", courier_id="c", service_code="s")


@pytest.mark.parametrize(
    ("status", "body", "error"),
    [
        (422, {"message": "Invalid rate"}, shipbubble.BookingRejected),
        (200, {"status": "error"}, shipbubble.BookingRejected),
        (500, {"status": "error"}, shipbubble.BookingOutcomeUnknown),
        (409, {"message": "Already exists"}, shipbubble.BookingOutcomeUnknown),
        (200, {"status": "success", "data": {}}, shipbubble.BookingOutcomeUnknown),
        (200, [], shipbubble.BookingOutcomeUnknown),
        (200, None, shipbubble.BookingOutcomeUnknown),
    ],
)
def test_booking_distinguishes_rejection_from_unknown(monkeypatch, status, body, error):
    monkeypatch.setattr(shipbubble, "enabled", lambda: True)
    client = Mock()
    client.post.return_value = (
        httpx.Response(status, json=body) if body is not None else httpx.Response(status, text="<html>")
    )
    manager = Mock()
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(shipbubble, "_client", lambda: manager)
    with pytest.raises(error):
        shipbubble.create_shipment(request_token="test-token", courier_id="courier", service_code="pickup")
    assert client.post.call_count == 1


def test_booking_timeout_is_not_retryable(monkeypatch):
    monkeypatch.setattr(shipbubble, "enabled", lambda: True)
    manager = Mock()
    client = Mock()
    client.post.side_effect = httpx.ReadTimeout("Response lost")
    manager.__enter__ = Mock(return_value=client)
    manager.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(shipbubble, "_client", lambda: manager)
    with pytest.raises(shipbubble.BookingOutcomeUnknown):
        shipbubble.create_shipment(request_token="test-token", courier_id="courier", service_code="pickup")
    assert client.post.call_count == 1


def test_confirmed_does_not_mean_rider_is_on_the_way():
    assert shipbubble.normalize_status("confirmed") == "booked"
    assert shipbubble.normalize_status("pending") == "booked"
    assert shipbubble.normalize_status("pickup_enroute") == "pickup_enroute"


@pytest.mark.parametrize("order_id", ["SHIP-42", {}, True, " ", "s" * 61])
def test_booking_requires_a_usable_provider_reference(monkeypatch, order_id):
    monkeypatch.setattr(shipbubble, "enabled", lambda: True)

    def respond(request):
        assert request.url.path == "/shipping/labels"
        return httpx.Response(200, json={
            "status": "success",
            "data": {"order_id": order_id, "courier": {"name": "Test Courier"}},
        })

    monkeypatch.setattr(
        shipbubble, "_client",
        lambda: httpx.Client(base_url="https://courier.example.test", transport=httpx.MockTransport(respond)),
    )
    if order_id == "SHIP-42":
        result = shipbubble.create_shipment(request_token="test-token", courier_id="courier", service_code="pickup")
        assert result["order_id"] == "SHIP-42"
        assert result["courier"] == "Test Courier"
    else:
        with pytest.raises(shipbubble.BookingOutcomeUnknown):
            shipbubble.create_shipment(request_token="test-token", courier_id="courier", service_code="pickup")


def test_to_option_normalizes_courier():
    opt = shipbubble._to_option(
        {
            "courier_id": "gig",
            "service_code": "gig",
            "courier_name": "GIG Logistics",
            "courier_image": "http://img",
            "rate_card_amount": 2500,
            "total": 2500,
            "currency": "NGN",
            "delivery_eta": "Within 1 - 2 working days",
            "service_type": "pickup",
        }
    )
    assert opt is not None
    assert opt.name == "GIG Logistics"
    assert opt.amount == 2500
    assert opt.currency == "NGN"
