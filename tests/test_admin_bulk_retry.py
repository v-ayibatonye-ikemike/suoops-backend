"""Bulk 'retry all held payouts for a business' admin endpoint."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from fastapi.testclient import TestClient

import app.api.routes_admin as routes_admin
import app.services.payouts as payouts
from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.db.session import get_db
from app.models import models
from app.models.admin_models import AdminUser


def _admin(db):
    admin = AdminUser(
        email="bulk-admin@suoops.com",
        name="Bulk Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_view_users=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _fake_provider(status="successful"):
    from app.services.payouts.base import PayoutProvider, PayoutResult

    class FakeProvider(PayoutProvider):
        name = "fake"

        def __init__(self):
            self.sent = []

        def transfer(self, db, *, seller, amount_kobo, reference, reason):
            self.sent.append((reference, amount_kobo))
            return PayoutResult(ok=True, reference=reference, provider=self.name, status=status)

        def transfer_status(self, reference):
            return status

    return FakeProvider()


def _seller_with_held(db, n):
    seller = models.User(
        name="Held Seller",
        phone="+2349555222333",
        account_number="0123456789",
        bank_name="GTBank",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)
    past = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=2)
    for i in range(n):
        cust = models.Customer(name=f"B{i}", phone=f"+234870000{i:04d}")
        db.add(cust)
        db.commit()
        db.refresh(cust)
        inv = models.Invoice(
            invoice_id=f"INV-HELD-{seller.id}-{i}",
            issuer_id=seller.id,
            customer_id=cust.id,
            amount=Decimal("5000"),
            status="paid",
            invoice_type="revenue",
            channel="storefront",
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        db.add(
            models.StorefrontOrderEscrow(
                invoice_id=inv.id,
                seller_id=seller.id,
                status="held",
                gross_kobo=500000,
                fee_kobo=15000,
                payout_kobo=500000,
                settle_at=past,
                charge_reference=None,
            )
        )
    db.commit()
    return seller


def test_bulk_retry_releases_all_held(monkeypatch):
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = _seller_with_held(db, 3)

    fake = _fake_provider(status="successful")
    monkeypatch.setattr(payouts, "get_payout_provider", lambda: fake)
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)
    monkeypatch.setattr(routes_admin, "_require_money_stepup", lambda *a, **k: None)

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(f"/admin/businesses/{seller.id}/retry-held-payouts", json={"otp": "x"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["total_held"] == 3
        assert body["released"] == 3
        assert body["failed"] == 0
        assert body["total_amount"] == 15000.0  # 3 × ₦5,000
        # CONSOLIDATED: 3 orders → ONE transfer, not three.
        assert len(fake.sent) == 1
        assert fake.sent[0][1] == 1500000  # summed payout kobo (3 × 500000)
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_bulk_retry_pending_counts_as_retried(monkeypatch):
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = _seller_with_held(db, 2)

    fake = _fake_provider(status="pending")  # queued, not yet disbursed
    monkeypatch.setattr(payouts, "get_payout_provider", lambda: fake)
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)
    monkeypatch.setattr(routes_admin, "_require_money_stepup", lambda *a, **k: None)

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(f"/admin/businesses/{seller.id}/retry-held-payouts", json={"otp": "x"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["retried"] == 2  # sent, awaiting confirmation
        assert body["released"] == 0
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_bulk_retry_404_unknown_business(monkeypatch):
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    monkeypatch.setattr(routes_admin, "_require_money_stepup", lambda *a, **k: None)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post("/admin/businesses/99999999/retry-held-payouts", json={"otp": "x"})
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_disputes_by_business_groups_held_counts():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = _seller_with_held(db, 4)  # 4 held orders for one business

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get("/admin/disputes/by-business")
        assert r.status_code == 200, r.text
        body = r.json()
        mine = [g for g in body["businesses"] if g["seller_id"] == seller.id]
        assert mine, "business not grouped"
        g = mine[0]
        assert g["held_count"] == 4
        assert g["disputed_count"] == 0
        assert g["held_total_naira"] == 20000.0  # 4 × ₦5,000
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()
