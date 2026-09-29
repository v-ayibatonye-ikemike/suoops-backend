"""Tests for the bank-account name resolution endpoint."""

import json
import secrets
from unittest.mock import AsyncMock

from fastapi.testclient import TestClient

from app.api.main import app
from app.services.otp_service import OTPService
from app.services.paystack_subaccount_service import PaystackSubaccountService


def _auth_headers(client: TestClient) -> dict[str, str]:
    phone = "+234" + secrets.token_hex(4)
    r = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "Resolver",
            "business_name": "Biz",
            "accept_terms": True,
        },
    )
    assert r.status_code == 200, r.text
    raw = OTPService()._store.get(f"otp:signup:{phone}")  # type: ignore[attr-defined]
    code = json.loads(raw)["code"]
    v = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": code,
            "bank_name": "SuoOps Bank",
            "account_number": "0001234567",
            "account_name": "Biz",
        },
    )
    assert v.status_code == 200, v.text
    return {"Authorization": f"Bearer {v.json()['access_token']}"}


def test_resolve_bank_account_success(monkeypatch):
    monkeypatch.setattr(PaystackSubaccountService, "resolve_bank_code", AsyncMock(return_value="999"))
    monkeypatch.setattr(
        PaystackSubaccountService,
        "resolve_account",
        AsyncMock(return_value="IKEMIKE CREATIVE HUB LTD"),
    )
    client = TestClient(app)
    headers = _auth_headers(client)
    r = client.post(
        "/users/me/resolve-bank-account",
        json={"bank_name": "OPay", "account_number": "7065730703"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["account_name"] == "IKEMIKE CREATIVE HUB LTD"


def test_resolve_bank_account_rejects_bad_number():
    client = TestClient(app)
    headers = _auth_headers(client)
    r = client.post(
        "/users/me/resolve-bank-account",
        json={"bank_name": "OPay", "account_number": "123"},
        headers=headers,
    )
    assert r.status_code == 400


def test_resolve_bank_account_requires_auth():
    client = TestClient(app)
    r = client.post(
        "/users/me/resolve-bank-account",
        json={"bank_name": "OPay", "account_number": "7065730703"},
    )
    assert r.status_code == 401
