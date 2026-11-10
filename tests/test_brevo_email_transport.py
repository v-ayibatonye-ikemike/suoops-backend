import base64
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.core.config import settings
from app.utils import smtp


@pytest.fixture
def api_transport(monkeypatch):
    monkeypatch.setattr(settings, "EMAIL_TRANSPORT", "brevo_api")
    monkeypatch.setattr(settings, "BREVO_API_KEY", "xkeysib-test-only")
    monkeypatch.setattr(settings, "FROM_EMAIL", "SuoOps <noreply@suoops.com>")
    for field in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_USER_ZEP", "SMTP_PASSWORD_ZEP", "BREVO_SMTP_LOGIN", "BREVO_SMTP_KEY"):
        monkeypatch.setattr(settings, field, None)
    monkeypatch.setattr(smtp.smtplib, "SMTP", MagicMock(side_effect=AssertionError("SMTP must not be used")))
    monkeypatch.setattr("app.services.email_suppression.is_suppressed", lambda email: False)
    client = MagicMock()
    client.__enter__.return_value = client
    client.post.return_value = httpx.Response(
        201, json={"messageId": "<accepted@example.test>"},
        request=httpx.Request("POST", "https://api.brevo.com/v3/smtp/email"),
    )
    monkeypatch.setattr(smtp.httpx, "Client", MagicMock(return_value=client))
    return client


def test_api_only_mime_preserves_content_and_headers(api_transport):
    message = MIMEMultipart()
    message["From"] = settings.FROM_EMAIL
    message["To"] = "Owner <owner@example.test>"
    message["Cc"] = "copy@example.test"
    message["Bcc"] = "hidden@example.test"
    message["Reply-To"] = "Feedback <feedback@suoops.com>"
    message["Subject"] = "Invoice"
    message["List-Unsubscribe"] = "<https://suoops.com/unsubscribe/test>"
    message["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    message.attach(MIMEText("Amount: ₦100", "plain", "utf-8"))
    message.attach(MIMEText("<b>Invoice</b>", "html"))
    pdf = MIMEApplication(b"%PDF-test", _subtype="pdf")
    pdf.add_header("Content-Disposition", "attachment", filename="Invoice_test.pdf")
    message.attach(pdf)

    assert smtp.email_is_configured()
    assert smtp.get_smtp_configs() == []
    assert smtp.send_email_message(message)
    call = api_transport.post.call_args
    assert call.args == ("https://api.brevo.com/v3/smtp/email",)
    assert call.kwargs["headers"] == {"api-key": "xkeysib-test-only"}
    payload = call.kwargs["json"]
    assert payload["sender"] == {"email": "noreply@suoops.com", "name": "SuoOps"}
    assert payload["to"] == [{"email": "owner@example.test", "name": "Owner"}]
    assert payload["cc"] == [{"email": "copy@example.test"}]
    assert payload["bcc"] == [{"email": "hidden@example.test"}]
    assert payload["replyTo"] == {"email": "feedback@suoops.com", "name": "Feedback"}
    assert payload["textContent"] == "Amount: ₦100"
    assert payload["htmlContent"] == "<b>Invoice</b>"
    assert payload["headers"]["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert payload["attachment"] == [{"name": "Invoice_test.pdf", "content": base64.b64encode(b"%PDF-test").decode()}]


def test_otp_api_only_bypasses_suppression(api_transport, monkeypatch):
    from app.services.otp_service import OTPService

    suppressed = MagicMock(side_effect=AssertionError("OTP must bypass suppression"))
    monkeypatch.setattr("app.services.email_suppression.is_suppressed", suppressed)
    OTPService()._send_email_otp("owner@example.test", "TEST-OTP", "login")
    assert "TEST-OTP" in api_transport.post.call_args.kwargs["json"]["textContent"]
    suppressed.assert_not_called()


@pytest.mark.parametrize("status,result", [(401, {}), (429, {}), (200, {"messageId": "id"}), (201, {}), (201, {"messageId": ""})])
def test_requires_confirmed_acceptance(api_transport, status, result, caplog):
    api_transport.post.return_value = httpx.Response(
        status, json=result, request=httpx.Request("POST", "https://api.brevo.com/v3/smtp/email"),
    )
    assert not smtp.send_email_with_fallback(
        "owner@example.test", "Private", None, "TEST-OTP", check_suppression=False,
    )
    assert "TEST-OTP" not in caplog.text
    assert settings.BREVO_API_KEY not in caplog.text
    assert api_transport.post.call_count == 1


def test_retention_uncertain_acceptance_is_not_retried(api_transport):
    api_transport.post.side_effect = httpx.ReadTimeout("redacted")
    with pytest.raises(smtp.EmailOutcomeUnknown):
        smtp.send_retention_email("owner@example.test", "Retention", "Body", "https://suoops.com/unsubscribe/test")
    assert api_transport.post.call_count == 1


def test_retention_missing_message_id_is_unknown(api_transport):
    api_transport.post.return_value = httpx.Response(
        201, json={}, request=httpx.Request("POST", "https://api.brevo.com/v3/smtp/email"),
    )
    with pytest.raises(smtp.EmailOutcomeUnknown):
        smtp.send_retention_email("owner@example.test", "Retention", "Body", "https://suoops.com/unsubscribe/test")


def test_batch_order_and_suppression(api_transport, monkeypatch):
    monkeypatch.setattr("app.services.email_suppression.is_suppressed", lambda email: email.startswith("skip"))
    emails = [
        ("first@example.test", "First", None, "Body"),
        ("skip@example.test", "Skip", None, "Body"),
        ("last@example.test", "Last", "<p>Body</p>", "Body"),
    ]
    assert smtp.send_smtp_batch(emails) == [True, False, True]
    assert [call.kwargs["json"]["subject"] for call in api_transport.post.call_args_list] == ["First", "Last"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["invoice", "receipt", "simple"])
async def test_async_notifications_use_api_and_preserve_pdf(api_transport, monkeypatch, kind):
    from app.services.notification import email_helpers
    from app.services.notification.service import NotificationService

    monkeypatch.setattr(email_helpers.aiosmtplib, "send", AsyncMock(side_effect=AssertionError("No SMTP")))
    download = MagicMock()
    download.__aenter__ = AsyncMock(return_value=download)
    download.__aexit__ = AsyncMock(return_value=False)
    download.get = AsyncMock(return_value=httpx.Response(
        200, content=b"%PDF-live-test", request=httpx.Request("GET", "https://example.test/invoice.pdf"),
    ))
    monkeypatch.setattr(email_helpers.httpx, "AsyncClient", MagicMock(return_value=download))
    service = NotificationService()
    invoice = SimpleNamespace(
        invoice_id="TEST", amount=100, status="pending", customer=None, due_date=None,
        issuer=None, receipt_pdf_url=None,
    )
    if kind == "invoice":
        sent = await service.send_invoice_email(invoice, "owner@example.test", "https://example.test/invoice.pdf")
    elif kind == "receipt":
        sent = await service.send_receipt_email(invoice, "owner@example.test", "https://example.test/invoice.pdf")
    else:
        sent = await service.send_email("owner@example.test", "Subject", "Body")
    assert sent
    payload = api_transport.post.call_args.kwargs["json"]
    if kind != "simple":
        assert base64.b64decode(payload["attachment"][0]["content"]) == b"%PDF-live-test"
        assert payload["attachment"][0]["name"] == f"{kind.title()}_TEST.pdf"


def test_admin_and_support_accept_api_only_configuration(api_transport, monkeypatch):
    from app.api import routes_admin_auth, routes_support

    monkeypatch.setattr(settings, "SMTP_HOST", None)
    assert routes_support._get_smtp_config() is not None
    assert routes_admin_auth.send_admin_invite_email("owner@example.test", "Owner", "https://example.test/invite")
    assert api_transport.post.call_count == 1
