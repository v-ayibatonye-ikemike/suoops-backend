"""Coverage tests for app/workers/tasks/welcome_tasks.py."""

from __future__ import annotations

from decimal import Decimal

from app.core.config import settings
from app.models import models
from app.workers.tasks import welcome_tasks


# ─────────────────────────── seeding helpers ───────────────────────────
def _make_user(db, idx: int, *, phone=None, email="set", name="Ada Lovelace"):
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


def _make_invoice(db, issuer_id, customer_id):
    inv = models.Invoice(
        invoice_id=f"INV-{issuer_id}-{customer_id}",
        issuer_id=issuer_id,
        customer_id=customer_id,
        amount=Decimal("5000"),
        status="pending",
    )
    db.add(inv)
    db.commit()
    return inv


class _FakeClient:
    def __init__(self):
        self.texts: list = []
        self.templates: list = []
        self.template_ok = True
        self.text_ok = True
        self.raise_on_template = False

    def send_text(self, to, msg):
        self.texts.append((to, msg))
        return self.text_ok

    def send_template(self, to, name, lang, components=None):
        if self.raise_on_template:
            raise RuntimeError("wa boom")
        self.templates.append((to, name))
        return self.template_ok


# ═══════════════════════ send_instant_welcome ═══════════════════════
def test_instant_welcome_user_not_found(db_session):
    result = welcome_tasks.send_instant_welcome(999999)
    assert result == {"email_sent": False, "whatsapp_sent": False}


def test_instant_welcome_already_sent(db_session):
    user = _make_user(db_session, 1)
    db_session.add(models.UserEmailLog(user_id=user.id, email_type="instant_welcome"))
    db_session.commit()

    result = welcome_tasks.send_instant_welcome(user.id)
    assert result == {"email_sent": False, "whatsapp_sent": False}


def test_instant_welcome_email_only(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone=None)
    sent_emails = []
    monkeypatch.setattr(
        welcome_tasks,
        "_send_email",
        lambda *args, **kwargs: sent_emails.append(args) or True,
    )

    result = welcome_tasks.send_instant_welcome(user.id)
    assert result["email_sent"] is True
    assert result["whatsapp_sent"] is False
    _, subject, html, plain = sent_emails[0]
    assert subject == "Welcome to SuoOps — Your Business, in One Place"
    for expected in (
        "I created SuoOps",
        "commerce operating system",
        "shareable storefront",
        "buyer protection",
        "courier delivery",
        "QR-verifiable invoices",
        "inventory",
        "expenses",
        "team access",
        "tax reports",
        "No plans or monthly fees",
        "https://suoops.com/dashboard/settings#profile",
        "https://suoops.com/dashboard/inventory",
        "https://suoops.com/dashboard/expenses",
        "https://suoops.com/dashboard/analytics",
        "https://suoops.com/dashboard/tax",
        "https://wa.me/2348106865807?text=Hi",
        "Founder & CEO, SuoOps",
    ):
        assert expected in plain
    assert "Founder &amp; CEO, SuoOps" in html
    assert "https://wa.me/2348106865807?text=Hi" in html
    assert "+234 818 376 3636" not in html
    # Log recorded so the daily activation sequence skips a duplicate welcome.
    logged = db_session.query(models.UserEmailLog).filter_by(user_id=user.id, email_type="instant_welcome").first()
    assert logged is not None


def test_instant_welcome_phone_still_uses_email_only(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr(welcome_tasks, "_send_email", lambda *a, **k: True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_ACTIVATION_WELCOME", "welcome_tpl")

    client = _FakeClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)
    # Avoid the real 3s + 2s sleeps in the onboarding branch.
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)
    monkeypatch.setattr("app.bot.onboarding_flow.start_onboarding", lambda phone, uid: None)
    monkeypatch.setattr("app.bot.onboarding_flow.send_onboarding_prompt", lambda c, p, n: None)
    # Don't hit the Celery broker for the scheduled follow-up.
    monkeypatch.setattr(welcome_tasks.send_activation_followup, "apply_async", lambda *a, **k: None)

    result = welcome_tasks.send_instant_welcome(user.id)
    assert result["email_sent"] is True
    assert result["whatsapp_sent"] is False
    assert client.templates == []
    assert client.texts == []


def test_instant_welcome_whatsapp_exception(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr(welcome_tasks, "_send_email", lambda *a, **k: True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_ACTIVATION_WELCOME", "welcome_tpl")

    client = _FakeClient()
    client.raise_on_template = True
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)
    monkeypatch.setattr(welcome_tasks.send_activation_followup, "apply_async", lambda *a, **k: None)

    result = welcome_tasks.send_instant_welcome(user.id)
    assert result["email_sent"] is True
    assert result["whatsapp_sent"] is False  # template raised -> caught


def test_broadcast_welcome_queues_all_reachable_accounts(monkeypatch, db_session):
    first = _make_user(db_session, 1)
    second = _make_user(db_session, 2, email=None, phone="+2348012345678")
    _make_user(db_session, 3, email=None, phone=None)
    queued = []
    monkeypatch.setattr(
        welcome_tasks.send_instant_welcome,
        "apply_async",
        lambda *, args, kwargs: queued.append((args, kwargs)),
    )

    result = welcome_tasks.broadcast_welcome()

    assert result == {"success": True, "queued": 2}
    assert queued == [
        ([first.id], {"broadcast": True}),
        ([second.id], {"broadcast": True}),
    ]


def test_broadcast_welcome_skips_signup_onboarding(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr(welcome_tasks, "_send_email", lambda *a, **k: True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_ACTIVATION_WELCOME", "welcome_tpl")
    client = _FakeClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)
    followups = []
    monkeypatch.setattr(
        welcome_tasks.send_activation_followup,
        "apply_async",
        lambda *a, **k: followups.append((a, k)),
    )

    result = welcome_tasks.send_instant_welcome(user.id, broadcast=True)

    assert result == {"email_sent": True, "whatsapp_sent": False}
    assert client.templates == []
    assert client.texts == []
    assert followups == []
    assert (
        db_session.query(models.UserEmailLog)
        .filter_by(user_id=user.id, email_type=welcome_tasks.WELCOME_BROADCAST_LOG_TYPE)
        .first()
    ) is not None


# ─────────────────────────── _send_email ───────────────────────────
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


def test_send_email_not_configured(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", None, raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_SMTP_LOGIN", None, raising=False)
    monkeypatch.setattr(settings, "BREVO_API_KEY", None, raising=False)
    ok = welcome_tasks._send_email("a@b.com", "Subj", "<p>hi</p>", "hi")
    assert ok is False


def test_send_email_success(monkeypatch):
    _FakeSMTP.sent = []
    monkeypatch.setattr(settings, "SMTP_USER", "user", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pass", raising=False)
    monkeypatch.setattr("smtplib.SMTP", _FakeSMTP)
    ok = welcome_tasks._send_email("a@b.com", "Subj", "<p>hi</p>", "hi")
    assert ok is True
    assert len(_FakeSMTP.sent) == 1


def test_send_email_smtp_raises(monkeypatch):
    monkeypatch.setattr(settings, "SMTP_USER", "user", raising=False)
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "pass", raising=False)

    class _Boom(_FakeSMTP):
        def __enter__(self):
            raise RuntimeError("down")

    monkeypatch.setattr("smtplib.SMTP", _Boom)
    ok = welcome_tasks._send_email("a@b.com", "Subj", "<p>hi</p>", "hi")
    assert ok is False


# ═══════════════════ send_activation_followup ═══════════════════
def test_followup_user_not_found(db_session):
    result = welcome_tasks.send_activation_followup(999999)
    assert result["reason"] == "in_app_only"


def test_followup_no_phone(db_session):
    user = _make_user(db_session, 1, phone=None)
    result = welcome_tasks.send_activation_followup(user.id)
    assert result["reason"] == "in_app_only"


def test_followup_already_activated(db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    cust = _make_customer(db_session)
    _make_invoice(db_session, user.id, cust.id)
    result = welcome_tasks.send_activation_followup(user.id)
    assert result["reason"] == "in_app_only"


def test_followup_already_sent(db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    db_session.add(models.UserEmailLog(user_id=user.id, email_type=welcome_tasks.FOLLOWUP_LOG_TYPE))
    db_session.commit()
    result = welcome_tasks.send_activation_followup(user.id)
    assert result["reason"] == "in_app_only"


def test_followup_budget_exhausted(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: False)
    result = welcome_tasks.send_activation_followup(user.id)
    assert result["reason"] == "in_app_only"


def test_followup_text_success(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: None)
    client = _FakeClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    result = welcome_tasks.send_activation_followup(user.id)
    assert result["sent"] is False
    logged = (
        db_session.query(models.UserEmailLog)
        .filter_by(user_id=user.id, email_type=welcome_tasks.FOLLOWUP_LOG_TYPE)
        .first()
    )
    assert logged is None


def test_followup_template_fallback(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: None)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_WIN_BACK", "win_back_tpl")
    client = _FakeClient()
    client.text_ok = False  # plain text fails -> template fallback used
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    result = welcome_tasks.send_activation_followup(user.id)
    assert result["sent"] is False
    assert client.templates == []


def test_followup_exception(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)

    def _boom():
        raise RuntimeError("client boom")

    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", _boom)
    result = welcome_tasks.send_activation_followup(user.id)
    assert result["sent"] is False
    assert result["reason"] == "in_app_only"


# ═══════════════════ send_first_paid_referral_nudge ═══════════════════
def test_referral_no_user_or_phone(db_session):
    result = welcome_tasks.send_first_paid_referral_nudge(999999)
    assert result["skipped_reason"] == "in_app_only"


def test_referral_already_sent(db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    db_session.add(models.UserEmailLog(user_id=user.id, email_type=welcome_tasks.FIRST_PAID_REFERRAL_LOG_TYPE))
    db_session.commit()
    result = welcome_tasks.send_first_paid_referral_nudge(user.id)
    assert result["skipped_reason"] == "in_app_only"


def test_referral_not_first_paid(db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    cust = _make_customer(db_session)
    for n in range(2):
        inv = models.Invoice(
            invoice_id=f"INV-P-{n}",
            issuer_id=user.id,
            customer_id=cust.id,
            amount=Decimal("1000"),
            status="paid",
        )
        db_session.add(inv)
    db_session.commit()

    result = welcome_tasks.send_first_paid_referral_nudge(user.id)
    assert result["skipped_reason"] == "in_app_only"


def test_referral_success(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    cust = _make_customer(db_session)
    inv = models.Invoice(
        invoice_id="INV-PAID-1",
        issuer_id=user.id,
        customer_id=cust.id,
        amount=Decimal("1000"),
        status="paid",
    )
    db_session.add(inv)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.analytics_service.calculate_professionalism_score",
        lambda db, uid: {"score": 60, "level": "Good", "tips": ["Add your logo", "Add bank details"]},
    )
    client = _FakeClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    result = welcome_tasks.send_first_paid_referral_nudge(user.id)
    assert result["sent"] is False
    assert client.texts == []


def test_referral_exception(monkeypatch, db_session):
    user = _make_user(db_session, 1, phone="+2348012345678")
    cust = _make_customer(db_session)
    inv = models.Invoice(
        invoice_id="INV-PAID-2",
        issuer_id=user.id,
        customer_id=cust.id,
        amount=Decimal("1000"),
        status="paid",
    )
    db_session.add(inv)
    db_session.commit()

    def _boom(db, uid):
        raise RuntimeError("score boom")

    monkeypatch.setattr("app.services.analytics_service.calculate_professionalism_score", _boom)
    result = welcome_tasks.send_first_paid_referral_nudge(user.id)
    assert result["sent"] is False
    assert result["skipped_reason"] == "in_app_only"
