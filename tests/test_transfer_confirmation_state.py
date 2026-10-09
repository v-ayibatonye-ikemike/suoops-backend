"""Transfer confirmations preserve invoice state and canonical link behavior."""

from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.core.exceptions import InvalidInvoiceStatusError
from app.models import models
from app.services.invoice_service import InvoiceService


@pytest.fixture
def pending_invoice(db_session):
    owner = models.User(phone="+2348160000091", name="Seller", business_name="Seller")
    buyer = models.Customer(name="Buyer")
    db_session.add_all([owner, buyer])
    db_session.flush()
    invoice = models.Invoice(
        invoice_id="INV-CONFIRM-ABC",
        issuer_id=owner.id,
        customer_id=buyer.id,
        amount=Decimal("1000"),
        status="pending",
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_lowercase_confirmation_link_matches_public_view(client, pending_invoice, monkeypatch):
    notify = Mock()
    monkeypatch.setattr(InvoiceService, "_notify_business_of_transfer", notify)
    response = client.post("/public/invoices/inv-confirm-abc/confirm-transfer")
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "awaiting_confirmation"
    notify.assert_called_once()


def test_cancelled_invoice_cannot_be_revived_by_transfer_confirmation(
    client, pending_invoice, db_session, monkeypatch,
):
    pending_invoice.status = "cancelled"
    db_session.commit()
    notify = Mock()
    monkeypatch.setattr(InvoiceService, "_notify_business_of_transfer", notify)
    response = client.post("/public/invoices/INV-CONFIRM-ABC/confirm-transfer")
    assert response.status_code == 400, response.text
    db_session.refresh(pending_invoice)
    assert pending_invoice.status == "cancelled"
    notify.assert_not_called()


def test_service_rejects_cancelled_confirmation(pending_invoice, db_session, monkeypatch):
    pending_invoice.status = "cancelled"
    db_session.commit()
    monkeypatch.setattr(InvoiceService, "_notify_business_of_transfer", Mock())
    service = InvoiceService(db_session, Mock())
    with pytest.raises(InvalidInvoiceStatusError):
        service.confirm_transfer(pending_invoice.invoice_id)


def test_successful_confirmation_invalidates_both_invoice_caches(
    pending_invoice, db_session, monkeypatch,
):
    notify = Mock()
    cache = Mock()
    monkeypatch.setattr(InvoiceService, "_notify_business_of_transfer", notify)
    service = InvoiceService(db_session, Mock(), cache=cache)
    service.confirm_transfer(pending_invoice.invoice_id)
    service.confirm_transfer(pending_invoice.invoice_id)
    cache.invalidate_invoice.assert_called_once_with(pending_invoice.invoice_id)
    cache.invalidate_user_invoices.assert_called_once_with(pending_invoice.issuer_id)
    notify.assert_called_once()


@pytest.mark.parametrize("status", ["paid", "awaiting_confirmation"])
def test_completed_confirmation_is_idempotent(pending_invoice, db_session, monkeypatch, status):
    pending_invoice.status = status
    db_session.commit()
    notify = Mock()
    monkeypatch.setattr(InvoiceService, "_notify_business_of_transfer", notify)
    service = InvoiceService(db_session, Mock())
    assert service.confirm_transfer(pending_invoice.invoice_id).status == status
    notify.assert_not_called()
