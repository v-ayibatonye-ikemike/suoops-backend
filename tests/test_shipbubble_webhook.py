"""Shipbubble webhook — signature verification + acknowledge."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import settings


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()


def test_shipbubble_webhook_verifies_and_acks(monkeypatch):
    monkeypatch.setattr(settings, "SHIPBUBBLE_WEBHOOK_SECRET", "whsec_test", raising=False)
    client = TestClient(app)
    payload = {
        "event": "shipment.status.changed",
        "order_id": "SB-TESTORDER1",
        "status": "in_transit",
        "courier": {"name": "GIG", "tracking_code": "123456"},
    }
    body = json.dumps(payload).encode()
    resp = client.post(
        "/webhooks/shipbubble",
        content=body,
        headers={"x-ship-signature": _sign(body, "whsec_test")},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["order_id"] == "SB-TESTORDER1"


def test_shipbubble_webhook_rejects_bad_signature(monkeypatch):
    monkeypatch.setattr(settings, "SHIPBUBBLE_WEBHOOK_SECRET", "whsec_test", raising=False)
    client = TestClient(app)
    body = json.dumps({"event": "x", "order_id": "SB-1"}).encode()
    resp = client.post(
        "/webhooks/shipbubble",
        content=body,
        headers={"x-ship-signature": "deadbeef"},
    )
    assert resp.status_code == 401
