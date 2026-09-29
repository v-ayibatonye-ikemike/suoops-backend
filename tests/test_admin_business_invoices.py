"""Admin business-invoices drill-down + storefront owner-trace fields."""

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
        email="bizinv-admin@suoops.com",
        name="Biz Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_view_users=True,
        can_view_analytics=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def _seller_with_invoices(db):
    seller = models.User(
        name="Store Owner",
        phone="+2349555111222",
        business_name="Owner Biz",
        storefront_slug="owner-biz",
        email="owner@example.com",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)
    cust = models.Customer(name="A Buyer", phone="+2348123999000")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    # 2 paid + 1 pending revenue invoice, plus an expense (excluded by default).
    specs = [
        ("INV-BI-1", "paid", "revenue", "5000"),
        ("INV-BI-2", "paid", "revenue", "3000"),
        ("INV-BI-3", "pending", "revenue", "2000"),
        ("INV-BI-EXP", "paid", "expense", "1000"),
    ]
    for iid, status, itype, amt in specs:
        db.add(
            models.Invoice(
                invoice_id=iid,
                issuer_id=seller.id,
                customer_id=cust.id,
                amount=Decimal(amt),
                status=status,
                invoice_type=itype,
                channel="storefront",
            )
        )
    db.commit()
    return seller


def test_business_invoices_lists_amounts_and_rollups():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = _seller_with_invoices(db)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get(f"/admin/businesses/{seller.id}/invoices")
        assert r.status_code == 200, r.text
        body = r.json()
        # Revenue-only by default → 3 invoices, expense excluded.
        assert body["total"] == 3
        assert len(body["invoices"]) == 3
        assert body["total_amount"] == 10000.0  # 5000+3000+2000
        assert body["paid_amount"] == 8000.0
        assert body["pending_amount"] == 2000.0
        # Newest first + customer name resolved + amounts present.
        first = body["invoices"][0]
        assert first["customer_name"] == "A Buyer"
        assert first["amount"] > 0
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_business_invoices_all_includes_expenses():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = _seller_with_invoices(db)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get(f"/admin/businesses/{seller.id}/invoices?invoice_type=all")
        assert r.status_code == 200, r.text
        assert r.json()["total"] == 4  # includes the expense row
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_business_invoices_404_for_unknown_user():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get("/admin/businesses/99999999/invoices")
        assert r.status_code == 404
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_storefronts_expose_owner_contact():
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = _seller_with_invoices(db)  # has a storefront_slug
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get("/admin/storefronts?search=owner-biz")
        assert r.status_code == 200, r.text
        stores = r.json()["storefronts"]
        mine = [s for s in stores if s["id"] == seller.id]
        assert mine, "store not returned"
        s = mine[0]
        assert s["owner_phone"] == "+2349555111222"
        assert s["owner_email"] == "owner@example.com"
        # Bare store (no logo/pay/product/description/location) → not-live reasons.
        reasons = s["not_live_reasons"]
        assert "No logo" in reasons
        assert "Online payments not set up" in reasons
        assert "No store description" in reasons
        assert "No location set" in reasons
        assert any("shopper-ready product" in r for r in reasons)
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_business_intelligence_caps_outlier_revenue():
    """A junk invoice above the ₦50M ceiling is excluded from total_revenue and
    the business row is flagged has_outlier_invoice — so BI matches the platform
    metrics' cap instead of showing billions."""
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = models.User(name="Outlier Biz", phone="+2349555777888")
    db.add(seller)
    db.commit()
    db.refresh(seller)
    cust = models.Customer(name="Buyer", phone="+2348123777888")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    # One normal ₦5,000 paid invoice + one ₦100,000,000 junk invoice (> ₦50M cap).
    for iid, amt, status in [("INV-OUT-1", "5000", "paid"), ("INV-OUT-2", "100000000", "pending")]:
        db.add(
            models.Invoice(
                invoice_id=iid,
                issuer_id=seller.id,
                customer_id=cust.id,
                amount=Decimal(amt),
                status=status,
                invoice_type="revenue",
            )
        )
    db.commit()

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get("/admin/businesses?search=Outlier%20Biz")
        assert r.status_code == 200, r.text
        mine = [b for b in r.json()["businesses"] if b["id"] == seller.id]
        assert mine, "business not returned"
        b = mine[0]
        assert b["total_revenue"] == 5000.0  # ₦100M junk invoice excluded
        assert b["has_outlier_invoice"] is True
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_business_invoices_payment_method():
    """Drill-down shows how each paid invoice was collected: online (webhook),
    manual (self-marked), or storefront."""
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = models.User(name="PM Biz", phone="+2349555444333")
    db.add(seller)
    db.commit()
    db.refresh(seller)
    cust = models.Customer(name="PM Buyer", phone="+2348123444333")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    specs = [
        # (invoice_id, channel, status_updated_by, expected method)
        ("INV-PM-ONLINE", None, None, "online"),
        ("INV-PM-MANUAL", None, seller.id, "manual"),
        ("INV-PM-STORE", "storefront", None, "storefront"),
    ]
    for iid, channel, updater, _exp in specs:
        db.add(
            models.Invoice(
                invoice_id=iid,
                issuer_id=seller.id,
                customer_id=cust.id,
                amount=Decimal("2000000"),
                status="paid",
                invoice_type="revenue",
                channel=channel,
                status_updated_by_user_id=updater,
            )
        )
    db.commit()

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get(f"/admin/businesses/{seller.id}/invoices")
        assert r.status_code == 200, r.text
        by_id = {inv["invoice_id"]: inv["payment_method"] for inv in r.json()["invoices"]}
        assert by_id["INV-PM-ONLINE"] == "online"
        assert by_id["INV-PM-MANUAL"] == "manual"
        assert by_id["INV-PM-STORE"] == "storefront"
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()


def test_business_intelligence_flags_duplicate_invoices():
    """Two identical large paid invoices to the same customer → duplicate flag."""
    client = TestClient(app)
    db = next(get_db())
    admin = _admin(db)
    seller = models.User(name="Dup Biz", phone="+2349555888777")
    db.add(seller)
    db.commit()
    db.refresh(seller)
    cust = models.Customer(name="Mariam", phone="+2348123888777")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    for iid in ("INV-DUP-1", "INV-DUP-2"):
        db.add(
            models.Invoice(
                invoice_id=iid,
                issuer_id=seller.id,
                customer_id=cust.id,
                amount=Decimal("8000000"),  # ₦8M each, same customer
                status="paid",
                invoice_type="revenue",
            )
        )
    db.commit()

    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.get("/admin/businesses?search=Dup%20Biz")
        assert r.status_code == 200, r.text
        mine = [b for b in r.json()["businesses"] if b["id"] == seller.id]
        assert mine, "business not returned"
        assert "duplicate_invoices" in mine[0]["risk_flags"]
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
        db.close()
