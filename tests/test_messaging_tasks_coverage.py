"""Coverage-focused tests for app.workers.tasks.messaging_tasks.

Seeds the in-memory test DB (via the ``db_session`` fixture) and calls the
Celery task functions directly (Celery runs eagerly in ENV=test). All external
I/O — WhatsApp client, SMTP, redis, OCR, NLP adapters, requests — is patched so
nothing touches the network.
"""

from __future__ import annotations

import itertools
import smtplib
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest

from app.core.config import settings
from app.models import models
from app.models.models import SubscriptionPlan
from app.workers.tasks import messaging_tasks as mt

_counter = itertools.count(1)


# ── Seed helpers ─────────────────────────────────────────────────────


def _make_user(
    db,
    *,
    plan=SubscriptionPlan.FREE,
    phone="+2348030000000",
    email="owner@example.com",
    name="Ada Obi",
    pro_override=False,
):
    n = next(_counter)
    if phone is not None:
        phone = f"{phone[:-4]}{n:04d}"
    user = models.User(
        phone=phone,
        email=email,
        name=name,
        business_name="Ada Stores",
        plan=plan,
        pro_override=pro_override,
        bank_name="GTBank",
        account_number="0123456789",
        account_name="ADA STORES",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_customer(db, *, name="Chidi", phone="+2348090000000", email="cust@example.com"):
    n = next(_counter)
    cust = models.Customer(
        name=name,
        phone=f"{phone[:-4]}{n:04d}" if phone else None,
        email=email,
    )
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
    channel=None,
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
        channel=channel,
    )
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


@pytest.fixture
def wa(monkeypatch):
    """Patch WhatsApp client + budget + conversation window. Returns the mock client."""
    client = MagicMock()
    client.send_template.return_value = True
    client.send_text.return_value = True
    client.upload_media.return_value = "media-123"
    client.send_image.return_value = True

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client, raising=True)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: True, raising=True)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True, raising=True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: 1, raising=True)
    return client


@pytest.fixture
def smtp_ok(monkeypatch):
    """Configure SMTP creds and patch smtplib.SMTP so email helpers succeed."""
    monkeypatch.setattr(settings, "SMTP_USER", "smtp-user", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "smtp-pass", raising=False)
    fake_smtp = MagicMock()
    monkeypatch.setattr(smtplib, "SMTP", fake_smtp)
    return fake_smtp


@pytest.fixture
def no_smtp(monkeypatch):
    """Ensure SMTP is unconfigured so email helpers early-return False."""
    monkeypatch.setattr(settings, "SMTP_USER", None, raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_SMTP_LOGIN", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_API_KEY", None, raising=False)


@pytest.fixture
def base_url():
    """Inject settings.BASE_URL (a field the app reads but the Settings model
    does not define — see BUG note). Injected into __dict__ for the test only."""
    settings.__dict__["BASE_URL"] = "https://suoops.com"
    yield
    settings.__dict__.pop("BASE_URL", None)


@pytest.fixture
def paystack_key():
    """Inject settings.PAYSTACK_SECRET_KEY (the code reads this name, but the
    Settings field is actually PAYSTACK_SECRET — see BUG note)."""
    settings.__dict__["PAYSTACK_SECRET_KEY"] = "sk_test_dummy"
    yield
    settings.__dict__.pop("PAYSTACK_SECRET_KEY", None)


# ═══════════════════════════════════════════════════════════════════════
# Pure helper functions
# ═══════════════════════════════════════════════════════════════════════


def test_is_valid_phone():
    assert mt._is_valid_phone("+2348012345678") is True
    assert mt._is_valid_phone("08012345678") is True
    assert mt._is_valid_phone(None) is False
    assert mt._is_valid_phone("") is False
    assert mt._is_valid_phone("123") is False
    assert mt._is_valid_phone("not-a-number") is False


def _mk_inv_obj(amount, due_days_overdue):
    inv = MagicMock()
    inv.amount = amount
    inv.due_date = datetime.now(timezone.utc) - timedelta(days=due_days_overdue)
    return inv


def test_build_owner_escalation_message_all_tiers():
    today = date.today()
    tiers = {
        "owner_critical": [_mk_inv_obj(5000, 20)],
        "owner_urgent": [_mk_inv_obj(3000, 10)],
        "owner_action": [_mk_inv_obj(2000, 5)],
        "owner_light": [_mk_inv_obj(1000, 2)],
    }
    msg = mt._build_owner_escalation_message(tiers, today)
    assert msg is not None
    assert "CRITICAL" in msg
    assert "URGENT" in msg
    assert "Action Required" in msg
    assert "Heads Up" in msg
    assert "Overdue Invoice Report" in msg


def test_build_owner_escalation_message_empty():
    tiers = {"owner_critical": [], "owner_urgent": [], "owner_action": [], "owner_light": []}
    assert mt._build_owner_escalation_message(tiers, date.today()) is None


def test_classify_customer_tier():
    assert mt._classify_customer_tier(2) == "customer_pre_due"
    assert mt._classify_customer_tier(0) == "customer_due_today"
    assert mt._classify_customer_tier(-1) == "customer_overdue_1d"
    assert mt._classify_customer_tier(-7) == "customer_overdue_7d"
    assert mt._classify_customer_tier(-20) == "customer_overdue_14d"
    assert mt._classify_customer_tier(10) is None


def test_format_customer_reminder_all_tiers(base_url):
    inv = MagicMock()
    inv.invoice_id = "INV-1"
    inv.amount = 5000
    inv.due_date = datetime.now(timezone.utc)
    inv.customer = MagicMock()
    inv.customer.name = "Chidi Umeh"
    for tier in (
        "customer_pre_due",
        "customer_due_today",
        "customer_overdue_1d",
        "customer_overdue_7d",
        "customer_overdue_14d",
    ):
        out = mt._format_customer_reminder(inv, tier, "Ada Stores")
        assert "INV-1" in out
        assert "Ada Stores" in out


def test_email_subject_for_tier():
    inv = MagicMock()
    inv.invoice_id = "INV-9"
    for tier in (
        "customer_pre_due",
        "customer_due_today",
        "customer_overdue_1d",
        "customer_overdue_7d",
        "customer_overdue_14d",
    ):
        subj = mt._email_subject_for_tier(inv, tier, "Ada Stores")
        assert "INV-9" in subj


def test_name_greeting():
    c1 = MagicMock()
    c1.name = "Chidi Umeh"
    assert mt._name_greeting(c1) == " Chidi"
    c2 = MagicMock()
    c2.name = None
    assert mt._name_greeting(c2) == ""
    c3 = MagicMock()
    c3.name = "   "
    assert mt._name_greeting(c3) == ""


def test_format_daily_summary_populated():
    msg = mt._format_daily_summary(10000, 3000, 5000, 2)
    assert "Cash In" in msg
    assert "Expenses" in msg
    assert "Net" in msg
    assert "Outstanding" in msg
    assert "Overdue" in msg


def test_format_daily_summary_empty():
    msg = mt._format_daily_summary(0, 0, 0, 0)
    assert "All clear today" in msg


# ═══════════════════════════════════════════════════════════════════════
# Email helper functions (smtplib)
# ═══════════════════════════════════════════════════════════════════════


def _sample_tiers():
    return {
        "owner_critical": [_mk_inv_obj(5000, 20)],
        "owner_urgent": [],
        "owner_action": [_mk_inv_obj(2000, 5)],
        "owner_light": [],
    }


def test_send_owner_overdue_email_success(smtp_ok):
    ok = mt._send_owner_overdue_email("to@example.com", "Ada Obi", _sample_tiers(), date.today())
    assert ok is True
    assert smtp_ok.called


def test_send_owner_overdue_email_not_configured(no_smtp):
    ok = mt._send_owner_overdue_email("to@example.com", "Ada", _sample_tiers(), date.today())
    assert ok is False


def test_send_owner_overdue_email_smtp_raises(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", "u", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "p", raising=False)
    boom = MagicMock(side_effect=OSError("smtp down"))
    monkeypatch.setattr(smtplib, "SMTP", boom)
    ok = mt._send_owner_overdue_email("to@example.com", "Ada", _sample_tiers(), date.today())
    assert ok is False


def test_send_mark_paid_email_success(smtp_ok, db_session):
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(
        db_session,
        user,
        cust,
        amount=8000,
        due_date=datetime.now(timezone.utc) - timedelta(days=6),
    )
    ok = mt._send_mark_paid_email("to@example.com", "Ada Obi", 2, 16000.0, 6, [inv], date.today())
    assert ok is True


def test_send_mark_paid_email_not_configured(no_smtp, db_session):
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user, cust, amount=8000)
    ok = mt._send_mark_paid_email("to@example.com", None, 2, 16000.0, 0, [inv], date.today())
    assert ok is False


def test_send_daily_summary_email_success(smtp_ok):
    ok = mt._send_daily_summary_email(
        to_email="to@example.com",
        name="Ada Obi",
        revenue=10000,
        expenses=2000,
        net=8000,
        outstanding=5000,
        overdue_count=1,
    )
    assert ok is True


def test_send_daily_summary_email_empty_body(smtp_ok):
    ok = mt._send_daily_summary_email(
        to_email="to@example.com",
        name=None,
        revenue=0,
        expenses=0,
        net=0,
        outstanding=0,
        overdue_count=0,
    )
    assert ok is True


def test_send_daily_summary_email_not_configured(no_smtp):
    ok = mt._send_daily_summary_email(
        to_email="to@example.com",
        name="Ada",
        revenue=100,
        expenses=0,
        net=100,
        outstanding=0,
        overdue_count=0,
    )
    assert ok is False


# ═══════════════════════════════════════════════════════════════════════
# send_overdue_reminders
# ═══════════════════════════════════════════════════════════════════════


def test_overdue_reminders_no_invoices(db_session, wa):
    result = mt.send_overdue_reminders()
    assert result["success"] is True
    assert result["total_overdue"] == 0


def test_overdue_reminders_whatsapp_template(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_OVERDUE_REPORT", "overdue_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=15000,
        due_date=datetime.now(timezone.utc) - timedelta(days=20),
    )
    result = mt.send_overdue_reminders()
    assert result["sent"] == 1
    wa.send_template.assert_called()
    # Reminder logs written
    assert db_session.query(models.InvoiceReminderLog).count() >= 1


def test_overdue_reminders_email_fallback(db_session, wa, smtp_ok, monkeypatch):
    # No WhatsApp template, window closed -> email fallback.
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_OVERDUE_REPORT", None, raising=False)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False, raising=True)
    user = _make_user(db_session, phone=None, email="ada@example.com")
    cust = _make_customer(db_session)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=9000,
        due_date=datetime.now(timezone.utc) - timedelta(days=10),
    )
    result = mt.send_overdue_reminders()
    assert result["email_sent"] == 1


def test_overdue_reminders_skip_already_sent(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_OVERDUE_REPORT", "overdue_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(
        db_session,
        user,
        cust,
        amount=5000,
        due_date=datetime.now(timezone.utc) - timedelta(days=5),  # owner_action
    )
    db_session.add(
        models.InvoiceReminderLog(
            invoice_id=inv.id,
            reminder_type="owner_action",
            channel="whatsapp",
            recipient=user.phone,
        )
    )
    db_session.commit()
    result = mt.send_overdue_reminders()
    assert result["skipped"] >= 1


def test_overdue_reminders_skip_no_channel(db_session, wa):
    # User with no phone and no email is skipped entirely.
    user = _make_user(db_session, phone=None, email=None)
    cust = _make_customer(db_session)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=5000,
        due_date=datetime.now(timezone.utc) - timedelta(days=3),
    )
    result = mt.send_overdue_reminders()
    assert result["sent"] == 0
    assert result["email_sent"] == 0


# ═══════════════════════════════════════════════════════════════════════
# send_customer_payment_reminders
# ═══════════════════════════════════════════════════════════════════════


def test_customer_reminders_no_candidates(db_session, wa):
    result = mt.send_customer_payment_reminders()
    assert result["success"] is True
    assert result["whatsapp_sent"] == 0


def test_customer_reminders_whatsapp_template(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_REMINDER", "pay_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=12000,
        due_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    result = mt.send_customer_payment_reminders()
    assert result["whatsapp_sent"] == 1
    assert db_session.query(models.InvoiceReminderLog).count() >= 1


def test_customer_reminders_email_only_customer(db_session, wa, base_url, monkeypatch):
    # Customer with email but no phone -> email reminder.
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_REMINDER", "pay_tpl", raising=False)

    async def fake_send_email(self, to, subject, body):
        return True

    monkeypatch.setattr(
        "app.services.notification.service.NotificationService.send_email",
        fake_send_email,
        raising=True,
    )
    user = _make_user(db_session)
    cust = _make_customer(db_session, phone=None, email="cust@example.com")
    _make_invoice(
        db_session,
        user,
        cust,
        amount=7000,
        due_date=datetime.now(timezone.utc) - timedelta(days=8),
    )
    result = mt.send_customer_payment_reminders()
    assert result["email_sent"] == 1


def test_customer_reminders_skip_already_sent(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_REMINDER", "pay_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(
        db_session,
        user,
        cust,
        amount=7000,
        due_date=datetime.now(timezone.utc) - timedelta(days=1),
    )
    db_session.add(
        models.InvoiceReminderLog(
            invoice_id=inv.id,
            reminder_type="customer_overdue_1d",
            channel="whatsapp",
            recipient=cust.phone,
        )
    )
    db_session.commit()
    result = mt.send_customer_payment_reminders()
    assert result["skipped"] >= 1


def test_customer_reminders_14d_notifies_owner(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_REMINDER", "pay_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=30000,
        due_date=datetime.now(timezone.utc) - timedelta(days=20),
    )
    result = mt.send_customer_payment_reminders()
    assert result["success"] is True


def test_customer_reminders_skip_abandoned_storefront(db_session, wa, monkeypatch):
    """Abandoned storefront carts (channel=='storefront', pending) are online-pay
    drop-offs, not receivables. They must NOT trigger customer reminders or owner
    escalations — matching the cash dashboard, which excludes them. Regression for
    the '0 overdue on dashboard but flood of escalation emails' bug.
    """
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_REMINDER", "pay_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    _make_invoice(
        db_session,
        user,
        cust,
        amount=30000,
        due_date=datetime.now(timezone.utc) - timedelta(days=20),
        channel="storefront",
    )
    result = mt.send_customer_payment_reminders()
    assert result["success"] is True
    assert result["whatsapp_sent"] == 0
    assert result["email_sent"] == 0
    # No reminder rows and no owner escalation should have been logged/sent.
    assert db_session.query(models.InvoiceReminderLog).count() == 0


# ═══════════════════════════════════════════════════════════════════════
# send_mark_paid_nudges
# ═══════════════════════════════════════════════════════════════════════


@pytest.fixture
def redis_mock(monkeypatch):
    r = MagicMock()
    r.get.return_value = None
    r.set.return_value = True
    monkeypatch.setattr("app.db.redis_client.get_redis_client", lambda: r, raising=True)
    return r


def test_mark_paid_nudges_none(db_session, wa, redis_mock):
    result = mt.send_mark_paid_nudges()
    assert result["success"] is True
    assert result["sent"] == 0


def test_mark_paid_nudges_whatsapp_template(db_session, wa, redis_mock, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_MARK_PAID_NUDGE", "nudge_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    _make_invoice(db_session, user, cust, amount=5000, created_at=old)
    _make_invoice(db_session, user, cust, amount=6000, created_at=old)
    result = mt.send_mark_paid_nudges()
    assert result["sent"] == 1
    wa.send_template.assert_called()
    redis_mock.set.assert_called()


def test_mark_paid_nudges_email_fallback(db_session, wa, redis_mock, smtp_ok, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_MARK_PAID_NUDGE", None, raising=False)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False, raising=True)
    user = _make_user(db_session, phone=None, email="ada@example.com")
    cust = _make_customer(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=12)
    _make_invoice(db_session, user, cust, amount=5000, created_at=old)
    _make_invoice(db_session, user, cust, amount=6000, created_at=old)
    result = mt.send_mark_paid_nudges()
    assert result["sent"] == 1


def test_mark_paid_nudges_cooldown_skip(db_session, wa, monkeypatch):
    r = MagicMock()
    r.get.return_value = "1"  # cooldown active
    monkeypatch.setattr("app.db.redis_client.get_redis_client", lambda: r, raising=True)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    _make_invoice(db_session, user, cust, amount=5000, created_at=old)
    _make_invoice(db_session, user, cust, amount=6000, created_at=old)
    result = mt.send_mark_paid_nudges()
    assert result["skipped_cooldown"] == 1


def test_mark_paid_nudges_skip_abandoned_storefront(db_session, wa, redis_mock, monkeypatch):
    """Abandoned storefront carts (pending, channel=='storefront') must NOT be
    counted as 'pending invoices to mark paid'. Regression for the '14 Invoices
    Still Pending' email sent to a user whose dashboard shows 0 pending.
    """
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_MARK_PAID_NUDGE", "nudge_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    old = datetime.now(timezone.utc) - timedelta(days=10)
    _make_invoice(db_session, user, cust, amount=5000, created_at=old, channel="storefront")
    _make_invoice(db_session, user, cust, amount=6000, created_at=old, channel="storefront")
    result = mt.send_mark_paid_nudges()
    assert result["success"] is True
    assert result["sent"] == 0
    wa.send_template.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# sync_provider_status
# ═══════════════════════════════════════════════════════════════════════


def test_sync_provider_status_unsupported():
    result = mt.sync_provider_status("stripe", "ref-1")
    assert result["success"] is False
    assert "Unsupported" in result["error"]


def test_sync_provider_status_no_key():
    # Force a falsy Paystack key to reach the "not configured" branch.
    original = settings.__dict__.get("PAYSTACK_SECRET")
    settings.__dict__["PAYSTACK_SECRET"] = None
    try:
        result = mt.sync_provider_status("paystack", "ref-1")
    finally:
        settings.__dict__["PAYSTACK_SECRET"] = original
    assert result["success"] is False


def test_sync_provider_status_success_paid(db_session, paystack_key, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user, cust, amount=5000, status="pending")

    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": {"status": "success", "gateway_response": "ok"}}
    monkeypatch.setattr("requests.get", lambda *a, **k: resp, raising=True)

    result = mt.sync_provider_status("paystack", inv.invoice_id)
    assert result["success"] is True
    assert result["status"] == "paid"
    assert result["changed"] is True


def test_sync_provider_status_invoice_not_found(db_session, paystack_key, monkeypatch):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": {"status": "success"}}
    monkeypatch.setattr("requests.get", lambda *a, **k: resp, raising=True)
    result = mt.sync_provider_status("paystack", "NONEXISTENT")
    assert result["success"] is False
    assert result["error"] == "Invoice not found"


def test_sync_provider_status_unknown_status(db_session, paystack_key, monkeypatch):
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": {"status": "weird"}}
    monkeypatch.setattr("requests.get", lambda *a, **k: resp, raising=True)
    result = mt.sync_provider_status("paystack", "ref-x")
    assert result["success"] is False
    assert "Unknown provider status" in result["error"]


def test_sync_provider_status_already_same(db_session, paystack_key, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user, cust, amount=5000, status="paid")
    resp = MagicMock()
    resp.raise_for_status.return_value = None
    resp.json.return_value = {"data": {"status": "success"}}
    monkeypatch.setattr("requests.get", lambda *a, **k: resp, raising=True)
    result = mt.sync_provider_status("paystack", inv.invoice_id)
    assert result["changed"] is False


# ═══════════════════════════════════════════════════════════════════════
# ocr_parse_image
# ═══════════════════════════════════════════════════════════════════════


def test_ocr_parse_image_success(monkeypatch):
    import base64

    class FakeOCR:
        async def parse_receipt(self, raw, context):
            return {"success": True, "merchant": "Shop"}

    monkeypatch.setattr("app.services.ocr_service.OCRService", FakeOCR, raising=True)
    b64 = base64.b64encode(b"imgbytes").decode()
    result = mt.ocr_parse_image(b64, "expense")
    assert result["success"] is True


# ═══════════════════════════════════════════════════════════════════════
# send_daily_summaries
# ═══════════════════════════════════════════════════════════════════════


def test_daily_summaries_no_users(db_session, wa):
    result = mt.send_daily_summaries()
    assert result["success"] is True
    assert result["sent"] == 0


def test_daily_summaries_whatsapp_template(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DAILY_SUMMARY", "daily_tpl", raising=False)
    # window closed so the post-send cash image early-returns.
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False, raising=True)
    user = _make_user(db_session, plan=SubscriptionPlan.PRO)
    cust = _make_customer(db_session)
    now = datetime.now(timezone.utc)
    _make_invoice(db_session, user, cust, amount=15000, status="paid", paid_at=now)
    result = mt.send_daily_summaries()
    assert result["sent"] == 1
    wa.send_template.assert_called()


def test_daily_summaries_email_fallback(db_session, wa, smtp_ok, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DAILY_SUMMARY", None, raising=False)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False, raising=True)
    user = _make_user(db_session, plan=SubscriptionPlan.PRO, phone=None, email="ada@example.com")
    cust = _make_customer(db_session)
    now = datetime.now(timezone.utc)
    _make_invoice(db_session, user, cust, amount=20000, status="paid", paid_at=now)
    result = mt.send_daily_summaries()
    assert result["email_sent"] == 1


def test_daily_summaries_pro_override(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DAILY_SUMMARY", "daily_tpl", raising=False)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False, raising=True)
    user = _make_user(db_session, plan=SubscriptionPlan.FREE, pro_override=True)
    cust = _make_customer(db_session)
    _make_invoice(db_session, user, cust, amount=8000, status="pending")
    result = mt.send_daily_summaries()
    assert result["sent"] == 1


def test_daily_summaries_plain_text_and_cash_image(db_session, wa, monkeypatch):
    # No template -> plain text within window, then cash-snapshot image follow-up.
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DAILY_SUMMARY", None, raising=False)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: True, raising=True)
    monkeypatch.setattr(
        "app.services.analytics_service.calculate_cash_position",
        lambda db, uid: {"cash_collected_today": 15000, "total_outstanding": 0},
        raising=True,
    )
    monkeypatch.setattr(
        "app.services.cash_dashboard_image.build_cash_snapshot_png",
        lambda cash, biz, cur: b"pngbytes",
        raising=True,
    )
    monkeypatch.setattr("app.utils.currency_fmt.get_user_currency", lambda db, uid: "NGN", raising=True)
    user = _make_user(db_session, plan=SubscriptionPlan.PRO)
    cust = _make_customer(db_session)
    now = datetime.now(timezone.utc)
    _make_invoice(db_session, user, cust, amount=15000, status="paid", paid_at=now)
    result = mt.send_daily_summaries()
    assert result["sent"] == 1
    wa.send_text.assert_called()
    wa.send_image.assert_called()


# ═══════════════════════════════════════════════════════════════════════
# _send_daily_cash_image
# ═══════════════════════════════════════════════════════════════════════


def test_send_daily_cash_image(db_session, monkeypatch):
    user = _make_user(db_session)
    client = MagicMock()
    client.upload_media.return_value = "mid-1"
    monkeypatch.setattr(
        "app.services.analytics_service.calculate_cash_position",
        lambda db, uid: {"cash_collected_today": 5000, "total_outstanding": 0},
        raising=True,
    )
    monkeypatch.setattr(
        "app.services.cash_dashboard_image.build_cash_snapshot_png",
        lambda cash, biz, cur: b"pngbytes",
        raising=True,
    )
    monkeypatch.setattr("app.utils.currency_fmt.get_user_currency", lambda db, uid: "NGN", raising=True)
    mt._send_daily_cash_image(db_session, client, user, lambda phone: True)
    client.upload_media.assert_called_once()
    client.send_image.assert_called_once()


def test_send_daily_cash_image_window_closed(db_session):
    user = _make_user(db_session)
    client = MagicMock()
    mt._send_daily_cash_image(db_session, client, user, lambda phone: False)
    client.upload_media.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════
# process_whatsapp_inbound
# ═══════════════════════════════════════════════════════════════════════


def test_process_whatsapp_inbound(db_session, monkeypatch):
    class FakeHandler:
        def __init__(self, **kwargs):
            pass

        async def handle_incoming(self, payload):
            return None

    monkeypatch.setattr("app.bot.nlp_service.NLPService", MagicMock(), raising=True)
    monkeypatch.setattr("app.bot.whatsapp_adapter.WhatsAppHandler", FakeHandler, raising=True)
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: MagicMock(), raising=True)

    result = mt.process_whatsapp_inbound({"entry": []})
    assert result is None


# ═══════════════════════════════════════════════════════════════════════
# _notify_owner_escalation
# ═══════════════════════════════════════════════════════════════════════


def test_notify_owner_escalation_whatsapp(db_session, wa, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user, cust, amount=30000)
    mt._notify_owner_escalation(inv, user, cust, "Ada Stores")
    wa.send_text.assert_called()


def test_notify_owner_escalation_email_fallback(db_session, wa, smtp_ok, monkeypatch):
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False, raising=True)
    wa.send_text.return_value = False
    user = _make_user(db_session, email="ada@example.com")
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user, cust, amount=30000)
    mt._notify_owner_escalation(inv, user, cust, "Ada Stores")
    assert smtp_ok.called


# ═══════════════════════════════════════════════════════════════════════
# _send_customer_whatsapp_reminder / _send_customer_email_reminder
# ═══════════════════════════════════════════════════════════════════════


def test_send_customer_whatsapp_reminder_template(db_session, wa, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_PAYMENT_REMINDER", "pay_tpl", raising=False)
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(
        db_session,
        user,
        cust,
        amount=12000,
        due_date=datetime.now(timezone.utc) - timedelta(days=2),
    )
    ok = mt._send_customer_whatsapp_reminder(inv, cust, user, "customer_overdue_1d", "Ada Stores")
    assert ok is True
    wa.send_template.assert_called()


def test_send_customer_email_reminder(db_session, base_url, monkeypatch):
    async def fake_send_email(self, to, subject, body):
        return True

    monkeypatch.setattr(
        "app.services.notification.service.NotificationService.send_email",
        fake_send_email,
        raising=True,
    )
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(
        db_session,
        user,
        cust,
        amount=12000,
        due_date=datetime.now(timezone.utc) - timedelta(days=2),
    )
    ok = mt._send_customer_email_reminder(inv, cust, user, "customer_overdue_1d", "Ada Stores")
    assert ok is True
