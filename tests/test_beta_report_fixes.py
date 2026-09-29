"""Regression tests for beta-report fixes.

Covers:
  * Expenses created as unified invoices (invoice_type='expense') show up in
    BOTH the invoice list AND the /expenses/stats/overview totals — the earlier
    bug where the stats endpoint queried the dead legacy Expense table (₦0).
  * Invoice line items with a non-positive unit price are rejected (422) instead
    of silently accepted.
"""

import datetime as dt
import json
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from app.api.main import app

client = TestClient(app)


def _signup_and_get_token(phone: str) -> str:
    r = client.post(
        "/auth/signup/request",
        json={
            "phone": phone,
            "email": f"{phone.lstrip('+')}@example.com",
            "name": "FixUser",
            "business_name": "Fix Biz",
            "accept_terms": True,
        },
    )
    assert r.status_code == 200, r.text

    from app.services.otp_service import OTPService

    svc = OTPService()
    raw = svc._store.get(f"otp:signup:{phone}")  # type: ignore[attr-defined]
    assert raw is not None
    code = json.loads(raw)["code"]

    v = client.post(
        "/auth/signup/verify",
        json={
            "phone": phone,
            "otp": code,
            "bank_name": "SuoOps Bank",
            "account_number": "0001234567",
            "account_name": "Fix Biz",
        },
    )
    assert v.status_code == 200, v.text
    token = v.json()["access_token"]

    # Fund the wallet so revenue-invoice fee checks don't get in the way.
    from app.db.session import SessionLocal
    from app.models import models as _m

    s = SessionLocal()
    try:
        u = s.query(_m.User).filter(_m.User.phone == phone).first()
        if u is not None:
            u.wallet_balance_kobo = 10_000_000
            s.commit()
    finally:
        s.close()
    return token


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_expense_appears_in_list_and_stats():
    token = _signup_and_get_token("+2349990002220")
    headers = _headers(token)

    today = dt.date.today()
    date_str = today.isoformat()

    # Create two expenses exactly like the dashboard Expenses page does.
    for amount in (1500, 2222):
        resp = client.post(
            "/invoices/",
            json={
                "invoice_type": "expense",
                "amount": amount,
                "due_date": date_str,
                "category": "supplies",
                "vendor_name": "Test Vendor",
                "lines": [{"description": "supplies", "quantity": 1, "unit_price": amount}],
                "status": "paid",
            },
            headers=headers,
        )
        assert resp.status_code == 200, resp.text

    # 1) The Expenses list (unified invoice endpoint) must return them as items.
    start = today.replace(day=1).isoformat()
    end = today.isoformat()
    lst = client.get(
        "/invoices/",
        params={"invoice_type": "expense", "start_date": start, "end_date": end},
        headers=headers,
    )
    assert lst.status_code == 200, lst.text
    body = lst.json()
    assert "items" in body, "list endpoint must return a paginated envelope"
    amounts = sorted(float(i["amount"]) for i in body["items"])
    assert amounts == [1500.0, 2222.0], amounts

    # 2) The stats endpoint must total them (regression: used to read the dead
    #    legacy Expense table and return ₦0).
    stats = client.get(
        "/expenses/stats/overview",
        params={"period_type": "month", "year": today.year, "month": today.month},
        headers=headers,
    )
    assert stats.status_code == 200, stats.text
    assert float(stats.json()["total_expenses"]) == 3722.0, stats.json()


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_zero_unit_price_rejected():
    token = _signup_and_get_token("+2349990002221")
    headers = _headers(token)

    resp = client.post(
        "/invoices/",
        json={
            "customer_name": "Zero Priced",
            "amount": 0,
            "lines": [{"description": "Freebie", "quantity": 1, "unit_price": 0}],
        },
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_positive_unit_price_accepted():
    token = _signup_and_get_token("+2349990002222")
    headers = _headers(token)

    resp = client.post(
        "/invoices/",
        json={
            "customer_name": "Valid Line",
            "amount": 2500,
            "lines": [{"description": "Service", "quantity": 1, "unit_price": 2500}],
        },
        headers=headers,
    )
    assert resp.status_code == 200, resp.text


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_whatsapp_style_expense_shows_on_dashboard():
    """An expense recorded via the shared helper (as WhatsApp/OCR do) must be
    visible on the dashboard invoice list + expense stats — no more data split."""
    import datetime as dt

    from app.db.session import SessionLocal
    from app.models import models as _m
    from app.services.expense_service import record_expense_invoice

    phone = "+2349990002223"
    token = _signup_and_get_token(phone)
    headers = _headers(token)

    s = SessionLocal()
    try:
        user = s.query(_m.User).filter(_m.User.phone == phone).first()
        assert user is not None
        record_expense_invoice(
            s,
            user_id=user.id,
            amount=4200,
            category="transport",
            description="fuel",
            merchant="Total",
            expense_date=dt.date.today(),
            input_method="text",
            channel="whatsapp",
        )
    finally:
        s.close()

    today = dt.date.today()
    lst = client.get(
        "/invoices/",
        params={
            "invoice_type": "expense",
            "start_date": today.replace(day=1).isoformat(),
            "end_date": today.isoformat(),
        },
        headers=headers,
    )
    assert lst.status_code == 200, lst.text
    amounts = [float(i["amount"]) for i in lst.json()["items"]]
    assert 4200.0 in amounts, amounts

    stats = client.get(
        "/expenses/stats/overview",
        params={"period_type": "month", "year": today.year, "month": today.month},
        headers=headers,
    )
    assert stats.status_code == 200, stats.text
    assert float(stats.json()["total_expenses"]) == 4200.0, stats.json()

    # The legacy /expenses CRUD list now reads the same unified data too.
    legacy_list = client.get("/expenses/", headers=headers)
    assert legacy_list.status_code == 200, legacy_list.text
    assert any(float(e["amount"]) == 4200.0 for e in legacy_list.json())


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_public_expense_payload_cannot_self_verify_or_inject_receipt():
    token = _signup_and_get_token("+2349990002224")
    headers = _headers(token)

    response = client.post(
        "/expenses/",
        json={
            "amount": 6400,
            "expense_date": dt.date.today().isoformat(),
            "category": "utilities",
            "merchant": "Power Co",
            "verified": True,
            "receipt_url": "https://attacker.invalid/fake-receipt.pdf",
        },
        headers=headers,
    )

    assert response.status_code == 201, response.text
    expense = response.json()
    assert expense["verified"] is False
    assert expense["receipt_url"] is None
    assert expense["record_status"] == "self_reported"


@patch("app.workers.tasks.generate_invoice_pdf_async.delay", MagicMock())
def test_exact_duplicate_expense_is_accepted_and_flagged():
    token = _signup_and_get_token("+2349990002225")
    headers = _headers(token)
    payload = {
        "amount": 8500,
        "expense_date": dt.date.today().isoformat(),
        "category": "transport",
        "merchant": "Metro Fuel",
        "description": "Delivery fuel",
    }

    first = client.post("/expenses/", json=payload, headers=headers)
    second = client.post("/expenses/", json=payload, headers=headers)

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["possible_duplicate"] is False
    assert second.json()["possible_duplicate"] is True
    assert second.json()["possible_duplicate_of_id"] == first.json()["id"]
    assert second.json()["record_status"] == "flagged"

    listed = client.get("/expenses/", headers=headers)
    assert listed.status_code == 200, listed.text
    duplicate = next(item for item in listed.json() if item["id"] == second.json()["id"])
    assert duplicate["possible_duplicate"] is True
