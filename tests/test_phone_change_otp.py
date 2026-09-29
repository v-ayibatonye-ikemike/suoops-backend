"""Changing the login phone requires a step-up OTP (a hijacked session must not
be able to silently point the phone at an attacker's number)."""

import json
import secrets

from fastapi.testclient import TestClient

from app.api.main import app
from app.services.otp_service import OTPService

client = TestClient(app)


def _num_phone() -> str:
    return "+23480" + f"{secrets.randbelow(10**8):08d}"


def _otp(identifier: str, purpose: str) -> str:
    raw = OTPService()._store.get(f"otp:{purpose}:{identifier}")  # type: ignore[attr-defined]
    assert raw is not None, f"no OTP for {purpose}:{identifier}"
    return json.loads(raw)["code"]


def _signup(phone: str) -> str:
    client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "PhoneUser",
            "business_name": "Phone Biz",
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
            "account_name": "Phone User",
        },
    )
    assert v.status_code == 200, v.text
    return v.json()["access_token"]


def test_phone_change_requires_step_up_otp():
    phone = _num_phone()
    headers = {"Authorization": f"Bearer {_signup(phone)}"}
    new_phone = _num_phone()

    # Changing the phone without an OTP is rejected.
    r = client.post("/users/me/phone", json={"phone": new_phone}, headers=headers)
    assert r.status_code == 401, r.text

    # Request the step-up code (sent to the CURRENT phone), then retry with it.
    ro = client.post("/users/me/phone/request-otp", headers=headers)
    assert ro.status_code == 200, ro.text
    r2 = client.post(
        "/users/me/phone",
        json={"phone": new_phone, "otp": _otp(phone, "phone_change")},
        headers=headers,
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["phone"] == new_phone


def test_phone_change_wrong_otp_rejected():
    phone = _num_phone()
    headers = {"Authorization": f"Bearer {_signup(phone)}"}
    new_phone = _num_phone()
    client.post("/users/me/phone/request-otp", headers=headers)
    r = client.post(
        "/users/me/phone",
        json={"phone": new_phone, "otp": "000000"},
        headers=headers,
    )
    assert r.status_code == 401, r.text
