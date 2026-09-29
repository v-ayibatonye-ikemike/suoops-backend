import secrets
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.main import app
from app.services.otp_service import _SHARED_STORE, OTPRecord  # Access test OTP store


def _signup_and_get_token(client: TestClient):
    """Perform signup via OTP flow and return auth headers.

    This replaces legacy password-based register/login.
    """
    phone = "+234" + secrets.token_hex(4)
    # Request signup OTP
    req = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "SmokeUser",
            "business_name": "Smoke Biz",
            "accept_terms": True,
        },
    )
    assert req.status_code == 200, req.text
    # Retrieve OTP directly from in-memory store (test environment convenience)
    key = f"otp:signup:{phone}"
    raw = _SHARED_STORE.get(key)  # type: ignore[attr-defined]
    assert raw, "Signup OTP missing from store"
    otp = OTPRecord.deserialize(raw).code
    verify = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": otp,
            "bank_name": "SmokeBank",
            "account_number": "1234567890",
            "account_name": "Smoke Biz",
        },
    )
    assert verify.status_code == 200, verify.text
    token = verify.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # Seed bank details so invoice creation passes
    bank_payload = {
        "business_name": "Smoke Biz",
        "bank_name": "SmokeBank",
        "account_number": "1234567890",
        "account_name": "Smoke Biz",
    }
    bd = client.patch("/users/me/bank-details", json=bank_payload, headers=headers)
    assert bd.status_code == 200, bd.text
    # Fund the wallet so invoice creation (3% fee) isn't blocked by the
    # ₦60 starter balance.
    from app.db.session import SessionLocal
    from app.models import models as _m

    _s = SessionLocal()
    try:
        _u = _s.query(_m.User).filter(_m.User.phone == phone).first()
        if _u is not None:
            _u.wallet_balance_kobo = 10_000_000
            _s.commit()
    finally:
        _s.close()
    return headers


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_create_list_invoice_auth_flow():
    client = TestClient(app)
    headers = _signup_and_get_token(client)
    resp = client.post(
        "/invoices/",
        json={
            "customer_name": "Test Customer",
            "amount": "1500",
            "lines": [{"description": "Service", "quantity": 1, "unit_price": "1500"}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    invoice_id = resp.json()["invoice_id"]
    resp2 = client.get("/invoices/", headers=headers)
    assert resp2.status_code == 200
    assert any(inv["invoice_id"] == invoice_id for inv in resp2.json()["items"])


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_invoice_detail_status_flow():
    client = TestClient(app)
    headers = _signup_and_get_token(client)
    create_resp = client.post(
        "/invoices/",
        json={
            "customer_name": "Detail Customer",
            "amount": "5000",
            "lines": [{"description": "Consulting", "quantity": 2, "unit_price": "2500"}],
        },
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    invoice_id = create_resp.json()["invoice_id"]

    detail = client.get(f"/invoices/{invoice_id}", headers=headers)
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert body["invoice_id"] == invoice_id
    assert body["customer"]["name"] == "Detail Customer"
    assert len(body["lines"]) == 1

    status_update = client.patch(
        f"/invoices/{invoice_id}",
        json={"status": "cancelled"},
        headers=headers,
    )
    assert status_update.status_code == 200, status_update.text
    assert status_update.json()["status"] == "cancelled"

    bad_status = client.patch(
        f"/invoices/{invoice_id}",
        json={"status": "unsupported"},
        headers=headers,
    )
    assert bad_status.status_code == 422

    # Events endpoint removed because invoices rely on manual confirmation


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_public_invoice_confirmation_flow():
    client = TestClient(app)
    headers = _signup_and_get_token(client)
    create_resp = client.post(
        "/invoices/",
        json={
            "customer_name": "Public Customer",
            "amount": "7500",
            "lines": [{"description": "Design", "quantity": 1, "unit_price": "7500"}],
        },
        headers=headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    invoice_id = create_resp.json()["invoice_id"]

    public_view = client.get(f"/public/invoices/{invoice_id}")
    assert public_view.status_code == 200, public_view.text
    # Without customer phone/email, invoice starts in awaiting_confirmation (manual payment tracking).
    assert public_view.json()["status"] == "awaiting_confirmation"

    confirm_resp = client.post(f"/public/invoices/{invoice_id}/confirm-transfer")
    assert confirm_resp.status_code == 200, confirm_resp.text
    assert confirm_resp.json()["status"] == "awaiting_confirmation"

    detail = client.get(f"/invoices/{invoice_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["status"] == "awaiting_confirmation"

    mark_paid = client.patch(
        f"/invoices/{invoice_id}",
        json={"status": "paid"},
        headers=headers,
    )
    assert mark_paid.status_code == 200, mark_paid.text
    assert mark_paid.json()["status"] == "paid"
