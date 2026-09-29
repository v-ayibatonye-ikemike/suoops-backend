"""Coverage tests for app/workers/tasks/feedback_tasks.py."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models import models
from app.workers.tasks import feedback_tasks


# ─────────────────────────── fake redis ───────────────────────────
class _FakeRedis:
    def __init__(self):
        self.store: dict = {}

    def setex(self, key, ttl, val):
        self.store[key] = val

    def get(self, key):
        return self.store.get(key)

    def delete(self, key):
        self.store.pop(key, None)


@pytest.fixture
def fake_redis(monkeypatch):
    r = _FakeRedis()
    monkeypatch.setattr("app.db.redis_client.get_redis_client", lambda: r)
    return r


class _FrozenDateTime(datetime):
    """datetime whose now() returns a fixed *naive* value.

    On SQLite ``created_at`` round-trips as a naive datetime, but the task
    computes ``thirty_days_ago`` with ``datetime.now(tz=timezone.utc)`` (aware),
    so the comparison raises ``TypeError``. Returning a naive value keeps both
    sides naive — matching the behavior on a tz-consistent DB (Postgres).

    Anchored to the ACTUAL current time (naive UTC), NOT a hardcoded date, so the
    relative offsets in the test data (``days_ago=...``) always mean what they
    say regardless of the calendar date the suite runs on. A hardcoded date made
    this test flaky once real time drifted past it.
    """

    @classmethod
    def now(cls, tz=None):  # noqa: D401 - signature match
        return datetime.now(timezone.utc).replace(tzinfo=None)


@pytest.fixture
def frozen_now(monkeypatch):
    monkeypatch.setattr(feedback_tasks, "datetime", _FrozenDateTime)


# ─────────────────────────── seeding helpers ───────────────────────────
def _make_user(db, idx, *, phone="+2348012345678", email="set", name="Ada Lovelace"):
    user = models.User(
        phone=phone,
        name=name,
        email=(f"user{idx}@example.com" if email == "set" else email),
        business_name=f"Biz {idx}",
    )
    db.add(user)
    db.commit()
    return user


def _make_customer(db):
    cust = models.Customer(name="Cust", phone="+2340000009999", email="c@example.com")
    db.add(cust)
    db.commit()
    return cust


def _make_invoice(db, issuer_id, customer_id, *, days_ago=1, invoice_type="revenue", suffix=""):
    created = datetime.now(timezone.utc) - timedelta(days=days_ago)
    inv = models.Invoice(
        invoice_id=f"INV-{issuer_id}-{customer_id}-{suffix}",
        issuer_id=issuer_id,
        customer_id=customer_id,
        amount=Decimal("1000"),
        status="paid",
        invoice_type=invoice_type,
        created_at=created,
    )
    db.add(inv)
    db.commit()
    return inv


# ─────────────────────── redis helper functions ───────────────────────
def test_mark_and_check_feedback_pending(fake_redis):
    phone = "+2348012345678"
    assert feedback_tasks.is_feedback_pending(phone) is False
    feedback_tasks.mark_feedback_pending(phone)
    assert feedback_tasks.is_feedback_pending(phone) is True
    feedback_tasks.clear_feedback_pending(phone)
    assert feedback_tasks.is_feedback_pending(phone) is False


def test_mark_and_check_recently_asked(fake_redis):
    assert feedback_tasks._was_recently_asked(1) is False
    feedback_tasks._mark_asked(1)
    assert feedback_tasks._was_recently_asked(1) is True


def test_redis_helpers_swallow_exceptions(monkeypatch):
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.db.redis_client.get_redis_client", _boom)
    # None of these should raise even when redis is unavailable.
    feedback_tasks.mark_feedback_pending("+234800")
    assert feedback_tasks.is_feedback_pending("+234800") is False
    feedback_tasks.clear_feedback_pending("+234800")
    assert feedback_tasks._was_recently_asked(1) is False
    feedback_tasks._mark_asked(1)


@pytest.mark.parametrize(
    "phone,expected",
    [
        (None, False),
        ("", False),
        ("abc", False),
        ("+123", False),
        ("+2348012345678", True),
        ("08012345678", True),
    ],
)
def test_is_valid_phone(phone, expected):
    assert feedback_tasks._is_valid_phone(phone) is expected


# ─────────────────────── collect_user_feedback ───────────────────────
class _FakeClient:
    def __init__(self):
        self.templates: list = []
        self.template_ok = True

    def send_template(self, to, name, lang, components=None):
        self.templates.append((to, name))
        return self.template_ok


def test_collect_feedback_empty(fake_redis, db_session):
    result = feedback_tasks.collect_user_feedback.run()
    assert result["success"] is True
    assert result["email_sent"] == 0
    assert result["failed"] == 0


def test_collect_feedback_whatsapp_success(monkeypatch, fake_redis, frozen_now, db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    _make_invoice(db_session, user.id, cust.id, days_ago=1, suffix="a")

    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_FEEDBACK", "feedback_tpl")
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: None)
    client = _FakeClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    result = feedback_tasks.collect_user_feedback.run()
    assert result["success"] is True
    assert result.get("whatsapp_sent") == 1
    assert client.templates
    # Marked pending + asked in redis.
    assert feedback_tasks.is_feedback_pending(user.phone) is True
    assert feedback_tasks._was_recently_asked(user.id) is True


def test_collect_feedback_recently_asked_skipped(monkeypatch, fake_redis, db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    _make_invoice(db_session, user.id, cust.id, days_ago=1, suffix="a")
    feedback_tasks._mark_asked(user.id)  # already asked recently

    result = feedback_tasks.collect_user_feedback.run()
    assert result["skipped"] >= 1
    assert result.get("whatsapp_sent", 0) == 0


def test_collect_feedback_inactive_skipped(monkeypatch, fake_redis, frozen_now, db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    # Only invoice is older than 30 days -> not recently active.
    _make_invoice(db_session, user.id, cust.id, days_ago=45, suffix="old")

    result = feedback_tasks.collect_user_feedback.run()
    assert result["skipped"] >= 1
    assert result.get("whatsapp_sent", 0) == 0


def test_collect_feedback_no_template_failed(monkeypatch, fake_redis, frozen_now, db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    _make_invoice(db_session, user.id, cust.id, days_ago=1, suffix="a")

    # Template not configured -> WhatsApp not sent -> counted as failed.
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_FEEDBACK", None)
    result = feedback_tasks.collect_user_feedback.run()
    assert result["failed"] >= 1


def test_collect_feedback_excludes_users_with_testimonial(monkeypatch, fake_redis, frozen_now, db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    _make_invoice(db_session, user.id, cust.id, days_ago=1, suffix="a")
    db_session.add(models.Testimonial(user_id=user.id, text="Great!", rating=5))
    db_session.commit()

    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_FEEDBACK", "feedback_tpl")
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    client = _FakeClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    result = feedback_tasks.collect_user_feedback.run()
    # User already has a testimonial -> excluded from candidates entirely.
    assert result.get("whatsapp_sent", 0) == 0
    assert not client.templates


# ─────────────────────── _send_feedback_email ───────────────────────
class _FakeSMTP:
    sent: list = []

    def __init__(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def starttls(self):
        pass

    def login(self, u, p):
        pass

    def send_message(self, msg):
        _FakeSMTP.sent.append(msg)


def test_send_feedback_email_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", None, raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_SMTP_LOGIN", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_API_KEY", None, raising=False)
    ok = feedback_tasks._send_feedback_email("a@b.com", "Ada", 10, "tok")
    assert ok is False


def test_send_feedback_email_success(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(settings, "SMTP_USER", "user", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pass", raising=False)
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    ok = feedback_tasks._send_feedback_email("a@b.com", "Ada", 10, "tok")
    assert ok is True
    assert len(_FakeSMTP.sent) == 1


def test_send_feedback_email_raises(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", "user", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pass", raising=False)

    class _Boom(_FakeSMTP):
        def __enter__(self):
            raise RuntimeError("down")

    monkeypatch.setattr("smtplib.SMTP", _Boom)
    ok = feedback_tasks._send_feedback_email("a@b.com", "Ada", 10, "tok")
    assert ok is False
