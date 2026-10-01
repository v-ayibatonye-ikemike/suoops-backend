"""Coverage tests for app/workers/tasks/tax_tasks.py.

NOTE (real bug, unfixed per task rules): ``tax_tasks`` references several
module-level names that are never imported/defined in the module:
``MONTH_NAMES``, ``os``, ``Template``, ``settings``, ``smtplib``,
``MIMEMultipart`` and ``MIMEText``. As shipped, ``generate_previous_month_reports``
raises ``NameError`` and both notification helpers raise ``NameError`` at runtime.

These tests document the current (buggy) behavior for the unpatched path and, to
exercise the remaining logic for coverage, inject the missing names as module
attributes at test time (this does NOT modify any file under app/).
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models import models
from app.models.alert_models import AlertEvent  # noqa: F401 - ensure table registered in metadata
from app.models.tax_models import FiscalInvoice
from app.workers.tasks import tax_tasks

MONTHS = [
    "",
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]


# ─────────────────────────── seeding helpers ───────────────────────────
def _make_user(db, idx: int, *, phone: str | None = None, email: str | None = None):
    user = models.User(
        phone=phone if phone is not None else f"+23480000000{idx:02d}",
        name=f"User {idx}",
        email=email if email is not None else f"user{idx}@example.com",
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


def _make_invoice(db, issuer_id: int, customer_id: int):
    inv = models.Invoice(
        invoice_id=f"INV-{issuer_id}-{customer_id}",
        issuer_id=issuer_id,
        customer_id=customer_id,
        amount=Decimal("1000"),
        status="paid",
    )
    db.add(inv)
    db.commit()
    return inv


# ─────────────────────── fakes for service layer ───────────────────────
class _FakeReport:
    def __init__(self, pdf_url=None):
        self.pdf_url = pdf_url


class _FakeReporting:
    raise_generate = False

    def __init__(self, db):
        self.db = db

    def generate_monthly_report(self, user_id, year, month, basis="paid", force_regenerate=False):
        if type(self).raise_generate:
            raise RuntimeError("boom-generate")
        return _FakeReport(pdf_url=None)

    def attach_report_pdf(self, report, pdf_url):
        report.pdf_url = pdf_url
        return report


class _FakeReportingWithPdf(_FakeReporting):
    def generate_monthly_report(self, user_id, year, month, basis="paid", force_regenerate=False):
        return _FakeReport(pdf_url="http://pdf/existing.pdf")


class _FakeTaxProfile:
    def __init__(self, db):
        self.db = db
        self.alerts: list[dict] = []
        _FakeTaxProfile.last = self

    def record_alert(self, category, message, severity="error"):
        self.alerts.append({"category": category, "message": message, "severity": severity})


class _FakePDF:
    def __init__(self, client):
        self.client = client

    def generate_monthly_tax_report_pdf(self, report, basis="paid"):
        return "http://pdf/generated.pdf"


def _inject_service_fakes(monkeypatch, reporting_cls=_FakeReporting):
    monkeypatch.setattr(tax_tasks, "TaxReportingService", reporting_cls)
    monkeypatch.setattr(tax_tasks, "TaxProfileService", _FakeTaxProfile)
    monkeypatch.setattr(tax_tasks, "PDFService", _FakePDF)
    monkeypatch.setattr(tax_tasks, "MONTH_NAMES", MONTHS, raising=False)


# ═══════════════════ generate_previous_month_reports ═══════════════════
def test_month_names_defined_and_indexable():
    """Regression: MONTH_NAMES must exist and be 1-indexed — the task builds a
    period label via MONTH_NAMES[prev_month] for months 1-12."""
    assert tax_tasks.MONTH_NAMES[1] == "January"
    assert tax_tasks.MONTH_NAMES[12] == "December"
    assert len(tax_tasks.MONTH_NAMES) == 13


def test_generate_reports_whatsapp_notify_path(monkeypatch, db_session):
    _inject_service_fakes(monkeypatch)
    # 6 users so the `total % 5 == 0` gc/progress branch runs.
    for i in range(1, 7):
        _make_user(db_session, i)

    monkeypatch.setattr(tax_tasks, "_notify_tax_report_whatsapp", lambda *a, **k: True)
    called = {"email": 0}
    monkeypatch.setattr(
        tax_tasks,
        "_send_tax_report_email",
        lambda **k: called.__setitem__("email", called["email"] + 1) or True,
    )

    result = tax_tasks.generate_previous_month_reports.run(basis="paid")
    assert result is None
    # WhatsApp succeeded for everyone, so email helper never invoked.
    assert called["email"] == 0
    assert _FakeTaxProfile.last.alerts == []


def test_generate_reports_email_fallback_path(monkeypatch, db_session):
    _inject_service_fakes(monkeypatch, reporting_cls=_FakeReportingWithPdf)
    _make_user(db_session, 1)

    monkeypatch.setattr(tax_tasks, "_notify_tax_report_whatsapp", lambda *a, **k: False)
    sent = {"count": 0}
    monkeypatch.setattr(
        tax_tasks,
        "_send_tax_report_email",
        lambda **k: sent.__setitem__("count", sent["count"] + 1) or True,
    )

    result = tax_tasks.generate_previous_month_reports.run(basis="all")
    assert result is None
    assert sent["count"] == 1


def test_generate_reports_notifies_once_per_period(monkeypatch, db_session):
    """Idempotency: re-running the monthly task in the same period must NOT
    re-send the 'Tax Report Is Ready' email (regression for repeated sends)."""
    _inject_service_fakes(monkeypatch, reporting_cls=_FakeReportingWithPdf)
    _make_user(db_session, 1)

    monkeypatch.setattr(tax_tasks, "_notify_tax_report_whatsapp", lambda *a, **k: False)
    sent = {"count": 0}
    monkeypatch.setattr(
        tax_tasks,
        "_send_tax_report_email",
        lambda **k: sent.__setitem__("count", sent["count"] + 1) or True,
    )

    tax_tasks.generate_previous_month_reports.run(basis="paid")
    assert sent["count"] == 1
    # Second run (e.g. manual trigger / beat re-fire) — no duplicate email.
    tax_tasks.generate_previous_month_reports.run(basis="paid")
    assert sent["count"] == 1


def test_generate_reports_failure_records_alerts(monkeypatch, db_session):
    _inject_service_fakes(monkeypatch, reporting_cls=_FakeReporting)
    _FakeReporting.raise_generate = True
    try:
        _make_user(db_session, 1)
        _make_user(db_session, 2)
        result = tax_tasks.generate_previous_month_reports.run(basis="paid")
    finally:
        _FakeReporting.raise_generate = False

    assert result is None
    alerts = _FakeTaxProfile.last.alerts
    categories = {a["category"] for a in alerts}
    assert "tax.report" in categories  # per-user failure alert
    assert "tax.report.summary" in categories  # summary alert


# ═══════════════════════════ transmit_invoice ═══════════════════════════
class _FakeTransmitter:
    result = {"status": "validated", "transaction_id": "TX-1"}
    raise_error = False

    def __init__(self, *a, **k):
        pass

    async def transmit(self, inv, fi):
        if type(self).raise_error:
            raise RuntimeError("transmit-fail")
        return type(self).result


def _seed_fiscal(db, fiscal_code="FC-1", invoice_id=None):
    fi = FiscalInvoice(
        invoice_id=invoice_id,
        fiscal_code=fiscal_code,
        fiscal_signature="sig",
        qr_code_data="qr",
        subtotal=Decimal("1000"),
        vat_amount=Decimal("75"),
        total_amount=Decimal("1075"),
    )
    db.add(fi)
    db.commit()
    return fi


def test_transmit_invoice_success(monkeypatch, db_session):
    monkeypatch.setattr("app.services.fiscalization_service.FiscalTransmitter", _FakeTransmitter)
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user.id, cust.id)
    _seed_fiscal(db_session, "FC-OK", invoice_id=inv.id)

    tax_tasks.transmit_invoice.run("FC-OK")

    refreshed = db_session.query(FiscalInvoice).filter_by(fiscal_code="FC-OK").one()
    assert refreshed.firs_validation_status == "validated"
    assert refreshed.firs_transaction_id == "TX-1"
    assert refreshed.transmitted_at is not None


def test_transmit_invoice_missing_fiscal(db_session):
    # No fiscal invoice with this code -> returns early, no raise.
    assert tax_tasks.transmit_invoice.run("DOES-NOT-EXIST") is None


def test_transmit_invoice_missing_invoice(db_session):
    # Fiscal invoice points at a non-existent invoice -> early return.
    _seed_fiscal(db_session, "FC-NOINV", invoice_id=999999)
    assert tax_tasks.transmit_invoice.run("FC-NOINV") is None


def test_transmit_invoice_failure_records_alert(monkeypatch, db_session):
    _FakeTransmitter.raise_error = True
    try:
        monkeypatch.setattr("app.services.fiscalization_service.FiscalTransmitter", _FakeTransmitter)
        user = _make_user(db_session, 1)
        cust = _make_customer(db_session)
        inv = _make_invoice(db_session, user.id, cust.id)
        _seed_fiscal(db_session, "FC-FAIL", invoice_id=inv.id)
        tax_tasks.transmit_invoice.run("FC-FAIL")
    finally:
        _FakeTransmitter.raise_error = False

    events = db_session.query(AlertEvent).filter_by(category="fiscal.transmit").all()
    assert len(events) == 1


# ═══════════════ _update_fiscal_invoice / failure helper ═══════════════
def test_update_fiscal_invoice_validated(db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user.id, cust.id)
    fi = _seed_fiscal(db_session, "FC-U1", invoice_id=inv.id)

    tax_tasks._update_fiscal_invoice(db_session, fi, {"status": "validated", "transaction_id": "T9"})
    assert fi.firs_validation_status == "validated"
    assert fi.firs_transaction_id == "T9"
    assert fi.transmitted_at is not None
    assert fi.firs_response["transmission"]["status"] == "validated"


def test_update_fiscal_invoice_non_validated_keeps_status(db_session):
    user = _make_user(db_session, 1)
    cust = _make_customer(db_session)
    inv = _make_invoice(db_session, user.id, cust.id)
    fi = _seed_fiscal(db_session, "FC-U2", invoice_id=inv.id)

    tax_tasks._update_fiscal_invoice(db_session, fi, {"status": "rejected"})
    assert fi.firs_validation_status == "rejected"
    assert fi.transmitted_at is None


def test_record_transmission_failure_adds_alert(db_session):
    tax_tasks._record_transmission_failure(db_session, "FC-X", RuntimeError("nope"))
    events = db_session.query(AlertEvent).filter_by(category="fiscal.transmit").all()
    assert len(events) == 1
    assert "FC-X" in events[0].message


# ─────────────────────────── _is_valid_phone ───────────────────────────
@pytest.mark.parametrize(
    "phone,expected",
    [
        (None, False),
        ("", False),
        ("abc", False),
        ("+123", False),  # too short
        ("+2348012345678", True),
        ("08012345678", True),
    ],
)
def test_is_valid_phone(phone, expected):
    assert tax_tasks._is_valid_phone(phone) is expected


# ─────────────────── _notify_tax_report_whatsapp ────────────────────
class _FakeWaClient:
    def __init__(self):
        self.templates: list = []
        self.texts: list = []
        self.template_ok = True
        self.text_ok = True

    def send_template(self, to, name, lang, components=None):
        self.templates.append((to, name))
        return self.template_ok

    def send_text(self, to, msg):
        self.texts.append((to, msg))
        return self.text_ok


def test_notify_whatsapp_invalid_phone_returns_false():
    user = SimpleNamespace(phone=None, name="A", id=1)
    assert tax_tasks._notify_tax_report_whatsapp(user, "July 2025", None) is False


def test_notify_whatsapp_budget_exhausted(monkeypatch):
    monkeypatch.setattr(tax_tasks, "settings", SimpleNamespace(), raising=False)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: False)
    user = SimpleNamespace(phone="+2348012345678", name="Ada", id=1)
    assert tax_tasks._notify_tax_report_whatsapp(user, "July 2025", None) is False


def test_notify_whatsapp_template_success(monkeypatch):
    fake_settings = SimpleNamespace(
        WHATSAPP_TEMPLATE_TAX_REPORT_READY="tax_ready",
        WHATSAPP_TEMPLATE_LANGUAGE="en",
    )
    monkeypatch.setattr(tax_tasks, "settings", fake_settings, raising=False)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    recorded = {"n": 0}
    monkeypatch.setattr(
        "app.utils.whatsapp_budget.record_whatsapp_send",
        lambda priority=False: recorded.__setitem__("n", recorded["n"] + 1),
    )
    client = _FakeWaClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    user = SimpleNamespace(phone="+2348012345678", name="Ada Lovelace", id=1)
    assert tax_tasks._notify_tax_report_whatsapp(user, "July 2025", "http://x") is True
    assert client.templates and recorded["n"] == 1


def test_notify_whatsapp_plaintext_fallback(monkeypatch):
    fake_settings = SimpleNamespace(
        WHATSAPP_TEMPLATE_TAX_REPORT_READY=None,  # no template -> plain-text path
        WHATSAPP_TEMPLATE_LANGUAGE="en",
    )
    monkeypatch.setattr(tax_tasks, "settings", fake_settings, raising=False)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda priority=False: None)
    monkeypatch.setattr("app.bot.conversation_window.is_window_open", lambda phone: True)
    client = _FakeWaClient()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: client)

    user = SimpleNamespace(phone="+2348012345678", name="Ada", id=1)
    assert tax_tasks._notify_tax_report_whatsapp(user, "July 2025", None) is True
    assert client.texts


def test_notify_whatsapp_returns_false_when_over_budget(monkeypatch):
    # Over the WhatsApp budget -> early False (no template/window attempt).
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda priority=False: False)
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: _FakeWaClient())
    user = SimpleNamespace(phone="+2348012345678", name="Ada", id=1)
    assert tax_tasks._notify_tax_report_whatsapp(user, "July 2025", None) is False


def test_send_tax_email_not_configured(monkeypatch):
    monkeypatch.setattr("app.utils.smtp.send_email_with_fallback", lambda *args, **kwargs: False)
    ok = tax_tasks._send_tax_report_email(to_email="a@b.com", name="Ada Lovelace", period="July 2025", pdf_url=None)
    assert ok is False


def test_send_tax_email_success(monkeypatch):
    sent = []
    monkeypatch.setattr(
        "app.utils.smtp.send_email_with_fallback",
        lambda *args, **kwargs: sent.append((args, kwargs)) or True,
    )

    ok = tax_tasks._send_tax_report_email(
        to_email="a@b.com",
        name="Ada Lovelace",
        period="July 2025",
        pdf_url="http://pdf/x.pdf",
    )
    assert ok is True
    assert len(sent) == 1


def test_tax_email_links_dashboard_and_omits_presigned_url(monkeypatch):
    """End-to-end guard for the two reported bugs:
    1. NO expiring presigned S3 URL is embedded (was 'Request has expired').
    2. The CTA points to the REAL /dashboard/tax route (was 404 /dashboard/tax-reports).
    Passing a presigned pdf_url must NOT leak it into the email.
    """
    sent = []
    monkeypatch.setattr(
        "app.utils.smtp.send_email_with_fallback",
        lambda *args, **kwargs: sent.append((args, kwargs)) or True,
    )

    presigned = (
        "https://suoops-s3-bucket.s3.amazonaws.com/tax-reports/1/2026-07.pdf"
        "?X-Amz-Signature=EXPIRESOON&X-Amz-Expires=3600"
    )
    ok = tax_tasks._send_tax_report_email(
        to_email="a@b.com",
        name="Ada",
        period="July 2026",
        pdf_url=presigned,
    )
    assert ok is True

    args, _ = sent[-1]
    blob = "\n".join((args[2], args[3]))

    # 1. No expiring presigned link anywhere in the email.
    assert presigned not in blob
    assert "X-Amz-Signature" not in blob
    assert "Download PDF" not in blob
    # 2. CTA uses the real dashboard route, never the 404 one.
    assert "suoops.com/dashboard/tax" in blob
    assert "/dashboard/tax-reports" not in blob


def test_send_tax_email_smtp_raises(monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.utils.smtp.send_email_with_fallback", fail)
    ok = tax_tasks._send_tax_report_email(to_email="a@b.com", name=None, period="July 2025", pdf_url=None)
    assert ok is False
