"""Integration-style test for business confirmation notifications.

Verifies that `InvoiceService.confirm_transfer` triggers NotificationService
email dispatch via the facade after customer reports payment.
Network calls are monkeypatched to avoid external dependencies.
"""

from __future__ import annotations

from decimal import Decimal

from app.models import models
from app.services.invoice_service import InvoiceService
from app.services.notification.service import NotificationService
from app.services.pdf_service import PDFService
from app.storage.s3_client import S3Client


class _DummyPDF(PDFService):  # type: ignore[misc]
    def __init__(self):
        self.client = S3Client()

    def generate_invoice_pdf(self, invoice, bank_details=None, logo_url=None, user_plan=None):  # noqa: D401
        return f"http://pdf.local/invoice/{invoice.invoice_id}.pdf"

    def generate_receipt_pdf(self, invoice):  # noqa: D401
        return f"http://pdf.local/receipt/{invoice.invoice_id}.pdf"


def test_business_confirmation_notifications(monkeypatch, db_session):
    # Create issuer with contact info
    user = models.User(
        phone="+2348333333333",
        name="Issuer",
        email="issuer@example.com",
        business_name="IssuerBiz",
        bank_name="TestBank",
        account_number="0123000000",
        account_name="ISSUER",
        wallet_balance_kobo=10_000_000,
    )
    db_session.add(user)
    db_session.commit()

    # Create customer
    customer = models.Customer(
        name="Confirming Customer",
        phone="+2348444444444",
        email="confirm@example.com",
    )
    db_session.add(customer)
    db_session.commit()

    service = InvoiceService(db_session, _DummyPDF())

    invoice = service.create_invoice(
        user.id,
        {
            "customer_name": customer.name,
            "customer_phone": customer.phone,
            "customer_email": customer.email,
            "amount": Decimal("2500.00"),
            "description": "Design Work",
        },
    )

    # Sanity pre-condition
    assert invoice.status == "pending"

    calls = {"email": 0}

    async def fake_send_email(self, to_email, subject, body):  # noqa: D401
        assert "Payment Confirmation" in subject
        calls["email"] += 1
        return True

    # Monkeypatch facade methods used internally by confirm_transfer flow
    monkeypatch.setattr(NotificationService, "send_email", fake_send_email)

    # Execute confirmation
    updated = service.confirm_transfer(invoice.invoice_id)
    assert updated.status == "awaiting_confirmation"

    # Email path attempted exactly once
    assert calls == {"email": 1}
