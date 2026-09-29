from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.main import app
from app.db.session import get_db
from app.models.models import Customer, Invoice, SubscriptionPlan, User


def _setup_entities(db):
    user = User(phone="+234000000001", name="Test Biz")
    # Tax reports require PRO plan.
    user.plan = SubscriptionPlan.PRO
    db.add(user)
    db.commit()
    db.refresh(user)
    cust = Customer(name="CSV Customer")
    db.add(cust)
    db.commit()
    db.refresh(cust)
    inv1 = Invoice(
        invoice_id="INV-CSV-1",
        issuer_id=user.id,
        customer_id=cust.id,
        amount=Decimal("10000"),
        discount_amount=Decimal("1000"),
        status="paid",
        vat_rate=7.5,
        vat_amount=Decimal("675"),
        vat_category="standard",
    )
    inv2 = Invoice(
        invoice_id="INV-CSV-2",
        issuer_id=user.id,
        customer_id=cust.id,
        amount=Decimal("5000"),
        status="paid",
        vat_rate=7.5,
        vat_amount=Decimal("375"),
        vat_category="standard",
    )
    db.add_all([inv1, inv2])
    db.commit()
    return user


def test_monthly_tax_report_csv_export(monkeypatch):
    client = TestClient(app)
    db = next(get_db())
    user = _setup_entities(db)
    from app.api import routes_auth

    app.dependency_overrides[routes_auth.get_current_user_id] = lambda: user.id
    headers = {"Authorization": "Bearer test"}
    r = client.post("/tax/reports/generate?year=2025&month=10&basis=paid", headers=headers)
    assert r.status_code == 200, r.text
    rcsv = client.get("/tax/reports/2025/10/csv?basis=paid", headers=headers)
    assert rcsv.status_code == 200, rcsv.text
    payload = rcsv.json()
    assert "csv_url" in payload
    assert payload["basis"] == "paid"


def test_fresh_pdf_url_resigns_from_key(monkeypatch):
    """Download must re-sign from the S3 key so the emailed/stored presigned URL
    (which expires ~1h) never surfaces to the user as 'Request has expired'."""
    from app.api.routes_tax import reports as tax_reports

    seen = {}
    monkeypatch.setattr(
        "app.storage.s3_client.s3_client.extract_key_from_url",
        lambda url: "tax-reports/1/2025-10.pdf",
    )

    def fake_presign(key, expires_in=None):
        seen["key"] = key
        return "https://s3.example.com/tax-reports/1/2025-10.pdf?X-Amz-Signature=FRESH"

    monkeypatch.setattr("app.storage.s3_client.s3_client.get_presigned_url", fake_presign)

    stale = "https://s3.example.com/tax-reports/1/2025-10.pdf?X-Amz-Signature=STALE"
    fresh = tax_reports._fresh_pdf_url(stale)
    assert "FRESH" in fresh and "STALE" not in fresh
    assert seen["key"] == "tax-reports/1/2025-10.pdf"


def test_fresh_pdf_url_falls_back(monkeypatch):
    """Falls back to the stored value when S3 can't re-sign (local dev / bad key)."""
    from app.api.routes_tax import reports as tax_reports

    monkeypatch.setattr("app.storage.s3_client.s3_client.extract_key_from_url", lambda url: None)
    stored = "http://localhost/storage/tax-reports/1/2025-10.pdf"
    assert tax_reports._fresh_pdf_url(stored) == stored
    assert tax_reports._fresh_pdf_url(None) is None
