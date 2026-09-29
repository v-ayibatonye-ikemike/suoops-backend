"""Anti-GMV-bloat guards: flagged users excluded from money metrics (A) and
low-trust large manual confirmations held for review (B)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.db.session import SessionLocal, get_db
from app.models import models
from app.models.admin_models import AdminUser
from app.services.invoice_service import build_invoice_service

_UNIQ = [0]


def _uniq():
    _UNIQ[0] += 1
    return _UNIQ[0]


def _admin(db):
    admin = AdminUser(
        email=f"gmv-admin{_uniq()}@suoops.com",
        name="GMV Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_view_analytics=True,
        can_view_users=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _seller(db, *, flagged=False, created_days=0, login_days=0):
    now = dt.datetime.now(dt.timezone.utc)
    u = models.User(
        name=f"Seller {_uniq()}",
        phone=f"+2349{_uniq():09d}",
        created_at=now - dt.timedelta(days=created_days),
        last_login=now - dt.timedelta(days=login_days),
        flagged_for_review=flagged,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _invoice(db, seller, *, amount, status, channel=None, itype="revenue"):
    cust = models.Customer(name=f"C{_uniq()}", phone=f"+2348{_uniq():09d}")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    inv = models.Invoice(
        invoice_id=f"INV-GMV-{_uniq()}",
        issuer_id=seller.id,
        customer_id=cust.id,
        amount=Decimal(str(amount)),
        status=status,
        invoice_type=itype,
        channel=channel,
        paid_at=dt.datetime.now(dt.timezone.utc) if status == "paid" else None,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


# ── Part A: flagged users excluded from GMV ──────────────────────────


def test_flagged_user_excluded_from_gmv_summary():
    db = next(get_db())
    admin = _admin(db)
    seller = _seller(db, created_days=90, login_days=1)
    _invoice(db, seller, amount=300000, status="paid")  # counts toward GMV

    client = TestClient(app)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        gmv_before = client.get("/admin/metrics/summary?period=all").json()["gmv"]
        assert gmv_before >= 300000

        seller.flagged_for_review = True
        db.commit()
        gmv_after = client.get("/admin/metrics/summary?period=all").json()["gmv"]
        assert gmv_after == gmv_before - 300000  # flagged seller's paid revenue dropped
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


# ── Part B: low-trust large manual confirmation held for review ──────


def test_low_trust_large_manual_confirm_blocked():
    db = SessionLocal()
    try:
        seller = _seller(db, created_days=1, login_days=1)  # brand-new → low trust
        inv = _invoice(db, seller, amount=600000, status="awaiting_confirmation")
        svc = build_invoice_service(db, user_id=seller.id)
        with pytest.raises(ValueError, match="held for review"):
            svc.update_status(seller.id, inv.invoice_id, "paid", updated_by_user_id=seller.id)
        db.refresh(inv)
        assert inv.status == "awaiting_confirmation"  # stayed unpaid → not in GMV
    finally:
        db.close()


def test_small_manual_confirm_allowed_for_low_trust():
    db = SessionLocal()
    try:
        seller = _seller(db, created_days=1, login_days=1)
        inv = _invoice(db, seller, amount=100000, status="awaiting_confirmation")  # < ₦500k
        svc = build_invoice_service(db, user_id=seller.id)
        out = svc.update_status(seller.id, inv.invoice_id, "paid", updated_by_user_id=seller.id)
        assert out.status == "paid"
    finally:
        db.close()


def test_trusted_seller_large_manual_confirm_allowed():
    db = SessionLocal()
    try:
        seller = _seller(db, created_days=120, login_days=1)  # old + active
        _invoice(db, seller, amount=50000, status="paid")  # prior paid history
        inv = _invoice(db, seller, amount=600000, status="awaiting_confirmation")
        svc = build_invoice_service(db, user_id=seller.id)
        out = svc.update_status(seller.id, inv.invoice_id, "paid", updated_by_user_id=seller.id)
        assert out.status == "paid"
    finally:
        db.close()


def test_force_confirm_bypasses_guard():
    db = SessionLocal()
    try:
        seller = _seller(db, created_days=1, login_days=1)
        inv = _invoice(db, seller, amount=600000, status="awaiting_confirmation")
        svc = build_invoice_service(db, user_id=seller.id)
        out = svc.update_status(seller.id, inv.invoice_id, "paid", updated_by_user_id=None, force=True)
        assert out.status == "paid"
    finally:
        db.close()


def test_admin_force_confirm_endpoint():
    db = next(get_db())
    admin = _admin(db)
    seller = _seller(db, created_days=1, login_days=1)
    inv = _invoice(db, seller, amount=600000, status="awaiting_confirmation")

    client = TestClient(app)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(f"/admin/invoices/{inv.invoice_id}/force-confirm")
        assert r.status_code == 200, r.text
        assert r.json()["status"] == "paid"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()
