import json
import secrets

from fastapi.testclient import TestClient

from app.api.main import app
from app.db.session import get_db
from app.models import models
from app.services.otp_service import OTPService


def _extract_otp(phone: str, purpose: str) -> str:
    service = OTPService()
    raw = service._store.get(f"otp:{purpose}:{phone}")  # type: ignore[attr-defined]
    assert raw is not None
    data = json.loads(raw)
    return data["code"]


def _auth_client() -> tuple[TestClient, str]:
    client = TestClient(app)
    phone = "+234" + secrets.token_hex(4)
    # signup
    # Provide business_name to satisfy non-null constraints if any and ensure phone persists
    r = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "TaxUser",
            "business_name": "Test Biz",
            "accept_terms": True,
        },
    )
    assert r.status_code == 200, r.text
    otp = _extract_otp(phone, "signup")
    v = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": otp,
            "bank_name": "Test Bank",
            "account_number": "0123456789",
            "account_name": "Test Biz",
        },
    )
    assert v.status_code == 200, v.text

    # Tax endpoints require PRO plan.
    db = next(get_db())
    user = db.query(models.User).filter(models.User.phone == phone).one()
    user.plan = models.SubscriptionPlan.PRO
    db.commit()

    access = v.json()["access_token"]
    return client, access


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_tax_profile_and_compliance_endpoints():
    client, token = _auth_client()
    headers = _auth_headers(token)

    # /tax/profile
    prof = client.get("/tax/profile", headers=headers)
    assert prof.status_code == 200, prof.text
    jp = prof.json()
    assert "business_size" in jp and "classification" in jp and "tax_rates" in jp

    # /tax/small-business-check
    sb = client.get("/tax/small-business-check", headers=headers)
    assert sb.status_code == 200, sb.text
    jsb = sb.json()
    assert "eligible" in jsb and "business_size" in jsb and "tax_rates" in jsb

    # /tax/compliance
    comp = client.get("/tax/compliance", headers=headers)
    assert comp.status_code == 200, comp.text
    jc = comp.json()
    assert "compliance_status" in jc and "requirements" in jc

    # /tax/vat/summary
    vat = client.get("/tax/vat/summary", headers=headers)
    assert vat.status_code == 200, vat.text
    jv = vat.json()
    assert "current_month" in jv and "registered" in jv
    assert "tax_period" in jv["current_month"]


def test_tax_profile_update_returns_summary():
    client, token = _auth_client()
    headers = _auth_headers(token)

    upd = client.post(
        "/tax/profile",
        json={"annual_turnover": 5000000, "fixed_assets": 1000000},
        headers=headers,
    )
    assert upd.status_code == 200, upd.text
    ju = upd.json()
    assert ju.get("message") == "Tax profile updated successfully"
    assert "summary" in ju
    assert "classification" in ju["summary"]


def test_tax_profile_invalid_tin_vat_validation():
    client, token = _auth_client()
    headers = _auth_headers(token)
    # Invalid TIN length
    r1 = client.post("/tax/profile", json={"tin": "123"}, headers=headers)
    assert r1.status_code == 422
    assert "exactly 10" in r1.text
    # Invalid TIN non-numeric
    r2 = client.post("/tax/profile", json={"tin": "ABCDEFGHIJ"}, headers=headers)
    assert r2.status_code == 422
    # Invalid TIN trivial sequence
    r3 = client.post("/tax/profile", json={"tin": "1234567890"}, headers=headers)
    assert r3.status_code == 422
    # Valid TIN
    ok = client.post("/tax/profile", json={"tin": "9876543210"}, headers=headers)
    assert ok.status_code == 200, ok.text
    # Invalid VAT number pattern (too short)
    v1 = client.post("/tax/profile", json={"vat_registration_number": "ABC123"}, headers=headers)
    assert v1.status_code == 422
    # Invalid VAT number pattern (chars + dash)
    v2 = client.post("/tax/profile", json={"vat_registration_number": "ABCD-1234"}, headers=headers)
    assert v2.status_code == 422
    # Valid VAT number
    vok = client.post("/tax/profile", json={"vat_registration_number": "ABCD1234"}, headers=headers)
    assert vok.status_code == 200, vok.text
