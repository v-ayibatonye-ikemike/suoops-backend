import datetime as dt
from decimal import Decimal

import pytest

from app.models import models
from app.models.inventory_models import Product
from app.workers.tasks import engagement_tasks, growth_tasks, maintenance_tasks, welcome_tasks


@pytest.fixture
def forbid_whatsapp(monkeypatch):
    def fail():
        raise AssertionError("activation marketing must not acquire a WhatsApp client")

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", fail)


def _user(db, *, email="owner@example.com", created_at=None):
    user = models.User(
        name="Ada Owner",
        email=email,
        phone="+2348012345678",
        phone_verified=True,
        created_at=created_at or dt.datetime.now(dt.timezone.utc),
    )
    db.add(user)
    db.commit()
    return user


def _invoice(db, user, *, status="paid"):
    customer = models.Customer(name="Customer")
    db.add(customer)
    db.flush()
    invoice = models.Invoice(
        invoice_id=f"INV-{user.id}-{customer.id}-{status}",
        issuer_id=user.id,
        customer_id=customer.id,
        amount=Decimal("30000"),
        status=status,
        invoice_type="revenue",
    )
    db.add(invoice)
    db.commit()
    return invoice


def test_welcome_activation_uses_email_not_whatsapp(db_session, monkeypatch, forbid_whatsapp):
    user = _user(db_session)
    monkeypatch.setattr(welcome_tasks, "_send_email", lambda *args, **kwargs: True)

    result = welcome_tasks.send_instant_welcome(user.id)

    assert result == {"email_sent": True, "whatsapp_sent": False}


def test_engagement_activation_has_no_whatsapp_fallback(db_session, forbid_whatsapp):
    user = _user(db_session, email=None)
    stats = {"activation_sent": 0, "whatsapp_sent": 0, "skipped": 0, "failed": 0}

    engagement_tasks._send_zero_invoice_nudge(db_session, user, "Ada", 7, stats)

    assert stats["whatsapp_sent"] == 0
    assert stats["failed"] == 1


def test_growth_marketing_has_no_whatsapp_fallback(db_session, forbid_whatsapp):
    user = _user(db_session, email=None)
    _invoice(db_session, user)
    _invoice(db_session, user)

    result = growth_tasks.send_payment_upsells()

    assert result["whatsapp_sent"] == 0
    assert result["failed"] == 1


def test_storefront_nudge_uses_email_not_whatsapp(db_session, monkeypatch, forbid_whatsapp):
    user = _user(
        db_session,
        created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2),
    )
    db_session.add(
        Product(
            user_id=user.id,
            sku="STORE-1",
            name="Store product",
            selling_price=Decimal("1000"),
        )
    )
    db_session.commit()
    monkeypatch.setattr(growth_tasks, "_send_smtp_email", lambda *args, **kwargs: True)

    result = growth_tasks.send_storefront_completion_nudges()

    assert result["email_sent"] == 1
    assert result["whatsapp_sent"] == 0


def test_maintenance_activation_tasks_do_not_call_whatsapp(db_session, forbid_whatsapp):
    now = dt.datetime.now(dt.timezone.utc)
    user = _user(db_session, email=None, created_at=now - dt.timedelta(days=35))
    user.last_login = now - dt.timedelta(days=35)
    for _ in range(3):
        _invoice(db_session, user, status="pending")
    db_session.add(
        Product(
            user_id=user.id,
            sku="SKU-1",
            name="Product",
            selling_price=Decimal("1000"),
        )
    )
    db_session.commit()

    winback = maintenance_tasks.winback_churned_businesses()
    activation = maintenance_tasks.nudge_zero_invoice_users()

    assert winback["failed"] == 1
    assert activation["sent_1d"] == activation["sent_3d"] == activation["sent_7d"] == 0
