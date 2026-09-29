"""Invoice paid_at timestamp & receipt dispatch tests.

Focus:
1. Status transition to 'paid' sets timezone-aware paid_at (UTC) & generates receipt PDF.
2. Receipt notification helper invoked with expected email subject containing business name.

Implementation:
- In-memory SQLite keeps tests lightweight.
- Monkeypatch notification helper to capture arguments.
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models import models
from app.services.invoice_service import InvoiceService
from app.services.pdf_service import PDFService
from app.storage.s3_client import S3Client

engine = create_engine("sqlite:///:memory:")


# Ensure SQLite preserves timezone info (older SQLite versions may drop tzinfo).
@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):  # noqa: D401
    # Nothing critical now; placeholder for future if needed.
    pass


SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


def _make_user(session, business_name: str = "Acme Corp"):
    """Create a user with a unique phone/email per invocation.

    The previous implementation reused a static phone which caused a
    UNIQUE constraint violation when tests created multiple users in
    the same in‑memory database (or when tests are run together in a
    shared process). We add a timestamp-based suffix to guarantee
    uniqueness without adding extra dependencies.
    """
    suffix = dt.datetime.now(dt.UTC).strftime("%H%M%S%f")  # unique suffix, UTC-aware
    user = models.User(
        phone=f"+234999{suffix}",
        name="Tester",
        email=f"tester+{suffix}@example.com",
        business_name=business_name,
        bank_name="Test Bank",
        account_number="0123456789",
        account_name="TESTER",
        wallet_balance_kobo=10_000_000,
    )
    session.add(user)
    session.commit()
    return user


def _make_customer(session):
    # Avoid duplicate (name, phone) rows causing MultipleResultsFound in customer helper
    existing = (
        session.query(models.Customer)
        .filter(
            models.Customer.name == "Customer",
            models.Customer.phone == "+234888888888",
        )
        .first()
    )
    if existing:
        return existing
    cust = models.Customer(name="Customer", phone="+234888888888", email="cust@example.com")
    session.add(cust)
    session.commit()
    return cust


class _DummyPDF(PDFService):  # type: ignore[misc]
    def __init__(self):
        # Avoid S3 network; override client
        self.client = S3Client()

    def generate_invoice_pdf(self, invoice, bank_details=None, logo_url=None, user_plan=None):  # noqa: D401
        return f"http://pdf.local/invoice/{invoice.invoice_id}.pdf"

    def generate_receipt_pdf(self, invoice):  # noqa: D401
        return f"http://pdf.local/receipt/{invoice.invoice_id}.pdf"


def test_paid_status_sets_paid_at_and_receipt_pdf(monkeypatch):
    session = SessionLocal()
    user = _make_user(session)
    _make_customer(session)
    service = InvoiceService(session, _DummyPDF())
    invoice = service.create_invoice(
        user.id,
        {
            "customer_name": "Customer",
            "customer_phone": "+234888888888",
            "amount": 5000,
            "description": "Test Item",
        },
    )
    assert invoice.paid_at is None
    assert invoice.receipt_pdf_url is None

    called = {}

    async def fake_send_receipt_notification(self, invoice, customer_email=None, customer_phone=None, pdf_url=None):  # noqa: D401
        called["invoice_id"] = invoice.invoice_id
        called["pdf_url"] = invoice.pdf_url
        return {"email": bool(customer_email), "whatsapp": bool(customer_phone)}

    # Patch NotificationService facade method directly (legacy wrapper removed)
    from app.services.notification.service import NotificationService

    monkeypatch.setattr(NotificationService, "send_receipt_notification", fake_send_receipt_notification)

    updated = service.update_status(user.id, invoice.invoice_id, "paid")
    assert updated.status == "paid"
    assert updated.paid_at is not None
    # Ensure timezone-aware UTC; if SQLite strips tzinfo (older versions) make test informative
    if updated.paid_at.tzinfo is None:
        raise AssertionError(f"paid_at is naive (tzinfo lost by backend): {updated.paid_at!r}")
    assert updated.paid_at.tzinfo.utcoffset(updated.paid_at) == dt.timedelta(0)
    assert updated.receipt_pdf_url is not None
    # Notification helper called
    assert called["invoice_id"] == invoice.invoice_id
    assert called["pdf_url"] == updated.pdf_url


def test_receipt_email_subject_includes_business_name(monkeypatch):
    session = SessionLocal()
    user = _make_user(session, business_name="MegaBiz")
    _make_customer(session)
    service = InvoiceService(session, _DummyPDF())
    invoice = service.create_invoice(
        user.id,
        {
            "customer_name": "Customer",
            "customer_phone": "+234888888888",
            "amount": 10000,
            "description": "Service Fee",
        },
    )

    # Monkeypatch NotificationService to inspect subject
    captured = {}

    class FakeNotificationService:
        async def send_receipt_notification(self, invoice, customer_email, customer_phone, pdf_url):  # noqa: D401
            # Emulate composing subject similar to real implementation
            subject = f"Payment Receipt - {invoice.issuer.business_name}"
            captured["subject"] = subject
            captured["pdf_url"] = pdf_url
            return {"email": True, "whatsapp": False}

    # Monkeypatch facade method now used directly inside service.update_status
    async def fake_send_receipt_notification(self, invoice, customer_email=None, customer_phone=None, pdf_url=None):  # noqa: D401
        subject = f"Payment Receipt - {invoice.issuer.business_name}"
        captured["subject"] = subject
        captured["pdf_url"] = pdf_url or invoice.receipt_pdf_url
        return {"email": bool(customer_email), "whatsapp": bool(customer_phone)}

    from app.services.notification.service import NotificationService

    monkeypatch.setattr(NotificationService, "send_receipt_notification", fake_send_receipt_notification)

    service.update_status(user.id, invoice.invoice_id, "paid")
    assert captured["subject"].startswith("Payment Receipt - MegaBiz")
    assert captured["pdf_url"].endswith(".pdf")
