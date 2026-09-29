"""Coverage-focused tests for app.workers.tasks.growth_tasks.

These tests seed the in-memory test DB (via the ``db_session`` fixture from
tests/conftest.py) and call the Celery task functions directly. All external
I/O (WhatsApp client, SMTP email, budget/redis helpers, conversation window)
is patched so nothing hits the network.

The task functions open their OWN DB session internally via SessionLocal, which
conftest rebinds to the test engine — so committed seed data is visible.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.models import models
from app.models.models import SubscriptionPlan
from app.workers.tasks import growth_tasks

_counter = itertools.count(1)


# ── Seed helpers ─────────────────────────────────────────────────────


def _make_user(
    db,
    *,
    plan=SubscriptionPlan.FREE,
    phone="+2348030000000",
    email="owner@example.com",
    name="Ada Obi",
):
    n = next(_counter)
    if phone is not None:
        phone = f"{phone[:-3]}{n:03d}"
    user = models.User(
        phone=phone,
        email=email,
        name=name,
        business_name="Ada Stores",
        plan=plan,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_customer(db, *, name="Chidi", phone="+2348090000000", email="cust@example.com"):
    n = next(_counter)
    cust = models.Customer(name=name, phone=f"{phone[:-3]}{n:03d}" if phone else None, email=email)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    return cust


def _make_invoice(
    db,
    issuer,
    customer,
    *,
    amount=10000,
    status="pending",
    invoice_type="revenue",
    due_date=None,
    paid_at=None,
    created_at=None,
):
    n = next(_counter)
    inv = models.Invoice(
        invoice_id=f"INV-{n:06d}",
        issuer_id=issuer.id,
        customer_id=customer.id,
        amount=amount,
        status=status,
        invoice_type=invoice_type,
        due_date=due_date,
        paid_at=paid_at,
        created_at=created_at or datetime.now(timezone.utc),
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@pytest.fixture
def patch_externals(monkeypatch):
    """Patch WhatsApp/email/budget externals. Returns the mock client + a dict.

    Callers can tweak client.send_template.return_value etc. and the toggles.
    """
    client = MagicMock()
    client.send_template.return_value = True
    client.send_text.return_value = True

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client, raising=True)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: True, raising=True)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True, raising=True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: 1, raising=True)
    # growth_tasks imports _send_smtp_email at module import time.
    smtp = MagicMock(return_value=True)
    monkeypatch.setattr(growth_tasks, "_send_smtp_email", smtp, raising=True)

    return {"client": client, "smtp": smtp}


# ═══════════════════════════════════════════════════════════════════════
# TASK 1: send_aggregate_unpaid_alerts
# ═══════════════════════════════════════════════════════════════════════


def test_aggregate_unpaid_no_data(db_session, patch_externals):
    result = growth_tasks.send_aggregate_unpaid_alerts()
    assert result["success"] is True
    assert result["whatsapp_sent"] == 0
    assert result["email_sent"] == 0


def test_aggregate_unpaid_email_fallback(db_session, patch_externals):
    # User with no valid phone -> email fallback path.
    user = _make_user(db_session, phone=None, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=4000)
    _make_invoice(db_session, user, cust, amount=5000)

    result = growth_tasks.send_aggregate_unpaid_alerts()

    assert result["success"] is True
    assert result["email_sent"] == 1
    assert patch_externals["smtp"].called


def test_aggregate_unpaid_whatsapp_template(db_session, patch_externals, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_UNPAID_ALERT", "unpaid_tpl", raising=False)
    user = _make_user(db_session, email=None)
    cust = _make_customer(db_session, email=None)
    _make_invoice(db_session, user, cust, amount=6000)
    _make_invoice(db_session, user, cust, amount=7000, status="awaiting_confirmation")

    result = growth_tasks.send_aggregate_unpaid_alerts()

    assert result["success"] is True
    assert result["whatsapp_sent"] == 1
    patch_externals["client"].send_template.assert_called_once()


def test_aggregate_unpaid_whatsapp_text_no_template(db_session, patch_externals, monkeypatch):
    # No template configured -> falls back to send_text within window.
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_UNPAID_ALERT", None, raising=False)
    user = _make_user(db_session, email=None)
    cust = _make_customer(db_session, email=None)
    _make_invoice(db_session, user, cust, amount=6000)
    _make_invoice(db_session, user, cust, amount=7000)

    result = growth_tasks.send_aggregate_unpaid_alerts()

    assert result["whatsapp_sent"] == 1
    patch_externals["client"].send_text.assert_called_once()


def test_aggregate_unpaid_dedup_skip(db_session, patch_externals):
    user = _make_user(db_session, phone=None, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=6000)
    _make_invoice(db_session, user, cust, amount=7000)
    # Recent dedup row -> skip.
    db_session.add(models.UserEmailLog(user_id=user.id, email_type="aggregate_unpaid"))
    db_session.commit()

    result = growth_tasks.send_aggregate_unpaid_alerts()

    assert result["skipped"] == 1
    assert result["email_sent"] == 0


def test_aggregate_unpaid_all_channels_fail(db_session, patch_externals):
    # No email, invalid phone, so nothing sends -> failed, dedup row removed.
    user = _make_user(db_session, phone=None, email=None)
    cust = _make_customer(db_session, email=None)
    _make_invoice(db_session, user, cust, amount=6000)
    _make_invoice(db_session, user, cust, amount=7000)

    result = growth_tasks.send_aggregate_unpaid_alerts()

    assert result["failed"] == 1
    # Dedup row should have been deleted so next run can retry.
    remaining = db_session.query(models.UserEmailLog).filter(models.UserEmailLog.user_id == user.id).count()
    assert remaining == 0


def test_aggregate_unpaid_email_send_fails(db_session, patch_externals):
    patch_externals["smtp"].return_value = False
    user = _make_user(db_session, phone=None, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=6000)
    _make_invoice(db_session, user, cust, amount=7000)

    result = growth_tasks.send_aggregate_unpaid_alerts()

    assert result["failed"] == 1
    assert result["email_sent"] == 0


# ═══════════════════════════════════════════════════════════════════════
# TASK 2: send_weekly_free_summary
# ═══════════════════════════════════════════════════════════════════════


def test_weekly_free_summary_no_users(db_session, patch_externals):
    result = growth_tasks.send_weekly_free_summary()
    assert result["success"] is True
    assert result["email_sent"] == 0


def test_weekly_free_summary_email_path(db_session, patch_externals):
    # Free user with email + revenue this week -> prefers email.
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    now = datetime.now(timezone.utc)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=15000,
        status="paid",
        paid_at=now - timedelta(days=1),
    )
    _make_invoice(
        db_session,
        user,
        cust,
        amount=3000,
        invoice_type="expense",
        created_at=now - timedelta(days=2),
    )
    _make_invoice(db_session, user, cust, amount=9000, status="pending")

    result = growth_tasks.send_weekly_free_summary()

    assert result["success"] is True
    assert result["email_sent"] == 1
    assert patch_externals["smtp"].called


def test_weekly_free_summary_skip_zero_activity(db_session, patch_externals):
    # Free user with only a cancelled invoice (counts for join, zero activity).
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=5000, status="cancelled")

    result = growth_tasks.send_weekly_free_summary()

    assert result["skipped"] == 1
    assert result["email_sent"] == 0


def test_weekly_free_summary_whatsapp_path(db_session, patch_externals):
    # Free user with phone and NO email -> WhatsApp text within window.
    user = _make_user(db_session, email=None)
    cust = _make_customer(db_session, email=None)
    _make_invoice(db_session, user, cust, amount=20000, status="pending")

    result = growth_tasks.send_weekly_free_summary()

    assert result["whatsapp_sent"] == 1
    patch_externals["client"].send_text.assert_called()


def test_weekly_free_summary_email_send_fails(db_session, patch_externals):
    patch_externals["smtp"].return_value = False
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=12000, status="pending")

    result = growth_tasks.send_weekly_free_summary()

    assert result["failed"] == 1


# ═══════════════════════════════════════════════════════════════════════
# TASK 3: send_payment_upsells
# ═══════════════════════════════════════════════════════════════════════


def test_payment_upsell_no_data(db_session, patch_externals):
    result = growth_tasks.send_payment_upsells()
    assert result["success"] is True
    assert result["email_sent"] == 0


def test_payment_upsell_email_path(db_session, patch_externals):
    # Free user collected >= 50000 -> email upsell.
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=60000, status="paid")

    result = growth_tasks.send_payment_upsells()

    assert result["email_sent"] == 1
    # dedup row created
    assert db_session.query(models.UserEmailLog).filter(models.UserEmailLog.email_type == "payment_upsell").count() == 1


def test_payment_upsell_phone_only_does_not_use_whatsapp(db_session, patch_externals, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_UPSELL", "upsell_tpl", raising=False)
    # Free user, phone only, >=2 payments.
    user = _make_user(db_session, email=None)
    cust = _make_customer(db_session, email=None)
    _make_invoice(db_session, user, cust, amount=10000, status="paid")
    _make_invoice(db_session, user, cust, amount=12000, status="paid")

    result = growth_tasks.send_payment_upsells()

    assert result["whatsapp_sent"] == 0
    assert result["failed"] == 1
    patch_externals["client"].send_template.assert_not_called()


def test_payment_upsell_has_no_whatsapp_text_fallback(db_session, patch_externals, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_UPSELL", None, raising=False)
    user = _make_user(db_session, email=None)
    cust = _make_customer(db_session, email=None)
    _make_invoice(db_session, user, cust, amount=10000, status="paid")
    _make_invoice(db_session, user, cust, amount=12000, status="paid")

    result = growth_tasks.send_payment_upsells()

    assert result["whatsapp_sent"] == 0
    assert result["failed"] == 1
    patch_externals["client"].send_text.assert_not_called()


def test_payment_upsell_skip_non_free(db_session, patch_externals):
    user = _make_user(db_session, plan=SubscriptionPlan.PRO, email="pro@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=60000, status="paid")

    result = growth_tasks.send_payment_upsells()

    assert result["skipped"] == 1
    assert result["email_sent"] == 0


def test_payment_upsell_dedup_skip(db_session, patch_externals):
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=60000, status="paid")
    db_session.add(models.UserEmailLog(user_id=user.id, email_type="payment_upsell"))
    db_session.commit()

    result = growth_tasks.send_payment_upsells()

    assert result["skipped"] == 1
    assert result["email_sent"] == 0


def test_payment_upsell_email_send_fails(db_session, patch_externals):
    patch_externals["smtp"].return_value = False
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=60000, status="paid")

    result = growth_tasks.send_payment_upsells()

    assert result["failed"] == 1
