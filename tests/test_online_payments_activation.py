from app.core.security import create_access_token
from app.models import models
from app.services.paystack_subaccount_service import PaystackSubaccountService


def _create_user(db_session, *, with_bank: bool = True) -> models.User:
    user = models.User(
        phone="+2348012345678",
        email="payments@example.com",
        name="Payment User",
        business_name="Payment Store",
        bank_name="Test Bank" if with_bank else None,
        account_number="0123456789" if with_bank else None,
        account_name="PAYMENT USER" if with_bank else None,
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_online_payment_activation_endpoints(client, db_session, monkeypatch):
    user = _create_user(db_session)
    headers = {"Authorization": "Bearer " + create_access_token(str(user.id))}

    async def fake_ensure_subaccount(self, target):
        target.paystack_subaccount_code = "ACCT_test"
        target.paystack_subaccount_active = True
        self.db.commit()
        return "ACCT_test"

    monkeypatch.setattr(PaystackSubaccountService, "ensure_subaccount", fake_ensure_subaccount)

    status = client.get("/invoices/online-payments-status", headers=headers)
    assert status.status_code == 200
    assert status.json() == {"enabled": False, "has_bank_details": True}

    enabled = client.post("/invoices/enable-online-payments", headers=headers)
    assert enabled.status_code == 200
    assert enabled.json()["enabled"] is True
    assert enabled.json()["subaccount_code"] == "ACCT_test"

    active_status = client.get("/invoices/online-payments-status", headers=headers)
    assert active_status.json() == {"enabled": True, "has_bank_details": True}

    disabled = client.post("/invoices/disable-online-payments", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["enabled"] is False
    db_session.refresh(user)
    assert user.paystack_subaccount_active is False


def test_online_payment_activation_requires_verified_bank_details(client, db_session):
    user = _create_user(db_session, with_bank=False)
    headers = {"Authorization": "Bearer " + create_access_token(str(user.id))}

    response = client.post("/invoices/enable-online-payments", headers=headers)

    assert response.status_code == 400
    assert "settlement account" in response.json()["detail"].lower()
