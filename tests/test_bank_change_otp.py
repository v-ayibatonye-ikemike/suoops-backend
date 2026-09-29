"""Step-up OTP required to change an existing bank account."""

import json
import secrets

from fastapi.testclient import TestClient

from app.api.main import app
from app.services.otp_service import OTPService

client = TestClient(app)


def _otp(phone: str, purpose: str) -> str:
    raw = OTPService()._store.get(f"otp:{purpose}:{phone}")  # type: ignore[attr-defined]
    assert raw is not None, f"no OTP for {purpose}"
    return json.loads(raw)["code"]


def _signup_with_bank(phone: str) -> str:
    client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "BankUser",
            "business_name": "Bank Biz",
            "accept_terms": True,
        },
    )
    v = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": _otp(phone, "signup"),
            "bank_name": "GTBank",
            "account_number": "0123456789",
            "account_name": "Bank User",
        },
    )
    assert v.status_code == 200, v.text
    return v.json()["access_token"]


def test_bank_change_requires_step_up_otp(monkeypatch):
    from unittest.mock import AsyncMock

    from app.services.paystack_subaccount_service import PaystackSubaccountService

    # Keep the server-side name resolution hermetic (no real Paystack call).
    monkeypatch.setattr(PaystackSubaccountService, "resolve_bank_code", AsyncMock(return_value="999"))
    monkeypatch.setattr(PaystackSubaccountService, "resolve_account", AsyncMock(return_value="Bank User"))

    phone = "+234" + secrets.token_hex(4)
    headers = {"Authorization": f"Bearer {_signup_with_bank(phone)}"}
    new_acct = {
        "bank_name": "Access Bank",
        "account_number": "1112223334",
        "account_name": "Bank User",
    }

    # Changing an existing account without an OTP is rejected.
    r = client.patch("/users/me/bank-details", json=new_acct, headers=headers)
    assert r.status_code == 401, r.text

    # Request the step-up code, then retry with it.
    ro = client.post("/users/me/bank-details/request-otp", headers=headers)
    assert ro.status_code == 200, ro.text
    r2 = client.patch(
        "/users/me/bank-details",
        json={**new_acct, "otp": _otp(phone, "bank_change")},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["account_number"] == "1112223334"


def test_bank_update_syncs_payout_account(monkeypatch):
    """Single-account model: saving bank details mirrors them into the payout
    fields so payouts follow the account the seller currently manages. The stored
    account NAME is the server-verified one (not whatever the client submitted)."""
    from unittest.mock import AsyncMock

    from app.db.session import get_db
    from app.models import models
    from app.services.paystack_subaccount_service import PaystackSubaccountService

    monkeypatch.setattr(PaystackSubaccountService, "resolve_bank_code", AsyncMock(return_value="999"))
    monkeypatch.setattr(
        PaystackSubaccountService,
        "resolve_account",
        AsyncMock(return_value="VERIFIED HOLDER"),
    )

    phone = "+234" + secrets.token_hex(4)
    headers = {"Authorization": f"Bearer {_signup_with_bank(phone)}"}

    # Change to a new account (with step-up OTP).
    client.post("/users/me/bank-details/request-otp", headers=headers)
    r = client.patch(
        "/users/me/bank-details",
        json={
            "bank_name": "Kuda Bank",
            "account_number": "3003182519",
            "account_name": "Client Supplied Name",
            "otp": _otp(phone, "bank_change"),
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text

    db = next(get_db())
    try:
        user = db.query(models.User).filter(models.User.phone == phone).one()
        # Payout fields now mirror the visible bank details.
        assert user.payout_bank_name == "Kuda Bank"
        assert user.payout_account_number == "3003182519"
        # The stored name is the bank-VERIFIED one, not the client's value.
        assert user.payout_account_name == "VERIFIED HOLDER"
    finally:
        db.close()
