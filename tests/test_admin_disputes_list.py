"""Admin /disputes list: pagination, search, capped count."""

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
        email="disputes-list-admin@suoops.com",
        name="Disputes Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_view_users=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _order(db, seller, *, status, suffix):
    customer = models.Customer(name=f"Buyer {suffix}", phone=f"+23481200000{suffix:02d}")
    db.add(customer)
    db.commit()
    db.refresh(customer)
    inv = models.Invoice(
        invoice_id=f"INV-DISP-{suffix}",
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
        gross_kobo=300000,
        fee_kobo=9000,
        payout_kobo=291000,
    )
    db.add(esc)
    db.commit()
    return esc


def test_disputes_list_paginates_and_searches():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = models.User(name="Disp Seller", phone="+2349555222111", business_name="Disp Biz")
    db.add(seller)
    db.commit()
    db.refresh(seller)
    for i in range(3):
        _order(db, seller, status="disputed", suffix=i)

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        # Page 1 of 2 (limit 2 of 3 disputed) -> has_more.
        r = client.get("/admin/disputes?status_filter=disputed&limit=2&skip=0")
        assert r.status_code == 200, r.text
        body = r.json()
        assert len(body["disputes"]) == 2
        assert body["has_more"] is True
        assert body["total"] == 3
        assert body["total_capped"] is False

        # Page 2 -> the remaining one, no more.
        r2 = client.get("/admin/disputes?status_filter=disputed&limit=2&skip=2")
        assert len(r2.json()["disputes"]) == 1
        assert r2.json()["has_more"] is False

        # Search by invoice public id narrows to one.
        rs = client.get("/admin/disputes?status_filter=disputed&search=INV-DISP-1")
        assert rs.status_code == 200
        got = rs.json()["disputes"]
        assert len(got) == 1
        assert got[0]["invoice_public_id"] == "INV-DISP-1"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()
