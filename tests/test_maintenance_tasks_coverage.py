"""Coverage tests for app/workers/tasks/maintenance_tasks.py.

These exercise the periodic maintenance Celery tasks against the in-memory
SQLite DB provided by conftest. All external I/O (SMTP, WhatsApp, Brevo,
account deletion) is mocked. Tasks are called directly as plain functions.
"""

from __future__ import annotations

import datetime as dt
import itertools

import pytest

from app.core.config import settings
from app.models import models
from app.models.models import SubscriptionPlan
from app.workers.tasks import maintenance_tasks as mt

_counter = itertools.count(1)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _make_user(db, **kw):
    n = next(_counter)
    defaults = dict(
        name=kw.pop("name", "Test User"),
        email=kw.pop("email", f"user{n}@example.com"),
        phone=kw.pop("phone", f"+23480{n:08d}"),
    )
    defaults.update(kw)
    user = models.User(**defaults)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_customer(db, **kw):
    n = next(_counter)
    defaults = dict(name=kw.pop("name", f"Cust {n}"), phone=kw.pop("phone", f"+23470{n:08d}"))
    defaults.update(kw)
    c = models.Customer(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_invoice(db, issuer, customer, **kw):
    n = next(_counter)
    from decimal import Decimal

    defaults = dict(
        invoice_id=kw.pop("invoice_id", f"INV-{n:06d}"),
        issuer_id=issuer.id,
        customer_id=customer.id,
        amount=kw.pop("amount", Decimal("1000.00")),
        status=kw.pop("status", "pending"),
        invoice_type=kw.pop("invoice_type", "revenue"),
    )
    defaults.update(kw)
    inv = models.Invoice(**defaults)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


# ─────────────────────────────────────────────────────────────────────
# downgrade_expired_subscriptions
# ─────────────────────────────────────────────────────────────────────


def test_downgrade_expired_subscriptions_downgrades_expired(db_session):
    now = _now()
    _make_user(
        db_session,
        plan=SubscriptionPlan.PRO,
        subscription_expires_at=now - dt.timedelta(days=1),
    )
    # Not expired PRO — should stay
    _make_user(
        db_session,
        plan=SubscriptionPlan.PRO,
        subscription_expires_at=now + dt.timedelta(days=10),
    )
    # Free user — ignored
    _make_user(db_session, plan=SubscriptionPlan.FREE)

    result = mt.downgrade_expired_subscriptions()
    assert result == {"success": True, "downgraded": 1}


def test_downgrade_expired_subscriptions_empty(db_session):
    result = mt.downgrade_expired_subscriptions()
    assert result == {"success": True, "downgraded": 0}


# ─────────────────────────────────────────────────────────────────────
# cleanup_stale_webhooks
# ─────────────────────────────────────────────────────────────────────


def test_cleanup_stale_webhooks(db_session):
    old = models.WebhookEvent(
        provider="paystack",
        external_id="old-1",
        created_at=_now() - dt.timedelta(days=100),
    )
    recent = models.WebhookEvent(
        provider="paystack",
        external_id="new-1",
        created_at=_now() - dt.timedelta(days=1),
    )
    db_session.add_all([old, recent])
    db_session.commit()

    result = mt.cleanup_stale_webhooks()
    assert result == {"success": True, "deleted": 1}


# ─────────────────────────────────────────────────────────────────────
# cleanup_old_logs
# ─────────────────────────────────────────────────────────────────────


def test_cleanup_old_logs(db_session):
    user = _make_user(db_session)
    customer = _make_customer(db_session)
    inv = _make_invoice(db_session, user, customer)

    old_reminder = models.InvoiceReminderLog(
        invoice_id=inv.id,
        reminder_type="customer_overdue_7d",
        channel="email",
        recipient="a@b.com",
        sent_at=_now() - dt.timedelta(days=100),
    )
    fresh_reminder = models.InvoiceReminderLog(
        invoice_id=inv.id,
        reminder_type="customer_due_today",
        channel="email",
        recipient="a@b.com",
        sent_at=_now() - dt.timedelta(days=1),
    )
    old_email = models.UserEmailLog(
        user_id=user.id,
        email_type="old_type",
        sent_at=_now() - dt.timedelta(days=200),
    )
    fresh_email = models.UserEmailLog(
        user_id=user.id,
        email_type="fresh_type",
        sent_at=_now() - dt.timedelta(days=1),
    )
    db_session.add_all([old_reminder, fresh_reminder, old_email, fresh_email])
    db_session.commit()

    result = mt.cleanup_old_logs()
    assert result["success"] is True
    assert result["reminder_logs_deleted"] == 1
    assert result["email_logs_deleted"] == 1


# ─────────────────────────────────────────────────────────────────────
# warn_inactive_accounts
# ─────────────────────────────────────────────────────────────────────


def test_warn_inactive_accounts_empty(db_session):
    result = mt.warn_inactive_accounts()
    assert result == {"success": True, "warned": 0, "skipped": 0, "failed": 0}


def test_warn_inactive_accounts_sends_and_records(db_session, monkeypatch):
    now = _now()
    # Inactive free user, never logged in, created 100 days ago, has email, no invoices
    u = _make_user(
        db_session,
        plan=SubscriptionPlan.FREE,
        last_login=None,
        created_at=now - dt.timedelta(days=100),
        name="Jane Doe",
    )

    sent_batches = []

    def fake_batch(batch):
        sent_batches.append(batch)
        return [True] * len(batch)

    monkeypatch.setattr("app.utils.smtp.send_smtp_batch", fake_batch)

    result = mt.warn_inactive_accounts()
    assert result["success"] is True
    assert result["warned"] == 1
    assert result["failed"] == 0
    # UserEmailLog recorded
    log = db_session.query(models.UserEmailLog).filter(models.UserEmailLog.user_id == u.id).first()
    assert log is not None
    assert log.email_type == mt.WARNING_EMAIL_TYPE


def test_warn_inactive_accounts_skips_already_warned(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        plan=SubscriptionPlan.FREE,
        last_login=None,
        created_at=now - dt.timedelta(days=100),
    )
    db_session.add(models.UserEmailLog(user_id=u.id, email_type=mt.WARNING_EMAIL_TYPE))
    db_session.commit()

    monkeypatch.setattr("app.utils.smtp.send_smtp_batch", lambda batch: [True] * len(batch))
    result = mt.warn_inactive_accounts()
    assert result["success"] is True
    assert result["warned"] == 0
    assert result["skipped"] == 1


def test_warn_inactive_accounts_batch_failure_counts_failed(db_session, monkeypatch):
    now = _now()
    _make_user(
        db_session,
        plan=SubscriptionPlan.FREE,
        last_login=now - dt.timedelta(days=120),
        created_at=now - dt.timedelta(days=200),
    )
    monkeypatch.setattr("app.utils.smtp.send_smtp_batch", lambda batch: [False] * len(batch))
    result = mt.warn_inactive_accounts()
    assert result["success"] is True
    assert result["warned"] == 0
    assert result["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# delete_inactive_accounts
# ─────────────────────────────────────────────────────────────────────


def test_delete_inactive_accounts_empty(db_session):
    result = mt.delete_inactive_accounts()
    assert result == {"success": True, "deleted": 0, "skipped": 0, "failed": 0}


def test_delete_inactive_accounts_deletes_warned(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        plan=SubscriptionPlan.FREE,
        last_login=None,
        created_at=now - dt.timedelta(days=120),
    )
    db_session.add(
        models.UserEmailLog(
            user_id=u.id,
            email_type=mt.WARNING_EMAIL_TYPE,
            sent_at=now - dt.timedelta(days=8),
        )
    )
    db_session.commit()

    deleted_ids = []

    def fake_delete(self, user_id, deleted_by_user_id=None):
        deleted_ids.append(user_id)
        return {"success": True}

    monkeypatch.setattr(
        "app.services.account_deletion_service.AccountDeletionService.delete_account",
        fake_delete,
    )

    result = mt.delete_inactive_accounts()
    assert result["success"] is True
    assert result["deleted"] == 1
    assert deleted_ids == [u.id]


def test_delete_inactive_accounts_skips_if_logged_in_after_warning(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        plan=SubscriptionPlan.FREE,
        # Logged in AFTER the warning was sent
        last_login=now - dt.timedelta(days=1),
        created_at=now - dt.timedelta(days=120),
    )
    db_session.add(
        models.UserEmailLog(
            user_id=u.id,
            email_type=mt.WARNING_EMAIL_TYPE,
            sent_at=now - dt.timedelta(days=8),
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.account_deletion_service.AccountDeletionService.delete_account",
        lambda self, user_id, deleted_by_user_id=None: {"success": True},
    )

    result = mt.delete_inactive_accounts()
    assert result["success"] is True
    assert result["deleted"] == 0
    assert result["skipped"] == 1
    # Warning log cleaned up
    remaining = db_session.query(models.UserEmailLog).filter(models.UserEmailLog.user_id == u.id).count()
    assert remaining == 0


def test_delete_inactive_accounts_handles_delete_failure(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        plan=SubscriptionPlan.FREE,
        last_login=None,
        created_at=now - dt.timedelta(days=120),
    )
    db_session.add(
        models.UserEmailLog(
            user_id=u.id,
            email_type=mt.WARNING_EMAIL_TYPE,
            sent_at=now - dt.timedelta(days=8),
        )
    )
    db_session.commit()

    def boom(self, user_id, deleted_by_user_id=None):
        raise RuntimeError("deletion failed")

    monkeypatch.setattr(
        "app.services.account_deletion_service.AccountDeletionService.delete_account",
        boom,
    )

    result = mt.delete_inactive_accounts()
    assert result["success"] is True
    assert result["deleted"] == 0
    assert result["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# reconcile_brevo_contacts
# ─────────────────────────────────────────────────────────────────────


def test_reconcile_brevo_contacts_fetch_failed(db_session, monkeypatch):
    monkeypatch.setattr("app.services.brevo_service.get_all_contacts_sync", lambda list_id: None)
    result = mt.reconcile_brevo_contacts()
    assert result["success"] is False
    assert result["reason"] == "brevo_fetch_failed"


def test_reconcile_brevo_contacts_nothing_to_remove(db_session, monkeypatch):
    _make_user(db_session, email="keep@example.com")

    monkeypatch.setattr(
        "app.services.brevo_service.get_all_contacts_sync",
        lambda list_id: {"keep@example.com": False},
    )
    result = mt.reconcile_brevo_contacts()
    assert result["success"] is True
    assert result["stale"] == 0
    assert result["removed"] == 0


def test_reconcile_brevo_contacts_dry_run(db_session, monkeypatch):
    _make_user(db_session, email="keep@example.com")

    # stale (not in DB) + one suppressed stale
    monkeypatch.setattr(
        "app.services.brevo_service.get_all_contacts_sync",
        lambda list_id: {
            "keep@example.com": False,
            "stale@example.com": False,
            "suppressed@example.com": True,
        },
    )
    deleted = []
    monkeypatch.setattr(
        "app.services.brevo_service.delete_contact_sync",
        lambda email: deleted.append(email) or True,
    )

    result = mt.reconcile_brevo_contacts(dry_run=True)
    assert result["success"] is True
    assert result["stale"] == 2
    assert result["kept_suppressed"] == 1
    assert result["removed"] == 0
    assert deleted == []  # dry run does not delete


def test_reconcile_brevo_contacts_removes_stale(db_session, monkeypatch):
    _make_user(db_session, email="keep@example.com")

    monkeypatch.setattr(
        "app.services.brevo_service.get_all_contacts_sync",
        lambda list_id: {
            "keep@example.com": False,
            "stale1@example.com": False,
            "stale2@example.com": False,
            "suppressed@example.com": True,
        },
    )
    deleted = []
    monkeypatch.setattr(
        "app.services.brevo_service.delete_contact_sync",
        lambda email: (deleted.append(email) or True),
    )

    result = mt.reconcile_brevo_contacts(dry_run=False)
    assert result["success"] is True
    assert result["stale"] == 3
    assert result["kept_suppressed"] == 1
    assert result["removed"] == 2
    assert set(deleted) == {"stale1@example.com", "stale2@example.com"}


# ─────────────────────────────────────────────────────────────────────
# winback_churned_businesses
# ─────────────────────────────────────────────────────────────────────


def _seed_churned_user(db, days_inactive: int, invoice_count: int = 3, **user_kw):
    from decimal import Decimal

    now = _now()
    u = _make_user(
        db,
        last_login=now - dt.timedelta(days=days_inactive),
        business_name=user_kw.pop("business_name", "Churn Biz"),
        **user_kw,
    )
    cust = _make_customer(db)
    for i in range(invoice_count):
        status = "paid" if i == 0 else "pending"
        _make_invoice(db, u, cust, amount=Decimal("2500.00"), status=status)
    return u


def test_winback_empty(db_session):
    result = mt.winback_churned_businesses()
    assert result == {
        "success": True,
        "sent_30d": 0,
        "sent_60d": 0,
        "sent_90d": 0,
        "skipped": 0,
        "failed": 0,
    }


def test_winback_tier_30(db_session, monkeypatch):
    _seed_churned_user(db_session, days_inactive=35)
    monkeypatch.setattr("app.utils.smtp.send_smtp_email", lambda *a, **k: True)
    result = mt.winback_churned_businesses()
    assert result["sent_30d"] == 1


def test_winback_tier_60(db_session, monkeypatch):
    _seed_churned_user(db_session, days_inactive=65)
    monkeypatch.setattr("app.utils.smtp.send_smtp_email", lambda *a, **k: True)
    result = mt.winback_churned_businesses()
    assert result["sent_60d"] == 1


def test_winback_tier_90(db_session, monkeypatch):
    _seed_churned_user(db_session, days_inactive=95)
    monkeypatch.setattr("app.utils.smtp.send_smtp_email", lambda *a, **k: True)
    result = mt.winback_churned_businesses()
    assert result["sent_90d"] == 1


def test_winback_skips_already_sent(db_session, monkeypatch):
    u = _seed_churned_user(db_session, days_inactive=35)
    db_session.add(models.UserEmailLog(user_id=u.id, email_type=mt.CHURN_EMAIL_TYPES[30]))
    db_session.commit()
    monkeypatch.setattr("app.utils.smtp.send_smtp_email", lambda *a, **k: True)
    result = mt.winback_churned_businesses()
    assert result["skipped"] == 1
    assert result["sent_30d"] == 0


def test_winback_failed_when_no_channel(db_session, monkeypatch):
    # No email, no verified phone -> can't send
    _seed_churned_user(db_session, days_inactive=35, email=None, phone=None, phone_verified=False)
    monkeypatch.setattr("app.utils.smtp.send_smtp_email", lambda *a, **k: False)
    result = mt.winback_churned_businesses()
    assert result["failed"] == 1


def test_winback_has_no_whatsapp_fallback(db_session, monkeypatch):
    # No email: promotional winback must not fall back to WhatsApp.
    _seed_churned_user(
        db_session,
        days_inactive=35,
        email=None,
        phone="+2348090000999",
        phone_verified=True,
    )
    monkeypatch.setattr("app.utils.smtp.send_smtp_email", lambda *a, **k: False)

    class FakeClient:
        def send_text(self, to, body):
            return True

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: FakeClient())
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda *a, **k: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda *a, **k: 1)

    result = mt.winback_churned_businesses()
    assert result["sent_30d"] == 0
    assert result["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# nudge_zero_invoice_users
# ─────────────────────────────────────────────────────────────────────


def test_nudge_zero_invoice_empty(db_session):
    result = mt.nudge_zero_invoice_users()
    assert result == {
        "success": True,
        "sent_1d": 0,
        "sent_3d": 0,
        "sent_7d": 0,
        "skipped": 0,
        "failed": 0,
    }


def _fake_wa(monkeypatch, send_text_ok=True, send_template_ok=True, can_send=True):
    class FakeClient:
        def send_text(self, to, body):
            return send_text_ok

        def send_template(self, to, name, lang, components):
            return send_template_ok

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: FakeClient())
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda *a, **k: can_send)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda *a, **k: 1)


@pytest.mark.parametrize("days,tier_key", [(1, "sent_1d"), (4, "sent_3d"), (8, "sent_7d")])
def test_nudge_zero_invoice_tiers(db_session, monkeypatch, days, tier_key):
    now = _now()
    _make_user(
        db_session,
        phone="+2348091110000",
        phone_verified=True,
        created_at=now - dt.timedelta(days=days),
    )
    _fake_wa(monkeypatch, send_text_ok=True)
    result = mt.nudge_zero_invoice_users()
    assert result[tier_key] == 0


def test_nudge_zero_invoice_budget_exceeded(db_session, monkeypatch):
    now = _now()
    _make_user(
        db_session,
        phone="+2348091110001",
        phone_verified=True,
        created_at=now - dt.timedelta(days=2),
    )
    _fake_wa(monkeypatch, can_send=False)
    result = mt.nudge_zero_invoice_users()
    assert result["skipped"] == 0


def test_nudge_zero_invoice_template_fallback(db_session, monkeypatch):
    now = _now()
    _make_user(
        db_session,
        phone="+2348091110002",
        phone_verified=True,
        created_at=now - dt.timedelta(days=2),
    )
    # send_text fails -> falls back to template
    _fake_wa(monkeypatch, send_text_ok=False, send_template_ok=True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_WIN_BACK", "winback_tpl")
    result = mt.nudge_zero_invoice_users()
    assert result["sent_1d"] == 0


def test_nudge_zero_invoice_skips_user_with_invoice(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        phone="+2348091110003",
        phone_verified=True,
        created_at=now - dt.timedelta(days=2),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust)
    _fake_wa(monkeypatch)
    result = mt.nudge_zero_invoice_users()
    # User has an invoice -> not a candidate
    assert result == {
        "success": True,
        "sent_1d": 0,
        "sent_3d": 0,
        "sent_7d": 0,
        "skipped": 0,
        "failed": 0,
    }
