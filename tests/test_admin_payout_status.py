"""Admin dispute payout-status endpoint + derived payout_state tests."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.db.session import get_db
from app.models import models
from app.models.admin_models import AdminUser


def _admin(db):
    admin = AdminUser(
        email="dispute-admin@suoops.com",
        name="Dispute Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_manage_tickets=True,
        can_view_users=True,
        can_view_analytics=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _order(db, *, status="held", transfer_reference=None, charge_reference=None, gross_kobo=300000):
    seller = models.User(name="Payout Seller", phone="+2349555000111")
    db.add(seller)
    db.commit()
    db.refresh(seller)
    customer = models.Customer(name="Payout Buyer", phone="+2348123450000")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    inv = models.Invoice(
        invoice_id=f"INV-PAYOUT-{status}-{transfer_reference or 'x'}-{gross_kobo}",
        issuer_id=seller.id,
        customer_id=customer.id,
        amount=Decimal("3000"),
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
        status=status,
        gross_kobo=gross_kobo,
        fee_kobo=9000,
        payout_kobo=int(gross_kobo * 0.97),
        transfer_reference=transfer_reference,
        charge_reference=charge_reference,
    )
    db.add(esc)
    db.commit()
    db.refresh(esc)
    return esc


def test_payout_status_none_when_no_transfer():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    esc = _order(db, status="held", transfer_reference=None)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get(f"/admin/disputes/{esc.id}/payout-status")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "none"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_payout_status_paid_when_released():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    esc = _order(db, status="released", transfer_reference="ESCROWREL-9")
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get(f"/admin/disputes/{esc.id}/payout-status")
        assert r.status_code == 200, r.text
        assert r.json()["state"] == "paid"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_payout_status_live_poll_normalizes_successful(monkeypatch):
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    esc = _order(db, status="held", transfer_reference="ESCROWREL-42")

    import app.services.payouts as payouts

    class _Prov:
        name = "flutterwave"

        def transfer_status(self, reference):
            return "successful"

    monkeypatch.setattr(payouts, "get_payout_provider", lambda: _Prov())
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get(f"/admin/disputes/{esc.id}/payout-status")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "paid"  # 'successful' normalized to 'paid'
        assert body["reference"] == "ESCROWREL-42"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_resolve_always_requires_stepup_otp():
    """EVERY refund/release (even a small amount) is rejected without an OTP."""
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    esc = _order(db, status="held", gross_kobo=300000)  # ₦3,000 (small)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(
            f"/admin/disputes/{esc.id}/resolve",
            json={"action": "release"},
            headers={"Authorization": "Bearer test"},
        )
        assert r.status_code == 428, r.text
        assert "code" in r.json()["detail"].lower()
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_retry_payout_resends_on_correct_rail_and_finalizes(monkeypatch):
    """Retry clears a stale (wrong-rail) reference and pays out fresh on the
    order's collecting rail, finalizing the hold on confirmed success."""
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    esc = _order(
        db,
        status="held",
        transfer_reference="ESCROWREL-STALE",  # burned on a different (failed) rail
        charge_reference="INVPAY-RETRY-1",
    )

    import app.services.escrow_service as escrow_mod
    import app.services.payouts as payouts
    from app.services.payouts.base import PayoutResult

    class _FW:
        name = "flutterwave"

        def transfer_status(self, reference):
            return "unknown"

        def transfer(self, db, *, seller, amount_kobo, reference, reason):
            return PayoutResult(ok=True, reference=reference, provider="flutterwave", status="successful")

        def transfer_exists(self, reference):
            return True

    monkeypatch.setattr(escrow_mod, "_collector_for_charge", lambda db, ref: "flutterwave")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: _FW())
    # OTP step-up is exercised separately; bypass it here to focus on the
    # retry/reconcile behavior.
    import app.api.routes_admin as routes_admin

    monkeypatch.setattr(routes_admin, "_require_money_stepup", lambda *a, **k: None)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(f"/admin/disputes/{esc.id}/retry-payout")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["state"] == "paid"
        assert body["escrow_status"] == "released"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()
