"""Tests for subscription endpoint response schemas.

Ensures response_model filtering prevents leaking sensitive fields
like Paystack subscription codes, transaction IDs, and IP addresses.
"""

import secrets

from fastapi.testclient import TestClient

from app.api.main import app
from app.core.security import create_access_token
from app.db.session import SessionLocal
from app.models import models


def _create_test_user(db) -> models.User:
    """Create a minimal test user for subscription tests."""
    user = models.User(
        phone="+234" + secrets.token_hex(4),
        name="Sub Test User",
        email=f"subtest-{secrets.token_hex(3)}@test.com",
        plan=models.SubscriptionPlan.FREE,
        invoice_balance=10,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _auth_headers(user_id: int) -> dict[str, str]:
    token = create_access_token(str(user_id))
    return {"Authorization": f"Bearer {token}"}


class TestSubscriptionStatus:
    def test_status_excludes_subscription_code(self):
        """subscription_code must NOT appear in /status response."""
        client = TestClient(app)
        db = SessionLocal()
        try:
            user = _create_test_user(db)
            # Simulate a Paystack subscription code on the user
            user.paystack_subscription_code = "SUB_fakecode123"
            db.commit()

            resp = client.get("/subscriptions/status", headers=_auth_headers(user.id))
            assert resp.status_code == 200
            data = resp.json()

            # Must NOT leak the Paystack subscription code
            assert "subscription_code" not in data
            # Should still show plan info
            assert data["plan"] == "free"
            # User has a subscription code set, so is_recurring should be True
            assert data["is_recurring"] is True
            assert "invoice_balance" in data
        finally:
            db.close()


class TestCommissionModel:
    def test_new_subscription_sales_are_disabled(self):
        client = TestClient(app)
        db = SessionLocal()
        try:
            user = _create_test_user(db)
            headers = _auth_headers(user.id)

            assert client.post("/subscriptions/initialize?plan=PRO", headers=headers).status_code in (404, 405)
            assert client.get("/subscriptions/verify/legacy-reference", headers=headers).status_code == 404
        finally:
            db.close()

    def test_all_features_and_voice_are_available_without_a_plan(self):
        from app.utils.feature_gate import FeatureGate

        client = TestClient(app)
        db = SessionLocal()
        try:
            user = _create_test_user(db)
            gate = FeatureGate(db, user.id)

            assert gate.can_use_voice() == (True, None)
            gate.check_voice_quota()

            response = client.get("/users/me/features", headers=_auth_headers(user.id))
            assert response.status_code == 200
            body = response.json()
            assert body["current_plan"] == "commission"
            assert body["upgrade_available"] is False
            assert body["upgrade_url"] is None
            assert all(
                body["features"][feature]
                for feature in (
                    "voice_invoice",
                    "custom_branding",
                    "inventory",
                    "team_management",
                    "tax_reports",
                )
            )
        finally:
            db.close()


class TestPaymentHistory:
    def test_payment_detail_excludes_sensitive_fields(self):
        """paystack_transaction_id, payment_metadata, ip_address must be excluded."""
        from app.models.payment_models import (
            PaymentProvider,
            PaymentStatus,
            PaymentTransaction,
        )

        client = TestClient(app)
        db = SessionLocal()
        try:
            user = _create_test_user(db)

            # Create a payment transaction with sensitive fields populated
            txn = PaymentTransaction(
                user_id=user.id,
                reference=f"TEST-{secrets.token_hex(6)}",
                amount=500000,  # 5000 NGN in kobo
                currency="NGN",
                plan_before="free",
                plan_after="pro",
                status=PaymentStatus.SUCCESS,
                provider=PaymentProvider.PAYSTACK,
                paystack_transaction_id="TXN_secret123",
                payment_metadata={"webhook": "raw_data"},
                ip_address="192.168.1.100",
                customer_email="test@test.com",
                customer_phone="+2340000000",
            )
            db.add(txn)
            db.commit()
            db.refresh(txn)

            resp = client.get(
                f"/subscriptions/history/{txn.id}",
                headers=_auth_headers(user.id),
            )
            assert resp.status_code == 200
            data = resp.json()

            # These sensitive fields must NOT be in the response
            assert "paystack_transaction_id" not in data
            assert "metadata" not in data
            assert "payment_metadata" not in data
            assert "ip_address" not in data
            assert "customer_phone" not in data

            # Safe fields should be present
            assert data["reference"] == txn.reference
            assert data["status"] == "success"
            assert data["provider"] == "paystack"
            assert data["plan_before"] == "free"
            assert data["plan_after"] == "pro"
        finally:
            db.close()

    def test_payment_list_returns_paginated(self):
        """GET /history should return paginated payment list."""
        client = TestClient(app)
        db = SessionLocal()
        try:
            user = _create_test_user(db)
            resp = client.get(
                "/subscriptions/history",
                headers=_auth_headers(user.id),
            )
            assert resp.status_code == 200
            data = resp.json()
            assert "payments" in data
            assert "total" in data
            assert "summary" in data
            assert isinstance(data["payments"], list)
        finally:
            db.close()


class TestSwitchPlan:
    def test_switch_to_starter_removed(self):
        """POST /switch-to-starter endpoint should no longer exist (STARTER plan removed)."""
        client = TestClient(app)
        db = SessionLocal()
        try:
            user = _create_test_user(db)
            resp = client.post(
                "/subscriptions/switch-to-starter",
                headers=_auth_headers(user.id),
            )
            # Endpoint removed — expect 404 or 405
            assert resp.status_code in (404, 405)
        finally:
            db.close()
