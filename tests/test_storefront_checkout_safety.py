from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import routes_storefront as storefront
from app.api import routes_webhooks as webhooks
from app.core.config import settings
from app.models import models
from app.models.payment_models import PaymentProvider, PaymentStatus, PaymentTransaction
from app.services import escrow_service
from app.services.collections.base import ChargeStatus
from tests.test_store_delivery_fee import _seed_store


@pytest.fixture
def checkout(db_session, monkeypatch):
    owner, product = _seed_store(db_session)
    monkeypatch.setattr(settings, "ESCROW_ENABLED", True)
    monkeypatch.setattr(escrow_service, "is_trusted_seller", lambda *a: False)
    monkeypatch.setattr(escrow_service, "detect_order_collusion", lambda *a, **kw: None)
    monkeypatch.setattr(escrow_service, "seller_velocity_hold_reason", lambda *a: None)
    monkeypatch.setattr(escrow_service, "send_delivery_code", Mock())
    start = AsyncMock(return_value={"authorization_url": "https://pay.example.test/order"})
    monkeypatch.setattr("app.services.invoice_payment_service.start_invoice_payment", start)
    payload = {
        "customer_name": "Buyer",
        "customer_phone": "+2348161111111",
        "items": [{"product_id": product.id, "quantity": 1}],
        "delivery_note": "12 Test St, Ikeja; blue gate",
    }
    return owner, payload, start


def test_payment_starts_only_after_protection_is_persisted(client, db_session, checkout):
    _, payload, start = checkout

    async def initialize(db, invoice, owner, **kwargs):
        row = db.query(models.StorefrontOrderEscrow).filter_by(invoice_id=invoice.id).one_or_none()
        assert row is not None, "A payable link must never precede the protection record"
        assert row.status == "pending"
        assert row.confirmation_code
        return {"authorization_url": "https://pay.example.test/order"}

    start.side_effect = initialize
    response = client.post("/public/store/teststore/order", json=payload)
    assert response.status_code == 200, response.text


def test_protection_failure_never_initializes_payment(client, db_session, checkout, monkeypatch):
    _, payload, start = checkout
    monkeypatch.setattr(escrow_service, "create_order_escrow", Mock(side_effect=RuntimeError("database unavailable")))
    response = client.post("/public/store/teststore/order", json=payload)
    assert response.status_code == 503, response.text
    assert "protection" in response.json()["detail"].lower()
    start.assert_not_awaited()

    invoice = db_session.query(models.Invoice).filter_by(channel="storefront").one()
    retry = client.post(f"/public/invoices/{invoice.invoice_id}/pay")
    assert retry.status_code == 409
    start.assert_not_awaited()


def test_separate_checkouts_do_not_share_invoice_or_release_code(client, db_session, checkout):
    _, payload, _ = checkout
    first = client.post("/public/store/teststore/order", json=payload)
    second = client.post("/public/store/teststore/order", json={**payload, "delivery_note": "24 Other Street"})
    assert first.status_code == second.status_code == 200
    assert first.json()["invoice_id"] != second.json()["invoice_id"]
    assert first.json()["delivery_code"] != second.json()["delivery_code"]


def test_duplicate_product_lines_cannot_bypass_quantity_or_stock_limits():
    with pytest.raises(ValidationError, match="once"):
        storefront.StoreOrderIn(
            customer_name="Buyer", customer_phone="08012345678",
            items=[{"product_id": 1, "quantity": 50}, {"product_id": 1, "quantity": 50}],
        )


def _record_payment(db, owner, invoice_id, provider_name):
    invoice = db.query(models.Invoice).filter_by(invoice_id=invoice_id).one()
    transaction = PaymentTransaction(
        user_id=owner.id, reference=f"INVPAY-{invoice_id}-RETRY", amount=1030000, currency="NGN",
        plan_before="free", plan_after="free", provider=PaymentProvider.PAYSTACK,
        status=PaymentStatus.PENDING, customer_email="buyer@example.test",
        payment_metadata={"invoice_id": invoice_id, "collector": provider_name},
    )
    db.add(transaction)
    db.commit()
    return invoice, transaction


@pytest.mark.parametrize("provider_name", ["paystack", "flutterwave"])
@pytest.mark.parametrize("recovery_path", ["webhook", "checkout_return"])
def test_payment_retry_recovers_after_invoice_commit(
    client, db_session, checkout, monkeypatch, provider_name, recovery_path,
):
    owner, payload, _ = checkout
    response = client.post("/public/store/teststore/order", json=payload)
    invoice_id = response.json()["invoice_id"]
    invoice, transaction = _record_payment(db_session, owner, invoice_id, provider_name)
    collector = Mock()
    collector.verify_charge.return_value = ChargeStatus(
        status="successful", amount_kobo=1030000, currency="NGN", raw={},
    )
    select_collector = Mock(return_value=collector)
    monkeypatch.setattr("app.services.collections.get_collection_provider_named", select_collector)
    real_activate = escrow_service.activate_escrow_on_payment
    activate = Mock(side_effect=RuntimeError("temporary activation failure"))
    monkeypatch.setattr(escrow_service, "activate_escrow_on_payment", activate)
    data = {"reference": transaction.reference, "tx_ref": transaction.reference, "status": "successful"}
    event = {"event": "charge.success" if provider_name == "paystack" else "charge.completed", "data": data}
    handler = (
        webhooks._handle_paystack_invoice_payment
        if provider_name == "paystack" else webhooks._handle_flutterwave_invoice_payment
    )
    with pytest.raises(HTTPException) as failure:
        handler(event, db_session, None)
    assert failure.value.status_code == 503
    db_session.refresh(invoice)
    db_session.refresh(transaction)
    assert invoice.status == "paid"
    assert transaction.status == PaymentStatus.PENDING
    assert db_session.query(models.WebhookEvent).filter_by(external_id=transaction.reference).count() == 1

    monkeypatch.setattr(escrow_service, "activate_escrow_on_payment", real_activate)
    if recovery_path == "webhook":
        assert handler(event, db_session, None)["status"] == "success"
    else:
        response = client.post(f"/public/invoices/{invoice_id}/verify", params={"reference": transaction.reference})
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "paid"
    select_collector.assert_called_with(provider_name)
    row = db_session.query(models.StorefrontOrderEscrow).filter_by(invoice_id=invoice.id).one()
    assert row.status == "held"
    assert row.charge_reference == transaction.reference
    db_session.refresh(transaction)
    assert transaction.status == PaymentStatus.SUCCESS
    assert handler(event, db_session, None)["status"] == "duplicate"


@pytest.mark.parametrize("provider_name", ["paystack", "flutterwave"])
def test_temporary_verification_failure_requests_provider_retry(
    db_session, checkout, monkeypatch, provider_name,
):
    collector = Mock()
    collector.verify_charge.side_effect = TimeoutError("temporary verification outage")
    monkeypatch.setattr("app.services.collections.get_collection_provider_named", lambda name: collector)
    handler = (
        webhooks._handle_paystack_invoice_payment
        if provider_name == "paystack" else webhooks._handle_flutterwave_invoice_payment
    )
    payload = {
        "event": "charge.success" if provider_name == "paystack" else "charge.completed",
        "data": {
            "reference": "INVPAY-RETRY", "tx_ref": "INVPAY-RETRY", "status": "successful",
            "metadata": {"invoice_id": "INV-1"}, "meta": {"invoice_id": "INV-1"},
        },
    }
    with pytest.raises(HTTPException) as failure:
        handler(payload, db_session, None)
    assert failure.value.status_code == 503


@pytest.mark.parametrize("provider_name", ["paystack", "flutterwave"])
@pytest.mark.parametrize("amount,currency", [(100, "NGN"), (1030000, "USD"), (None, "NGN"), (1030000, None)])
def test_checkout_return_rejects_incomplete_or_mismatched_charge(
    client, db_session, checkout, monkeypatch, provider_name, amount, currency,
):
    owner, payload, _ = checkout
    invoice_id = client.post("/public/store/teststore/order", json=payload).json()["invoice_id"]
    invoice, transaction = _record_payment(db_session, owner, invoice_id, provider_name)
    collector = Mock()
    collector.verify_charge.return_value = ChargeStatus(status="successful", amount_kobo=amount, currency=currency)
    monkeypatch.setattr("app.services.collections.get_collection_provider_named", lambda name: collector)
    response = client.post(f"/public/invoices/{invoice_id}/verify", params={"reference": transaction.reference})
    assert response.status_code == 409, response.text
    db_session.refresh(invoice)
    db_session.refresh(transaction)
    assert invoice.status == "pending"
    assert transaction.status == PaymentStatus.PENDING
    assert db_session.query(models.WebhookEvent).count() == 0


@pytest.mark.parametrize("reference", ["INVPAY-UNKNOWN", "INVPAY-OTHER-INVOICE-RETRY"])
def test_checkout_return_requires_a_reference_for_this_invoice(client, db_session, checkout, monkeypatch, reference):
    owner, payload, _ = checkout
    invoice_id = client.post("/public/store/teststore/order", json=payload).json()["invoice_id"]
    _, transaction = _record_payment(db_session, owner, invoice_id, "paystack")
    transaction.reference = "INVPAY-OTHER-INVOICE-RETRY"
    transaction.payment_metadata = {"invoice_id": "INV-OTHER"}
    db_session.commit()
    select_collector = Mock()
    monkeypatch.setattr("app.services.collections.get_collection_provider_named", select_collector)
    response = client.post(f"/public/invoices/{invoice_id}/verify", params={"reference": reference})
    assert response.status_code == 400
    select_collector.assert_not_called()


def test_changed_checkout_total_requires_buyer_review(client, db_session, checkout):
    _, payload, start = checkout
    response = client.post("/public/store/teststore/order", json={**payload, "expected_total_kobo": 1030001})
    assert response.status_code == 409
    assert "Prices or delivery fees have changed" in response.json()["detail"]
    start.assert_not_awaited()
    assert db_session.query(models.Invoice).count() == 0

    response = client.post("/public/store/teststore/order", json={**payload, "expected_total_kobo": 1030000})
    assert response.status_code == 200, response.text
    assert start.await_args.kwargs["charge_amount_kobo"] == 1030000
