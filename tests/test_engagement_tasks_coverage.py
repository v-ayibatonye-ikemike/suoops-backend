"""Coverage tests for app/workers/tasks/engagement_tasks.py.

Exercises lifecycle engagement email logic: the main scheduled task plus the
individual helper functions (_send_activation, _send_monetization, _send_tip,
_send_zero_invoice_nudge, _send_phone_nudge, _send_wa_template, _process_user).

All email/WhatsApp I/O is mocked. Real Jinja templates on disk are rendered.
"""

from __future__ import annotations

import datetime as dt
import itertools
from decimal import Decimal

import pytest

from app.core.config import settings
from app.models import models
from app.models.models import SubscriptionPlan
from app.workers.tasks import engagement_tasks as et

_counter = itertools.count(1)


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _make_user(db, **kw):
    n = next(_counter)
    defaults = dict(
        name=kw.pop("name", "Jane Doe"),
        email=kw.pop("email", f"user{n}@example.com"),
        phone=kw.pop("phone", f"+23480{n:08d}"),
    )
    defaults.update(kw)
    u = models.User(**defaults)
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def _make_customer(db, **kw):
    n = next(_counter)
    c = models.Customer(name=kw.pop("name", f"Cust {n}"), phone=kw.pop("phone", f"+23470{n:08d}"))
    db.add(c)
    db.commit()
    db.refresh(c)
    return c


def _make_invoice(db, issuer, customer, **kw):
    n = next(_counter)
    defaults = dict(
        invoice_id=f"INV-{n:06d}",
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


class _FakeClient:
    def __init__(self, ok=True, raise_exc=False):
        self.ok = ok
        self.raise_exc = raise_exc

    def send_template(self, phone, name, lang, components):
        if self.raise_exc:
            raise RuntimeError("wa fail")
        return self.ok

    def send_text(self, to, body):
        if self.raise_exc:
            raise RuntimeError("wa fail")
        return self.ok


def _patch_wa(monkeypatch, ok=True, can_send=True, raise_exc=False):
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: _FakeClient(ok, raise_exc))
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda *a, **k: can_send)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda *a, **k: 1)


def _new_stats():
    return {
        "activation_sent": 0,
        "monetization_sent": 0,
        "tips_sent": 0,
        "phone_nudge_sent": 0,
        "whatsapp_sent": 0,
        "skipped": 0,
        "failed": 0,
    }


# ─────────────────────────────────────────────────────────────────────
# Small helpers
# ─────────────────────────────────────────────────────────────────────


def test_get_user_name_with_name():
    u = models.User(name="John Smith", email="a@b.com")
    assert et._get_user_name(u) == "John"


def test_get_user_name_without_name():
    u = models.User(name="", email="a@b.com")
    assert et._get_user_name(u) == "there"


def test_was_sent_and_record_sent(db_session):
    u = _make_user(db_session)
    assert et._was_sent(db_session, u.id, "type_a") is False
    et._record_sent(db_session, u.id, "type_a")
    db_session.commit()
    assert et._was_sent(db_session, u.id, "type_a") is True


# ─────────────────────────────────────────────────────────────────────
# _send_wa_template
# ─────────────────────────────────────────────────────────────────────


def test_send_wa_template_no_phone(db_session):
    assert et._send_wa_template(None, "tpl", ["x"], "wa_x", db_session, 1) is False


def test_send_wa_template_no_template(db_session):
    assert et._send_wa_template("+234800", None, ["x"], "wa_x", db_session, 1) is False


def test_send_wa_template_already_sent(db_session):
    u = _make_user(db_session)
    et._record_sent(db_session, u.id, "wa_x")
    db_session.commit()
    assert et._send_wa_template("+234800", "tpl", ["x"], "wa_x", db_session, u.id) is False


def test_send_wa_template_budget_exceeded(db_session, monkeypatch):
    u = _make_user(db_session)
    _patch_wa(monkeypatch, can_send=False)
    assert et._send_wa_template("+234800", "tpl", ["x"], "wa_x", db_session, u.id) is False


def test_send_wa_template_success(db_session, monkeypatch):
    u = _make_user(db_session)
    _patch_wa(monkeypatch, ok=True)
    ok = et._send_wa_template("+234800", "tpl", ["Jane"], "wa_x", db_session, u.id)
    db_session.commit()
    assert ok is True
    assert et._was_sent(db_session, u.id, "wa_x") is True


def test_send_wa_template_no_params(db_session, monkeypatch):
    u = _make_user(db_session)
    _patch_wa(monkeypatch, ok=True)
    ok = et._send_wa_template("+234800", "tpl", [], "wa_y", db_session, u.id)
    assert ok is True


def test_send_wa_template_exception(db_session, monkeypatch):
    u = _make_user(db_session)
    _patch_wa(monkeypatch, raise_exc=True)
    assert et._send_wa_template("+234800", "tpl", ["x"], "wa_x", db_session, u.id) is False


# ─────────────────────────────────────────────────────────────────────
# _send_activation
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day", [0, 1, 3])
def test_send_activation_days(db_session, monkeypatch, day):
    u = _make_user(db_session, phone=None)  # no phone -> skip WA branch
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_activation(db_session, u, "Jane", day, stats)
    db_session.commit()
    assert stats["activation_sent"] == 1


def test_send_activation_unknown_day(db_session):
    u = _make_user(db_session)
    stats = _new_stats()
    et._send_activation(db_session, u, "Jane", 5, stats)
    assert stats["skipped"] == 1


def test_send_activation_already_sent(db_session, monkeypatch):
    u = _make_user(db_session, phone=None)
    et._record_sent(db_session, u.id, et.EMAIL_WELCOME_FIRST_INVOICE)
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_activation(db_session, u, "Jane", 0, stats)
    assert stats["skipped"] == 1


def test_send_activation_email_fails(db_session, monkeypatch):
    u = _make_user(db_session, phone=None)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: False)
    stats = _new_stats()
    et._send_activation(db_session, u, "Jane", 0, stats)
    assert stats["failed"] == 1


def test_send_activation_with_phone_does_not_acquire_whatsapp(db_session, monkeypatch):
    u = _make_user(db_session, phone="+2348090001111")
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_ACTIVATION_WELCOME", "act_welcome")

    def fail():
        raise AssertionError("activation email must not acquire a WhatsApp client")

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", fail)
    stats = _new_stats()
    et._send_activation(db_session, u, "Jane", 0, stats)
    db_session.commit()
    assert stats["activation_sent"] == 1
    assert stats["whatsapp_sent"] == 0


# ─────────────────────────────────────────────────────────────────────
# _send_monetization
# ─────────────────────────────────────────────────────────────────────


def test_send_monetization_wallet_empty(db_session, monkeypatch):
    u = _make_user(db_session, phone=None, wallet_balance_kobo=0)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    sent = et._send_monetization(db_session, u, "Jane", 5, stats)
    db_session.commit()
    assert sent is True
    assert stats["monetization_sent"] == 1


def test_send_monetization_wallet_low(db_session, monkeypatch):
    u = _make_user(db_session, phone=None, wallet_balance_kobo=3000)  # ₦30
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    sent = et._send_monetization(db_session, u, "Jane", 5, stats)
    assert sent is True
    assert stats["monetization_sent"] == 1


def test_send_monetization_three_invoices(db_session, monkeypatch):
    u = _make_user(db_session, phone=None, wallet_balance_kobo=100000)  # ₦1000
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    sent = et._send_monetization(db_session, u, "Jane", 3, stats)
    assert sent is True
    assert stats["monetization_sent"] == 1


def test_send_monetization_no_threshold(db_session, monkeypatch):
    u = _make_user(db_session, phone=None, wallet_balance_kobo=100000)  # ₦1000
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    sent = et._send_monetization(db_session, u, "Jane", 1, stats)
    assert sent is False


def test_send_monetization_email_fails(db_session, monkeypatch):
    u = _make_user(db_session, phone=None, wallet_balance_kobo=0)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: False)
    stats = _new_stats()
    sent = et._send_monetization(db_session, u, "Jane", 5, stats)
    assert sent is True
    assert stats["failed"] == 1


def test_send_monetization_no_duplicate_whatsapp_limit(db_session, monkeypatch):
    """Owners get the nudge by email only — no duplicate paid WhatsApp send."""
    u = _make_user(db_session, phone="+2348090002222", wallet_balance_kobo=0)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_INVOICE_PACK_PROMO", "pack_promo")
    _patch_wa(monkeypatch, ok=True)
    stats = _new_stats()
    et._send_monetization(db_session, u, "Jane", 5, stats)
    db_session.commit()
    assert stats["monetization_sent"] == 1
    assert stats["whatsapp_sent"] == 0


def test_send_monetization_no_duplicate_whatsapp_low(db_session, monkeypatch):
    """Owners get the low-balance nudge by email only — no duplicate WhatsApp."""
    u = _make_user(db_session, phone="+2348090003333", wallet_balance_kobo=3000)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_LOW_BALANCE", "low_bal")
    _patch_wa(monkeypatch, ok=True)
    stats = _new_stats()
    et._send_monetization(db_session, u, "Jane", 5, stats)
    db_session.commit()
    assert stats["monetization_sent"] == 1
    assert stats["whatsapp_sent"] == 0


# ─────────────────────────────────────────────────────────────────────
# _send_tip
# ─────────────────────────────────────────────────────────────────────


def test_send_tip_sends_first(db_session, monkeypatch):
    u = _make_user(db_session)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_tip(db_session, u, "Jane", stats)
    db_session.commit()
    assert stats["tips_sent"] == 1
    assert et._was_sent(db_session, u.id, f"{et.EMAIL_TIP_PREFIX}0") is True


def test_send_tip_recent_skips(db_session, monkeypatch):
    u = _make_user(db_session)
    # Recent tip within 4 days
    db_session.add(
        models.UserEmailLog(
            user_id=u.id,
            email_type=f"{et.EMAIL_TIP_PREFIX}0",
            sent_at=_now() - dt.timedelta(days=1),
        )
    )
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_tip(db_session, u, "Jane", stats)
    assert stats["skipped"] == 1


def test_send_tip_all_sent_skips(db_session, monkeypatch):
    u = _make_user(db_session)
    # Mark all tips sent, last one old enough (>4 days)
    for i in range(len(et.TIPS)):
        db_session.add(
            models.UserEmailLog(
                user_id=u.id,
                email_type=f"{et.EMAIL_TIP_PREFIX}{i}",
                sent_at=_now() - dt.timedelta(days=10),
            )
        )
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_tip(db_session, u, "Jane", stats)
    assert stats["skipped"] == 1


def test_send_tip_email_fails(db_session, monkeypatch):
    u = _make_user(db_session)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: False)
    stats = _new_stats()
    et._send_tip(db_session, u, "Jane", stats)
    assert stats["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# _send_phone_nudge
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day", [5, 10])
def test_send_phone_nudge_sends(db_session, monkeypatch, day):
    u = _make_user(db_session)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_phone_nudge(db_session, u, "Jane", day, stats)
    db_session.commit()
    assert stats["phone_nudge_sent"] == 1


def test_send_phone_nudge_unknown_day(db_session):
    u = _make_user(db_session)
    stats = _new_stats()
    et._send_phone_nudge(db_session, u, "Jane", 3, stats)
    assert stats == _new_stats()


def test_send_phone_nudge_no_email(db_session):
    u = _make_user(db_session, email=None)
    stats = _new_stats()
    et._send_phone_nudge(db_session, u, "Jane", 5, stats)
    assert stats == _new_stats()


def test_send_phone_nudge_already_sent(db_session, monkeypatch):
    u = _make_user(db_session)
    et._record_sent(db_session, u.id, et.EMAIL_PHONE_NUDGE_DAY5)
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_phone_nudge(db_session, u, "Jane", 5, stats)
    assert stats == _new_stats()


def test_send_phone_nudge_email_fails(db_session, monkeypatch):
    u = _make_user(db_session)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: False)
    stats = _new_stats()
    et._send_phone_nudge(db_session, u, "Jane", 5, stats)
    assert stats["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# _send_zero_invoice_nudge
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("day", [7, 14])
def test_send_zero_invoice_nudge_email(db_session, monkeypatch, day):
    u = _make_user(db_session, phone=None)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_zero_invoice_nudge(db_session, u, "Jane", day, stats)
    db_session.commit()
    assert stats["activation_sent"] == 1


def test_send_zero_invoice_nudge_unknown_day(db_session):
    u = _make_user(db_session)
    stats = _new_stats()
    et._send_zero_invoice_nudge(db_session, u, "Jane", 3, stats)
    assert stats["skipped"] == 1


def test_send_zero_invoice_nudge_already_sent(db_session, monkeypatch):
    u = _make_user(db_session, phone=None)
    et._record_sent(db_session, u.id, et.EMAIL_NUDGE_DAY7)
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._send_zero_invoice_nudge(db_session, u, "Jane", 7, stats)
    assert stats["skipped"] == 1


def test_send_zero_invoice_nudge_has_no_whatsapp_window_fallback(db_session, monkeypatch):
    u = _make_user(db_session, email=None, phone="+2348090004444")
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: True)
    _patch_wa(monkeypatch, ok=True)
    stats = _new_stats()
    et._send_zero_invoice_nudge(db_session, u, "Jane", 7, stats)
    db_session.commit()
    assert stats["activation_sent"] == 0
    assert stats["whatsapp_sent"] == 0
    assert stats["failed"] == 1


def test_send_zero_invoice_nudge_has_no_whatsapp_template_fallback(db_session, monkeypatch):
    u = _make_user(db_session, email=None, phone="+2348090005555")
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: False)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_WIN_BACK", "winback")
    _patch_wa(monkeypatch, ok=True)
    stats = _new_stats()
    et._send_zero_invoice_nudge(db_session, u, "Jane", 7, stats)
    db_session.commit()
    assert stats["activation_sent"] == 0
    assert stats["whatsapp_sent"] == 0
    assert stats["failed"] == 1


def test_send_zero_invoice_nudge_all_fail(db_session, monkeypatch):
    # No email, no phone -> failed
    u = _make_user(db_session, email=None, phone=None)
    stats = _new_stats()
    et._send_zero_invoice_nudge(db_session, u, "Jane", 7, stats)
    assert stats["failed"] == 1


# ─────────────────────────────────────────────────────────────────────
# _process_user branch dispatch
# ─────────────────────────────────────────────────────────────────────


def test_process_user_activation_day0(db_session, monkeypatch):
    now = _now()
    u = _make_user(db_session, phone=None, created_at=now)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats["activation_sent"] == 1


def test_process_user_day1_skipped(db_session, monkeypatch):
    now = _now()
    u = _make_user(db_session, phone=None, created_at=now - dt.timedelta(days=1))
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    assert stats["skipped"] == 1


def test_process_user_day0_instant_welcome_skipped(db_session, monkeypatch):
    now = _now()
    u = _make_user(db_session, phone=None, created_at=now)
    et._record_sent(db_session, u.id, "instant_welcome")
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    assert stats["skipped"] == 1


def test_process_user_day7_nudge(db_session, monkeypatch):
    now = _now()
    u = _make_user(db_session, phone=None, created_at=now - dt.timedelta(days=7))
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats["activation_sent"] == 1


def test_process_user_day14_nudge(db_session, monkeypatch):
    now = _now()
    u = _make_user(db_session, phone=None, created_at=now - dt.timedelta(days=14))
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats["activation_sent"] == 1


def test_process_user_first_invoice_uses_email_not_whatsapp(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        phone="+2348090006666",
        plan=SubscriptionPlan.PRO,
        created_at=now - dt.timedelta(days=30),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust, created_at=now - dt.timedelta(days=1))
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_FIRST_INVOICE", "first_inv")
    _patch_wa(monkeypatch, ok=True)
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats["whatsapp_sent"] == 0
    assert stats.get("emails_sent", 0) == 1


def test_process_user_first_invoice_email_fallback(db_session, monkeypatch):
    # PRO user, one invoice, WA unavailable (no template) -> email fallback
    now = _now()
    u = _make_user(
        db_session,
        phone=None,
        plan=SubscriptionPlan.PRO,
        created_at=now - dt.timedelta(days=30),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust, created_at=now - dt.timedelta(days=1))
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats.get("emails_sent", 0) == 1


def test_process_user_monetization(db_session, monkeypatch):
    # FREE user with invoices + empty wallet -> monetization send + return
    now = _now()
    u = _make_user(
        db_session,
        phone=None,
        plan=SubscriptionPlan.FREE,
        wallet_balance_kobo=0,
        created_at=now - dt.timedelta(days=30),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust)
    # mark first-invoice WA already handled so we go straight to monetization
    et._record_sent(db_session, u.id, "wa_first_invoice")
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats["monetization_sent"] == 1


def test_process_user_tips_disabled_skip(db_session, monkeypatch):
    # FREE user with invoices, high wallet, already monetized -> tips-disabled skip
    now = _now()
    u = _make_user(
        db_session,
        phone=None,
        plan=SubscriptionPlan.FREE,
        wallet_balance_kobo=500000,
        created_at=now - dt.timedelta(days=40),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust)
    et._record_sent(db_session, u.id, "wa_first_invoice")
    et._record_sent(db_session, u.id, et.EMAIL_3_INVOICES_SENT)
    db_session.commit()
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    assert stats["skipped"] >= 1


def test_process_user_winback_does_not_use_whatsapp(db_session, monkeypatch):
    now = _now()
    u = _make_user(
        db_session,
        phone="+2348090008888",
        plan=SubscriptionPlan.PRO,
        created_at=now - dt.timedelta(days=60),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust, created_at=now - dt.timedelta(days=10))
    et._record_sent(db_session, u.id, "wa_first_invoice")
    db_session.commit()
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_WIN_BACK", "winback")
    _patch_wa(monkeypatch, ok=True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    db_session.commit()
    assert stats["whatsapp_sent"] == 0
    assert stats["skipped"] >= 1


def test_process_user_recent_pro_no_winback(db_session, monkeypatch):
    # PRO user, recent invoice (<7 days) -> falls to final skip
    now = _now()
    u = _make_user(
        db_session,
        phone="+2348090009999",
        plan=SubscriptionPlan.PRO,
        created_at=now - dt.timedelta(days=60),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u, cust, created_at=now - dt.timedelta(days=1))
    et._record_sent(db_session, u.id, "wa_first_invoice")
    db_session.commit()
    _patch_wa(monkeypatch, ok=True)
    stats = _new_stats()
    et._process_user(db_session, u, now, stats)
    assert stats["skipped"] >= 1


# ─────────────────────────────────────────────────────────────────────
# send_engagement_emails (main scheduled task)
# ─────────────────────────────────────────────────────────────────────


def test_send_engagement_emails_empty(db_session, monkeypatch):
    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    result = et.send_engagement_emails()
    assert result["success"] is True


def test_send_engagement_emails_mixed(db_session, monkeypatch):
    now = _now()
    # Day 0 activation user
    _make_user(db_session, phone=None, created_at=now)
    # FREE user with invoice + empty wallet (monetization)
    u2 = _make_user(
        db_session,
        phone=None,
        plan=SubscriptionPlan.FREE,
        wallet_balance_kobo=0,
        created_at=now - dt.timedelta(days=40),
    )
    cust = _make_customer(db_session)
    _make_invoice(db_session, u2, cust)
    et._record_sent(db_session, u2.id, "wa_first_invoice")
    db_session.commit()

    monkeypatch.setattr(et, "_send_smtp_email", lambda *a, **k: True)
    _patch_wa(monkeypatch, ok=True)

    result = et.send_engagement_emails()
    assert result["success"] is True
    assert result["activation_sent"] >= 1
    assert result["monetization_sent"] >= 1


def test_send_engagement_emails_handles_user_error(db_session, monkeypatch):
    now = _now()
    _make_user(db_session, phone=None, created_at=now)

    def boom(db, user, now_, stats, invoice_count_map=None):
        raise RuntimeError("processing error")

    monkeypatch.setattr(et, "_process_user", boom)
    result = et.send_engagement_emails()
    assert result["success"] is True
    assert result["failed"] == 1
