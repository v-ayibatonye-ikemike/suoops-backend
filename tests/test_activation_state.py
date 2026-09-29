from decimal import Decimal

from app.api import routes_auth
from app.api.main import app
from app.models import models
from app.models.inventory_models import Product


def _get_activation_state(client, user_id: int):
    app.dependency_overrides[routes_auth.get_current_user_id] = lambda: user_id
    try:
        return client.get("/users/me/activation-state", headers={"Authorization": "Bearer test"})
    finally:
        app.dependency_overrides.pop(routes_auth.get_current_user_id, None)


def test_activation_state_for_new_user(client, db_session):
    user = models.User(name="New Owner", email="new@example.com")
    db_session.add(user)
    db_session.commit()

    response = _get_activation_state(client, user.id)

    assert response.status_code == 200
    assert response.json() == {
        "business_profile_ready": False,
        "bank_details_ready": False,
        "storefront_enabled": False,
        "storefront_profile_ready": False,
        "product_count": 0,
        "online_payments_enabled": False,
        "invoice_count": 0,
        "paid_invoice_count": 0,
        "progress_percent": 0,
    }


def test_activation_state_progresses_and_excludes_expenses(client, db_session):
    user = models.User(
        name="Ada Owner",
        email="ada@example.com",
        business_name="Ada Foods",
        bank_name="Test Bank",
        account_number="0123456789",
        account_name="Ada Owner",
    )
    customer = models.Customer(name="Customer")
    db_session.add_all([user, customer])
    db_session.flush()
    db_session.commit()

    partial_response = _get_activation_state(client, user.id)
    assert partial_response.status_code == 200
    assert partial_response.json()["progress_percent"] == 33
    assert partial_response.json()["business_profile_ready"] is True
    assert partial_response.json()["bank_details_ready"] is True
    assert partial_response.json()["storefront_profile_ready"] is False

    user.paystack_subaccount_active = True
    user.storefront_enabled = True
    user.storefront_slug = "ada-foods"
    user.storefront_description = "Fresh meals"
    user.storefront_state = "Lagos"
    user.logo_url = "https://assets.example/logo.png"
    db_session.add_all(
        [
            Product(
                user_id=user.id,
                sku="MEAL-1",
                name="Meal",
                description="A fresh meal",
                selling_price=Decimal("5000"),
                image_url="https://assets.example/meal.png",
            ),
            Product(
                user_id=user.id,
                sku="OLD-1",
                name="Archived item",
                selling_price=Decimal("1000"),
                is_active=False,
            ),
            models.Invoice(
                invoice_id="INV-PENDING",
                issuer_id=user.id,
                customer_id=customer.id,
                amount=Decimal("5000"),
                status="pending",
                invoice_type="revenue",
            ),
            models.Invoice(
                invoice_id="INV-PAID",
                issuer_id=user.id,
                customer_id=customer.id,
                amount=Decimal("7000"),
                status="paid",
                invoice_type="revenue",
            ),
            models.Invoice(
                invoice_id="EXP-PAID",
                issuer_id=user.id,
                customer_id=customer.id,
                amount=Decimal("3000"),
                status="paid",
                invoice_type="expense",
            ),
        ]
    )
    db_session.commit()

    response = _get_activation_state(client, user.id)

    assert response.status_code == 200
    assert response.json() == {
        "business_profile_ready": True,
        "bank_details_ready": True,
        "storefront_enabled": True,
        "storefront_profile_ready": True,
        "product_count": 2,
        "online_payments_enabled": True,
        "invoice_count": 2,
        "paid_invoice_count": 1,
        "progress_percent": 100,
    }
