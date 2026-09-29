"""Wallet top-up endpoint (/invoices/purchase-pack) — the wallet-credit contract.

Regression guard: this endpoint previously imported a removed constant
(PACK_OPTIONS) and 500'd on every call. It now takes an amount tier and records
wallet_credit_kobo so the Paystack webhook credits the prepaid wallet.
"""

import secrets
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

from app.api.main import app
from app.models.payment_models import PaymentTransaction
from app.services.otp_service import _SHARED_STORE, OTPRecord


def _auth_headers(client: TestClient) -> dict[str, str]:
    phone = "+234" + secrets.token_hex(4)
    start = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "TUser",
            "business_name": "TUser Biz",
            "accept_terms": True,
        },
    )
    assert start.status_code == 200, start.text
    raw = _SHARED_STORE.get(f"otp:signup:{phone}")  # type: ignore[attr-defined]
    otp = OTPRecord.deserialize(raw).code
    verify = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": otp,
            "bank_name": "Test Bank",
            "account_number": "0123456789",
            "account_name": "TUser Biz",
        },
    )
    assert verify.status_code == 200, verify.text
    return {"Authorization": f"Bearer {verify.json()['access_token']}"}


def test_wallet_topup_initializes_and_records_wallet_credit(db_session):
    client = TestClient(app)
    headers = _auth_headers(client)

    mock_response = Mock()
    mock_response.raise_for_status = Mock()
    mock_response.json.return_value = {
        "status": True,
        "data": {"authorization_url": "https://paystack.test/pay/topup"},
    }
    with patch("httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_response)
        r = client.post("/invoices/purchase-pack", params={"amount": 1250}, headers=headers)

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["authorization_url"] == "https://paystack.test/pay/topup"
    assert body["reference"].startswith("INVPACK-")
    assert body["wallet_credit_naira"] == 1250
    assert body["amount"] > 1250  # customer also covers the Paystack fee

    tx = db_session.query(PaymentTransaction).filter(PaymentTransaction.reference == body["reference"]).one()
    assert tx.payment_metadata["wallet_credit_kobo"] == 125000


def test_wallet_topup_rejects_non_tier_amount():
    client = TestClient(app)
    headers = _auth_headers(client)
    r = client.post("/invoices/purchase-pack", params={"amount": 999}, headers=headers)
    assert r.status_code == 400
