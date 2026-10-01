"""
Tax and Fiscalization Tasks.

Celery tasks for tax report generation and invoice fiscalization.
"""

from __future__ import annotations

import gc
import logging
import os

try:
    import resource
except Exception:
    resource = None  # type: ignore

from celery import Task
from jinja2 import Template

from app.core.config import settings
from app.db.session import session_scope
from app.models.models import Invoice, User, UserEmailLog
from app.models.tax_models import FiscalInvoice
from app.services.pdf_service import PDFService
from app.services.tax_reporting_service import TaxReportingService
from app.services.tax_service import TaxProfileService
from app.storage.s3_client import s3_client
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Month names indexed 1-12 (index 0 is an unused placeholder) for report labels.
MONTH_NAMES = [
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


def _rss_mb() -> float:
    """Get current RSS memory in MB."""
    if resource is None:
        return -1.0
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024.0


@celery_app.task(
    bind=True,
    name="tax.generate_previous_month_reports",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def generate_previous_month_reports(self: Task, basis: str = "paid") -> None:
    """Generate monthly tax reports (PDF) for all users for the previous month.

    Memory-conscious implementation using streaming and periodic GC.
    """
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    prev_month = now.month - 1 or 12
    year = now.year - 1 if prev_month == 12 and now.month == 1 else now.year

    with session_scope() as db:
        tax_service = TaxProfileService(db)
        reporting = TaxReportingService(db)
        pdf_service = PDFService(s3_client)

        # Fetch users with phone/email so we can notify after generation
        users = db.query(User).yield_per(100)
        total = 0
        failures = 0
        notified_wa = 0
        notified_email = 0
        month_label = MONTH_NAMES[prev_month]
        period_label = f"{month_label} {year}"

        for user in users:
            user_id = user.id
            total += 1
            try:
                report = reporting.generate_monthly_report(
                    user_id, year, prev_month, basis=basis, force_regenerate=False
                )
                if not report.pdf_url:
                    pdf_url = pdf_service.generate_monthly_tax_report_pdf(report, basis=basis)
                    reporting.attach_report_pdf(report, pdf_url)

                if total % 5 == 0:
                    gc.collect()
                    rss = _rss_mb()
                    if rss > 0:
                        logger.info(
                            "[tax.generate_previous_month_reports] progress=%s users rss=%.1fMB",
                            total,
                            rss,
                        )

                logger.info(
                    "Generated monthly tax report for user=%s period=%s-%02d",
                    user_id,
                    year,
                    prev_month,
                )

                # ── Notify user that report is ready (ONCE per period) ─────
                # Idempotency guard: this task can run more than once for the
                # same month (manual admin trigger, Celery retry, or beat
                # re-firing after a worker restart). Without this check it
                # re-sent the "Tax Report Is Ready" email on every run. Scope the
                # dedup key to the period so next month still notifies.
                notify_key = f"tax_report_ready:{year}-{prev_month:02d}"
                already_notified = (
                    db.query(UserEmailLog.id)
                    .filter(
                        UserEmailLog.user_id == user_id,
                        UserEmailLog.email_type == notify_key,
                    )
                    .first()
                )
                if already_notified:
                    db.expire_all()
                    continue

                wa_ok = _notify_tax_report_whatsapp(
                    user,
                    period_label,
                    report.pdf_url,
                )
                notified = False
                if wa_ok:
                    notified_wa += 1
                    notified = True
                elif user.email:
                    email_ok = _send_tax_report_email(
                        to_email=user.email,
                        name=user.name or user.business_name,
                        period=period_label,
                        pdf_url=report.pdf_url,
                    )
                    if email_ok:
                        notified_email += 1
                        notified = True

                # Record the send so re-runs don't re-notify for this period.
                if notified:
                    db.add(UserEmailLog(user_id=user_id, email_type=notify_key))
                    db.commit()

            except Exception as e:
                failures += 1
                logger.exception("Failed generating report for user %s: %s", user_id, e)
                tax_service.record_alert(
                    category="tax.report",
                    message=f"Monthly report generation failed for user {user_id}: {e}",
                    severity="error",
                )
            db.expire_all()

        if failures:
            tax_service.record_alert(
                category="tax.report.summary",
                message=f"Monthly report generation completed with {failures} failures",
                severity="warning" if failures < total else "error",
            )

        rss_final = _rss_mb()
        if rss_final > 0:
            logger.info(
                "[tax.generate_previous_month_reports] completed users=%s "
                "rss_final=%.1fMB failures=%s wa_notified=%s email_notified=%s",
                total,
                rss_final,
                failures,
                notified_wa,
                notified_email,
            )
    gc.collect()


@celery_app.task(name="fiscalization.transmit_invoice", bind=True)
def transmit_invoice(self: Task, fiscal_code: str) -> None:
    """Background transmission of a fiscalized invoice to external gateway."""
    import asyncio

    from app.services.fiscalization_service import FiscalTransmitter

    with session_scope() as db:
        fi: FiscalInvoice | None = db.query(FiscalInvoice).filter(FiscalInvoice.fiscal_code == fiscal_code).first()
        if not fi:
            logger.warning("Transmit skip: fiscal invoice not found | fiscal_code=%s", fiscal_code)
            return

        inv: Invoice | None = db.query(Invoice).filter(Invoice.id == fi.invoice_id).first()
        if not inv:
            logger.warning(
                "Transmit skip: invoice missing | fiscal_code=%s invoice_id=%s",
                fiscal_code,
                fi.invoice_id,
            )
            return

        transmitter = FiscalTransmitter()
        try:
            tx_result = asyncio.run(transmitter.transmit(inv, fi))
            _update_fiscal_invoice(db, fi, tx_result)
            logger.info(
                "Fiscal invoice transmitted | fiscal_code=%s status=%s",
                fiscal_code,
                fi.firs_validation_status,
            )
        except Exception as e:
            logger.exception(
                "Fiscal invoice transmission failed | fiscal_code=%s error=%s",
                fiscal_code,
                e,
            )
            _record_transmission_failure(db, fiscal_code, e)


def _update_fiscal_invoice(db, fi: FiscalInvoice, tx_result: dict) -> None:
    """Update fiscal invoice with transmission result."""
    from datetime import datetime, timezone

    fi.firs_validation_status = tx_result.get("status", fi.firs_validation_status)
    if tx_result.get("transaction_id"):
        fi.firs_transaction_id = tx_result["transaction_id"]

    existing = fi.firs_response or {}
    existing["transmission"] = tx_result
    fi.firs_response = existing

    if tx_result.get("status") == "validated" and not fi.transmitted_at:
        fi.transmitted_at = datetime.now(timezone.utc)

    db.commit()


def _record_transmission_failure(db, fiscal_code: str, error: Exception) -> None:
    """Record transmission failure as alert."""
    try:
        from app.models.alert_models import AlertEvent

        evt = AlertEvent(
            category="fiscal.transmit",
            message=f"Transmit failed {fiscal_code}: {error}",
            severity="error",
        )
        db.add(evt)
        db.commit()
    except Exception:
        db.rollback()


# ── Tax report notification helpers ──────────────────────────────────


def _is_valid_phone(phone: str | None) -> bool:
    """Return True if phone looks like real digits (not an OAuth placeholder)."""
    if not phone:
        return False
    digits = phone.lstrip("+")
    return digits.isdigit() and len(digits) >= 10


def _notify_tax_report_whatsapp(
    user: User,
    period: str,
    pdf_url: str | None,
) -> bool:
    """Try to send a tax-report-ready notification via WhatsApp.

    Attempts template first (works outside 24h window), then plain text
    if the conversation window is open.
    """
    if not _is_valid_phone(user.phone):
        return False

    try:
        from app.bot.conversation_window import is_window_open
        from app.core.whatsapp import get_whatsapp_client
        from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

        if not can_send_whatsapp(priority=False):
            return False

        client = get_whatsapp_client()
        template_name = getattr(settings, "WHATSAPP_TEMPLATE_TAX_REPORT_READY", None)
        template_lang = getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en")

        # Template first
        if template_name:
            first_name = (user.name or "").split()[0] or "there"
            ok = client.send_template(
                user.phone,
                template_name,
                template_lang,
                components=[
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": f"{first_name}, your "},
                            {"type": "text", "text": period},
                        ],
                    }
                ],
            )
            if ok:
                record_whatsapp_send(priority=False)
                return True

        # Plain text fallback (only inside 24h window)
        if is_window_open(user.phone):
            msg = (
                f"\U0001f4ca Your *{period} Tax Report* is ready!\n\n"
                "View and download it from your dashboard:\n"
                "\U0001f517 suoops.com/dashboard/tax"
            )
            ok = client.send_text(user.phone, msg)
            if ok:
                record_whatsapp_send(priority=False)
            return ok

    except Exception as e:
        logger.warning("Tax report WA notification failed for user %s: %s", user.id, e)

    return False


def _send_tax_report_email(
    to_email: str,
    name: str | None,
    period: str,
    pdf_url: str | None,
) -> bool:
    """Send a tax-report-ready notification via email."""
    display_name = (name or "").split()[0] if name else "there"

    headline = f"Your {period} Tax Report Is Ready"
    # NOTE: don't embed a presigned S3 URL here — it expires (~1h) long before the
    # user opens the email ("AccessDenied / Request has expired"). Send them to the
    # dashboard, which mints a fresh download link on demand.
    body_html = (
        f"Your monthly tax report for <b>{period}</b> has been generated "
        "and is ready to view and download on your dashboard."
    )

    tpl_path = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "email", "engagement_tip.html")
    try:
        with open(tpl_path) as f:
            tpl = Template(f.read())
        html_body = tpl.render(
            headline=headline,
            name=display_name,
            body_text=body_html,
            tip_text="Keep your records tidy — download and store your reports monthly.",
            cta_url="https://suoops.com/dashboard/tax",
            cta_label="View Tax Report \u2192",
        )
    except Exception:
        html_body = f"<p>Hi {display_name},</p><p>{headline}.</p><p>{body_html}</p>"

    plain_body = (
        f"Hi {display_name},\n\n"
        f"Your {period} tax report has been generated.\n\n"
        f"View and download it at https://suoops.com/dashboard/tax"
    )

    subject = f"\U0001f4ca Your {period} Tax Report Is Ready \u2014 SuoOps"

    from app.utils.smtp import send_email_with_fallback

    try:
        return send_email_with_fallback(to_email, subject, html_body, plain_body)
    except Exception as e:
        logger.warning("Tax report email failed for %s: %s", to_email, e)
        return False
