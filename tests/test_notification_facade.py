"""Async tests for NotificationService facade (invoice + receipt).

These tests focus on verifying that the public composite methods
`send_invoice_notification` and `send_receipt_notification` orchestrate
channel calls and return an aggregate dict with the expected shape.

Network / provider calls are monkeypatched out so tests run quickly and
without external dependencies.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import models
from app.services.notification.service import NotificationService


@pytest.fixture()
def sample_invoice(db_session):  # type: ignore[override]
    # Create issuer
    user = models.User(
        phone="+2348111111111",
        name="Biz",
        email="biz@example.com",
        business_name="BizCo",
        bank_name="Bank",
        account_number="0123456789",
        account_name="BIZCO",
    )
    db_session.add(user)
    db_session.commit()
    # Create customer
    customer = models.Customer(
        name="Customer",
        phone="+2348222222222",
        email="cust@example.com",
    )
    db_session.add(customer)
    db_session.commit()
    inv = models.Invoice(
        invoice_id="INV-TEST-123",
        issuer_id=user.id,
        customer_id=customer.id,
        amount=Decimal("1000.00"),
        status="pending",
        pdf_url="http://pdf.local/invoice/INV-TEST-123.pdf",
    )
    db_session.add(inv)
    db_session.commit()
    db_session.refresh(inv)
    return inv


@pytest.mark.asyncio
async def test_send_invoice_notification(monkeypatch, sample_invoice):
    service = NotificationService()
    calls = {"email": 0, "whatsapp": 0}

    async def fake_invoice_email(self, invoice, recipient_email, pdf_url=None, subject="New Invoice"):
        calls["email"] += 1
        return True

    async def fake_invoice_whatsapp(self, invoice, recipient_phone, pdf_url=None):
        calls["whatsapp"] += 1
        return True

    monkeypatch.setattr(NotificationService, "send_invoice_email", fake_invoice_email)
    monkeypatch.setattr(NotificationService, "send_invoice_whatsapp", fake_invoice_whatsapp)

    result = await service.send_invoice_notification(
        invoice=sample_invoice,
        customer_email=sample_invoice.customer.email,  # type: ignore[arg-type]
        customer_phone=sample_invoice.customer.phone,  # type: ignore[arg-type]
        pdf_url=sample_invoice.pdf_url,
    )

    assert result == {"email": True, "whatsapp": True}
    assert calls == {"email": 1, "whatsapp": 1}


@pytest.mark.asyncio
async def test_send_receipt_notification(monkeypatch, sample_invoice, db_session):
    # Simulate invoice paid & receipt URL
    sample_invoice.status = "paid"
    sample_invoice.receipt_pdf_url = "http://pdf.local/receipt/INV-TEST-123.pdf"
    db_session.commit()

    service = NotificationService()
    calls = {"email": 0, "whatsapp": 0}

    async def fake_receipt_email(self, invoice, recipient_email, pdf_url=None):
        calls["email"] += 1
        return True

    async def fake_receipt_whatsapp(self, invoice, recipient_phone, pdf_url=None):
        calls["whatsapp"] += 1
        return True

    monkeypatch.setattr(NotificationService, "send_receipt_email", fake_receipt_email)
    monkeypatch.setattr(NotificationService, "send_receipt_whatsapp", fake_receipt_whatsapp)

    result = await service.send_receipt_notification(
        invoice=sample_invoice,
        customer_email=sample_invoice.customer.email,  # type: ignore[arg-type]
        customer_phone=sample_invoice.customer.phone,  # type: ignore[arg-type]
        pdf_url=sample_invoice.receipt_pdf_url,
    )

    assert result == {"email": True, "whatsapp": True}
    assert calls == {"email": 1, "whatsapp": 1}
