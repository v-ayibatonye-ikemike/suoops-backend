import json
import secrets

from fastapi.testclient import TestClient

from app.api.main import app
from app.services.otp_service import OTPService


def _extract_otp(phone: str, purpose: str) -> str:
    service = OTPService()
    raw = service._store.get(f"otp:{purpose}:{phone}")  # type: ignore[attr-defined]
    assert raw is not None
    data = json.loads(raw)
    return data["code"]


def test_signup_requires_terms_acceptance():
    """Signup is rejected until the business accepts the Terms & Conditions."""
    client = TestClient(app)
    phone = "+234" + secrets.token_hex(4)
    resp = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "NoTerms",
            "business_name": "No Terms Biz",
        },
    )
    assert resp.status_code == 400, resp.text
    assert "terms" in resp.text.lower()


def test_signup_and_login_with_otp():
    client = TestClient(app)
    phone = "+234" + secrets.token_hex(4)

    # Step 1: request signup OTP
    reg = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "UserA",
            "business_name": "User A Biz",
            "accept_terms": True,
        },
    )
    assert reg.status_code == 200, reg.text

    otp = _extract_otp(phone, "signup")

    # Step 2: verify signup OTP and receive tokens
    verify_signup = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": otp,
            "bank_name": "Test Bank",
            "account_number": "0123456789",
            "account_name": "User A Biz",
        },
    )
    assert verify_signup.status_code == 200, verify_signup.text
    signup_payload = verify_signup.json()
    assert signup_payload["token_type"] == "bearer"
    refresh_cookie = verify_signup.cookies.get("whatsinvoice.refresh")
    assert refresh_cookie is not None

    # Step 3: request login OTP
    login_request = client.post("/auth/login/request", json={"phone": phone})
    assert login_request.status_code == 200, login_request.text

    login_otp = _extract_otp(phone, "login")

    # Step 4: verify login OTP and get tokens
    login_verify = client.post(
        "/auth/login/verify",
        json={"phone": phone, "otp": login_otp},
    )
    assert login_verify.status_code == 200, login_verify.text
    login_payload = login_verify.json()
    assert login_payload["token_type"] == "bearer"
    assert "access_expires_at" in login_payload
