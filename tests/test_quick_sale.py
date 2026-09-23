"""Quick Sale (walk-in / in-person sale) tests.

Covers:
1. Service-level flow: create_invoice(channel="quick_sale") + immediate
   update_status(..., "paid") records a paid sale with no customer contact
   required, no pre-payment PDF, and a payment_method tag.
2. Two rapid, identically-priced/described quick sales are NOT deduped into
   one invoice (the generic dedup-on-double-submit guard must not merge
   distinct walk-in sales).
3. Endpoint-level: POST /invoices/quick-sale returns a paid invoice in one
   call.
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.main import app
from app.db.base_class import Base
from app.models import models
from app.services.invoice_service import InvoiceService
from app.services.pdf_service import PDFService
from app.storage.s3_client import S3Client

engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


class _DummyPDF(PDFService):  # type: ignore[misc]
    def __init__(self):
        self.client = S3Client()

    def generate_invoice_pdf(self, invoice, bank_details=None, logo_url=None, user_plan=None):  # noqa: D401
        return f"http://pdf.local/invoice/{invoice.invoice_id}.pdf"

    def generate_receipt_pdf(self, invoice):  # noqa: D401
        return f"http://pdf.local/receipt/{invoice.invoice_id}.pdf"


def _make_user(session, *, wallet_balance_kobo: int = 10_000_000):
    suffix = dt.datetime.now(dt.UTC).strftime("%H%M%S%f")
    user = models.User(
        phone=f"+234998{suffix}",
        name="Tester",
        email=f"quicksale+{suffix}@example.com",
        business_name="Corner Shop",
        bank_name="Test Bank",
        account_number="0123456789",
        account_name="TESTER",
        wallet_balance_kobo=wallet_balance_kobo,
    )
    session.add(user)
    session.commit()
    return user


def _quick_sale_data(amount=2000, description="Bag of rice", payment_method="cash"):
    return {
        "amount": amount,
        "currency": "NGN",
        "invoice_type": "revenue",
        "channel": "quick_sale",
        "payment_method": payment_method,
        "customer_name": "Walk-in Customer",
        "lines": [{"description": description, "quantity": 1, "unit_price": amount}],
    }


def test_quick_sale_created_then_marked_paid_has_no_precreated_pdf():
    session = SessionLocal()
    user = _make_user(session)
    service = InvoiceService(session, _DummyPDF())

    invoice = service.create_invoice(
        user.id, _quick_sale_data(), created_by_user_id=user.id
    )
    # No customer contact -> not auto-paid at creation; caller flips it.
    assert invoice.status == "awaiting_confirmation"
    assert invoice.paid_at is None
    # Quick sales skip the pre-payment PDF entirely (no bank details needed).
    assert invoice.pdf_url is None
    assert invoice.channel == "quick_sale"
    assert invoice.payment_method == "cash"

    updated = service.update_status(user.id, invoice.invoice_id, "paid", updated_by_user_id=user.id)
    assert updated.status == "paid"
    assert updated.paid_at is not None
    assert updated.receipt_pdf_url is not None
    assert updated.payment_method == "cash"


def test_rapid_identical_quick_sales_are_not_deduped():
    """Two different walk-in customers buying the same-priced item within the
    same minute must both be recorded — the double-submit dedup guard is for
    named-customer invoices, not repeatable walk-in sales."""
    session = SessionLocal()
    user = _make_user(session)
    service = InvoiceService(session, _DummyPDF())

    first = service.create_invoice(user.id, _quick_sale_data(), created_by_user_id=user.id)
    second = service.create_invoice(user.id, _quick_sale_data(), created_by_user_id=user.id)

    assert first.invoice_id != second.invoice_id


def test_quick_sale_endpoint_records_paid_sale_in_one_call():
    client = TestClient(app)

    phone = "+2349990009999"
    r = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "QuickSaleUser",
            "business_name": "Test Biz",
            "accept_terms": True,
        },
    )
    assert r.status_code == 200, r.text

    from app.services.otp_service import OTPService
    import json

    svc = OTPService()
    raw = svc._store.get(f"otp:signup:{phone}")  # type: ignore[attr-defined]
    assert raw is not None
    code = json.loads(raw)["code"]

    v = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": code,
            "bank_name": "SuoOps Bank",
            "account_number": "0001234567",
            "account_name": "Test Biz",
        },
    )
    assert v.status_code == 200, v.text
    token = v.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    from app.db.session import SessionLocal as AppSessionLocal

    s = AppSessionLocal()
    try:
        u = s.query(models.User).filter(models.User.phone == phone).first()
        u.wallet_balance_kobo = 10_000_000
        s.commit()
    finally:
        s.close()

    resp = client.post(
        "/invoices/quick-sale",
        json={"amount": 1500, "description": "Bottled water", "payment_method": "cash"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "paid"
    assert body["channel"] == "quick_sale"
    assert body["payment_method"] == "cash"
    assert body["customer_name"] == "Walk-in Customer"
    assert body["pdf_url"] is None
