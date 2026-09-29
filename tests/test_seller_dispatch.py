"""Seller dispatch (send-out) proof endpoint tests.

Covers the seller-protection handoff step: a seller marks a held storefront
order 'sent out' with a courier tracking code + note, which persists dispatch
fields, posts a system notice to the order thread, and surfaces the 'sent out'
status to the buyer via the delivery-code thread view.
"""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.api import routes_auth
from app.api.main import app
from app.db.session import get_db
from app.models import models


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
