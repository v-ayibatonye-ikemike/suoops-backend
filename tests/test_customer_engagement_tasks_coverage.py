"""Coverage tests for app/workers/tasks/customer_engagement_tasks.py.

Covers the dormant-customer nudge and post-payment referral Celery tasks plus
their helper functions. WhatsApp I/O is mocked; email is disabled in the module.
"""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models import models
from app.workers.tasks import customer_engagement_tasks as ce

_counter = itertools.count(1)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _make_user(db, **kw):
    n = next(_counter)
    defaults = dict(
        name=kw.pop("name", "Biz Owner"),
        email=kw.pop("email", f"owner{n}@example.com"),
        phone=kw.pop("phone", f"+23480{n:08d}"),
        business_name=kw.pop("business_name", "Acme Ltd"),
    )
    defaults.update(kw)
    u = models.User(**defaults)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_customer(db, **kw):
    n = next(_counter)
    defaults = dict(
        name=kw.pop("name", f"Cust {n}"),
        phone=kw.pop("phone", f"+23470{n:08d}"),
        whatsapp_opted_in=kw.pop("whatsapp_opted_in", True),
    )
    defaults.update(kw)
    c = models.Customer(**defaults)
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_invoice(db, issuer, customer_id, **kw):
    n = next(_counter)
    defaults = dict(
        invoice_id=f"INV-{n:06d}",
        issuer_id=issuer.id,
        customer_id=customer_id,
        amount=kw.pop("amount", Decimal("5000.00")),
        status=kw.pop("status", "paid"),
        invoice_type=kw.pop("invoice_type", "revenue"),
    )
    defaults.update(kw)
    inv = models.Invoice(**defaults)
    db.add(inv)
    db.commit()
    db.refresh(inv)
    return inv


class _FakeClient:
    def __init__(self, ok=True, raise_exc=False):
        self.ok = ok
        self.raise_exc = raise_exc

    def send_template(self, phone, name, lang, components):
        if self.raise_exc:
            raise RuntimeError("wa fail")
        return self.ok


def _patch_wa(monkeypatch, ok=True, can_send=True, raise_exc=False):
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: _FakeClient(ok, raise_exc))
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda *a, **k: can_send)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda *a, **k: 1)


# ─────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "phone,expected",
    [
        (None, False),
        ("", False),
        ("abc", False),
        ("+234801234", False),  # only 9 digits
        ("08012345678", True),
        ("+2348012345678", True),
    ],
)
def test_is_valid_phone(phone, expected):
    assert ce._is_valid_phone(phone) is expected


def test_already_sent_and_record_send(db_session):
    user = _make_user(db_session)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user, cust.id)
    assert ce._already_sent(db_session, inv.id, "post_payment_referral", "email") is False
    ce._record_send(db_session, inv.id, "post_payment_referral", "email", "a@b.com")
    db_session.commit()
    assert ce._already_sent(db_session, inv.id, "post_payment_referral", "email") is True


# ─────────────────────────────────────────────────────────────────────
# send_dormant_customer_nudges
# ─────────────────────────────────────────────────────────────────────


def test_dormant_empty(db_session):
    result = ce.send_dormant_customer_nudges()
    assert result == {
        "success": True,
        "email_sent": 0,
        "whatsapp_sent": 0,
        "skipped": 0,
        "failed": 0,
    }


def test_dormant_whatsapp_sent(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, whatsapp_opted_in=True)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        created_at=_now() - dt.timedelta(days=40),
    )
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DORMANT_CUSTOMER", "dormant_tpl")
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_dormant_customer_nudges()
    assert result["success"] is True
    assert result["whatsapp_sent"] == 1


def test_dormant_skips_already_sent(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, whatsapp_opted_in=True)
    inv = _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        created_at=_now() - dt.timedelta(days=40),
    )
    db_session.add(
        models.InvoiceReminderLog(
            invoice_id=inv.id,
            reminder_type="customer_dormant_21d",
            channel="whatsapp",
            recipient=cust.phone,
        )
    )
    db_session.commit()
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DORMANT_CUSTOMER", "dormant_tpl")
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_dormant_customer_nudges()
    assert result["skipped"] == 1
    assert result["whatsapp_sent"] == 0


def test_dormant_skips_no_contact(db_session, monkeypatch):
    user = _make_user(db_session)
    # No email, invalid phone -> skipped
    cust = _make_customer(db_session, email=None, phone="123", whatsapp_opted_in=True)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        created_at=_now() - dt.timedelta(days=40),
    )
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_dormant_customer_nudges()
    assert result["skipped"] == 1


def test_dormant_failed_when_not_opted_in(db_session, monkeypatch):
    user = _make_user(db_session)
    # Valid phone but not opted in, no email -> no channel -> failed
    cust = _make_customer(db_session, email=None, whatsapp_opted_in=False)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        created_at=_now() - dt.timedelta(days=40),
    )
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DORMANT_CUSTOMER", "dormant_tpl")
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_dormant_customer_nudges()
    assert result["failed"] == 1


def test_dormant_wa_exception(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, whatsapp_opted_in=True)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        created_at=_now() - dt.timedelta(days=40),
    )
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_DORMANT_CUSTOMER", "dormant_tpl")
    _patch_wa(monkeypatch, raise_exc=True)

    result = ce.send_dormant_customer_nudges()
    # send raised -> not delivered -> failed
    assert result["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# send_post_payment_referrals
# ─────────────────────────────────────────────────────────────────────


def test_referral_empty(db_session):
    result = ce.send_post_payment_referrals()
    assert result == {
        "success": True,
        "email_sent": 0,
        "whatsapp_sent": 0,
        "skipped": 0,
        "failed": 0,
    }


def test_referral_whatsapp_sent(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, whatsapp_opted_in=True)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        paid_at=_now() - dt.timedelta(hours=2),
    )
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_REFERRAL_ASK", "ref_tpl")
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_post_payment_referrals()
    assert result["success"] is True
    assert result["whatsapp_sent"] == 1


def test_referral_skips_already_sent(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, whatsapp_opted_in=True)
    inv = _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        paid_at=_now() - dt.timedelta(hours=2),
    )
    db_session.add(
        models.InvoiceReminderLog(
            invoice_id=inv.id,
            reminder_type="post_payment_referral",
            channel="whatsapp",
            recipient="+2348012345678",
        )
    )
    db_session.commit()
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_REFERRAL_ASK", "ref_tpl")
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_post_payment_referrals()
    assert result["skipped"] == 1
    assert result["whatsapp_sent"] == 0


def test_referral_skips_no_customer(db_session, monkeypatch):
    user = _make_user(db_session)
    # customer_id points to a non-existent customer -> joinedload gives None
    _make_invoice(
        db_session,
        user,
        999999,
        status="paid",
        paid_at=_now() - dt.timedelta(hours=2),
    )
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_post_payment_referrals()
    assert result["skipped"] == 1


def test_referral_skips_no_contact(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, email=None, phone="123", whatsapp_opted_in=True)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        paid_at=_now() - dt.timedelta(hours=2),
    )
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_post_payment_referrals()
    assert result["skipped"] == 1


def test_referral_failed_when_not_opted_in(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, email=None, whatsapp_opted_in=False)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        paid_at=_now() - dt.timedelta(hours=2),
    )
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_REFERRAL_ASK", "ref_tpl")
    _patch_wa(monkeypatch, ok=True)

    result = ce.send_post_payment_referrals()
    assert result["failed"] == 1


def test_referral_wa_exception(db_session, monkeypatch):
    user = _make_user(db_session)
    cust = _make_customer(db_session, whatsapp_opted_in=True)
    _make_invoice(
        db_session,
        user,
        cust.id,
        status="paid",
        paid_at=_now() - dt.timedelta(hours=2),
    )
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_REFERRAL_ASK", "ref_tpl")
    _patch_wa(monkeypatch, raise_exc=True)

    result = ce.send_post_payment_referrals()
    assert result["failed"] == 1
