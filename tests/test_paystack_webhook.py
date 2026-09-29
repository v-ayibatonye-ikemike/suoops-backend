import hashlib
import hmac
import json
import secrets

from fastapi.testclient import TestClient

from app.api.main import app
from app.core.config import settings
from app.services.otp_service import _SHARED_STORE, OTPRecord


def _auth_headers(client: TestClient) -> dict[str, str]:
    """Obtain auth headers via OTP signup flow (replaces legacy password endpoints)."""
    phone = "+234" + secrets.token_hex(4)
    start = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "PUser",
            "business_name": "PUser Biz",
            "accept_terms": True,
        },
    )
    assert start.status_code == 200, start.text
    key = f"otp:signup:{phone}"
    raw = _SHARED_STORE.get(key)  # type: ignore[attr-defined]
    assert raw, "Signup OTP missing"
    otp = OTPRecord.deserialize(raw).code
    verify = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": otp,
            "bank_name": "Test Bank",
            "account_number": "0123456789",
            "account_name": "PUser Biz",
        },
    )
    assert verify.status_code == 200, verify.text
    token = verify.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def test_paystack_subscription_charge_upgrades_plan():
    client = TestClient(app)
    headers = _auth_headers(client)

    profile = client.get("/users/me", headers=headers)
    assert profile.status_code == 200, profile.text
    user_data = profile.json()
    user_id = user_data["id"]
    assert user_data["plan"].lower() == "free"

    subscription_event = {
        "event": "charge.success",
        "data": {
            "reference": "SUB-12345",
            "metadata": {
                "user_id": user_id,
                "plan": "pro",
            },
        },
    }
    raw = json.dumps(subscription_event).encode()
    sig = hmac.new(settings.PAYSTACK_SECRET.encode(), raw, hashlib.sha512).hexdigest()

    result = client.post(
        "/webhooks/paystack",
        content=raw,
        headers={"x-paystack-signature": sig},
    )
    assert result.status_code == 200, result.text
    payload = result.json()
    assert payload["status"] == "success"
    assert payload["new_plan"] == "pro"

    refreshed = client.get("/users/me", headers=headers)
    assert refreshed.status_code == 200
    assert refreshed.json()["plan"].lower() == "pro"
