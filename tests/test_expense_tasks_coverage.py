"""Focused coverage for expense habit reminders."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from app.models import models
from app.workers.tasks import expense_tasks


def _make_user(db, index: int, *, email: str | None = "owner@example.com"):
    user = models.User(
        name=f"Owner {index}",
        phone=f"+2348012345{index:03d}",
        email=email,
        business_name=f"Business {index}",
    )
    db.add(user)
    db.commit()
    return user


def _add_invoice(db, user, index: int, *, invoice_type: str):
    customer = models.Customer(
        name=f"Customer {index}",
        phone=f"+2348098765{index:03d}",
        email=f"customer{index}@example.com",
    )
    db.add(customer)
    db.flush()
    invoice = models.Invoice(
        invoice_id=f"EXP-HABIT-{index}",
        issuer_id=user.id,
        customer_id=customer.id,
        amount=5000,
        status="paid",
        invoice_type=invoice_type,
        due_date=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db.add(invoice)
    db.commit()
    return invoice


def test_expense_reminder_includes_business_with_no_expenses(monkeypatch, db_session):
    user = _make_user(db_session, 1)
    _add_invoice(db_session, user, 1, invoice_type="revenue")
    batches = []
    monkeypatch.setattr(
        "app.utils.smtp.send_smtp_batch",
        lambda batch: batches.append(batch) or [True] * len(batch),
    )
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", MagicMock)

    result = expense_tasks.send_expense_reminders()

    assert result["email_sent"] == 1
    assert result["users_targeted"] == 1
    assert "Sales are not the same as profit" in batches[0][0][3]
    assert db_session.query(models.UserEmailLog).filter_by(user_id=user.id).first()


def test_expense_reminder_skips_recent_expense(monkeypatch, db_session):
    user = _make_user(db_session, 2)
    _add_invoice(db_session, user, 2, invoice_type="revenue")
    _add_invoice(db_session, user, 3, invoice_type="expense")
    batch = MagicMock(return_value=[])
    monkeypatch.setattr("app.utils.smtp.send_smtp_batch", batch)

    result = expense_tasks.send_expense_reminders()

    assert result["users_targeted"] == 0
    batch.assert_not_called()


def test_expense_reminder_uses_open_whatsapp_window(monkeypatch, db_session):
    user = _make_user(db_session, 3, email=None)
    _add_invoice(db_session, user, 4, invoice_type="revenue")
    client = MagicMock()
    client.send_text.return_value = True
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: None)

    result = expense_tasks.send_expense_reminders()

    assert result["whatsapp_sent"] == 1
    client.send_text.assert_called_once()
