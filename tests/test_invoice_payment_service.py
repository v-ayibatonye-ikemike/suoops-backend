"""Regression tests for the invoice / storefront online-payment path."""

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, Mock, patch

import pytest

from app.models import models
from app.models.payment_models import PaymentStatus, PaymentTransaction
from app.services.invoice_payment_service import start_invoice_payment


def _seed(db):
    issuer = models.User(
        phone="+2348160000000",
        name="Owner",
        business_name="Owner Biz",
        bank_name="Opay",
        account_number="0123456789",
        account_name="OWNER",
        paystack_subaccount_active=True,
        paystack_subaccount_code="ACCT_test123",
    )
    db.add(issuer)
    db.commit()
    db.refresh(issuer)

    cust = models.Customer(name="Buyer", phone="+2348161111111", email="buyer@example.com")
    db.add(cust)
    db.commit()
    db.refresh(cust)

    now = dt.datetime.now(dt.timezone.utc)
    inv = models.Invoice(
        invoice_id="INV-PAYTEST-1",
        issuer_id=issuer.id,
        customer_id=cust.id,
        amount=Decimal("11000"),
        status="pending",
        created_at=now,
        due_date=now + dt.timedelta(days=3),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return issuer, inv


@pytest.mark.asyncio
async def test_start_invoice_payment_populates_plan_columns(db_session):
    """The PaymentTransaction must set the legacy NOT NULL plan_before/plan_after
    columns; omitting them previously caused a NotNullViolation on commit."""
    issuer, inv = _seed(db_session)

    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {
        "status": True,
        "data": {"authorization_url": "https://paystack.test/pay/xyz"},
    }

    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
        result = await start_invoice_payment(db_session, inv, issuer)

    assert result["authorization_url"] == "https://paystack.test/pay/xyz"
    assert result["reference"].startswith("INVPAY-INV-PAYTEST-1-")

    tx = db_session.query(PaymentTransaction).filter(PaymentTransaction.reference == result["reference"]).one()
    assert tx.plan_before is not None
    assert tx.plan_after is not None
    assert tx.status == PaymentStatus.PENDING
    assert tx.amount == 1_100_000  # ₦11,000 in kobo
