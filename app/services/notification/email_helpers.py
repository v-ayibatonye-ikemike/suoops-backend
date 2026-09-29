from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import TYPE_CHECKING

import aiosmtplib
import httpx

from app.core.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from app.models import models
    from app.services.notification_service import NotificationService

logger = logging.getLogger(__name__)


async def send_invoice_email(
    service: NotificationService,
    invoice: models.Invoice,
    recipient_email: str,
    pdf_url: str | None = None,
    subject: str = "New Invoice",
) -> bool:
    """Send invoice email with PDF attachment (extracted helper)."""
    try:
        smtp_config = service._get_smtp_config()
        if not smtp_config:
            logger.warning("No email provider configured")
            return False
        smtp_host = smtp_config["host"]
        smtp_port = smtp_config["port"]
        smtp_user = smtp_config["user"]
        smtp_password = smtp_config["password"]
        from_email = getattr(settings, "FROM_EMAIL", smtp_user)

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = recipient_email
        msg["Subject"] = f"{subject} - {invoice.invoice_id}"

        frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
        payment_link = f"{frontend_url.rstrip('/')}/pay/{invoice.invoice_id}"

        body = f"""
Hello {invoice.customer.name if invoice.customer else 'Customer'},

Your invoice {invoice.invoice_id} for ₦{invoice.amount:,.2f} has been generated.

Invoice Details:
- Invoice ID: {invoice.invoice_id}
- Amount: ₦{invoice.amount:,.2f}
- Status: {invoice.status.upper()}
{f"- Due Date: {invoice.due_date.strftime('%B %d, %Y')}" if invoice.due_date else ""}

📌 Click here to make payment:
{payment_link}

On the payment page you can:
- View bank name, account number, and account name
- Use one-click "Copy" buttons or copy all details at once
- Notify the business after you've made the transfer

After you complete the transfer, tap "I've sent the transfer" on that page
so the business can confirm and your receipt will be issued.

Please find your invoice attached as a PDF.

Thank you for your business!

---
Powered by SuoOps
"""
        msg.attach(MIMEText(body, "plain"))

        if pdf_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(pdf_url)
                    response.raise_for_status()
                pdf_attachment = MIMEApplication(response.content, _subtype="pdf")
                pdf_attachment.add_header(
                    "Content-Disposition", "attachment", filename=f"Invoice_{invoice.invoice_id}.pdf"
                )
                msg.attach(pdf_attachment)
            except Exception as e:  # pragma: no cover
                logger.error("Failed to download PDF for email attachment: %s", e)

        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=smtp_user,
            password=smtp_password,
            start_tls=True,
        )
        logger.info("Sent invoice email to %s for invoice %s", recipient_email, invoice.invoice_id)
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to send invoice email: %s", e)
        return False


async def send_receipt_email(
    service: NotificationService,
    invoice: models.Invoice,
    recipient_email: str,
    pdf_url: str | None = None,
) -> bool:
    """Send payment receipt email (extracted helper)."""
    try:
        smtp_config = service._get_smtp_config()
        if not smtp_config:
            logger.warning("No email provider configured")
            return False
        smtp_host = smtp_config["host"]
        smtp_port = smtp_config["port"]
        smtp_user = smtp_config["user"]
        smtp_password = smtp_config["password"]
        from_email = getattr(settings, "FROM_EMAIL", smtp_user)

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = recipient_email
        business_name = None
        try:
            if hasattr(invoice, "issuer") and invoice.issuer:
                business_name = getattr(invoice.issuer, "business_name", None) or getattr(invoice.issuer, "name", None)
        except Exception:  # pragma: no cover
            business_name = None
        subject_business = f"{business_name} - " if business_name else ""
        msg["Subject"] = f"Payment Receipt - {subject_business}{invoice.invoice_id}"

        body = f"""
Hello {invoice.customer.name if invoice.customer else 'Customer'},

Thank you for your payment! We have received your payment for invoice {invoice.invoice_id}.

Payment Confirmation:
- Invoice ID: {invoice.invoice_id}
- Amount Paid: ₦{invoice.amount:,.2f}
- Status: PAID ✓
- Payment Date: {datetime.now(timezone.utc).strftime('%B %d, %Y %H:%M:%S UTC')}
- Payment Method: Bank Transfer

Your receipt is attached as a PDF for your records.

Thank you for your business!

---
Powered by SuoOps
"""
        msg.attach(MIMEText(body, "plain"))

        effective_pdf_url = pdf_url or getattr(invoice, "receipt_pdf_url", None)
        if effective_pdf_url:
            try:
                async with httpx.AsyncClient(timeout=30.0) as client:
                    response = await client.get(effective_pdf_url)
                    response.raise_for_status()
                pdf_attachment = MIMEApplication(response.content, _subtype="pdf")
                pdf_attachment.add_header(
                    "Content-Disposition", "attachment", filename=f"Receipt_{invoice.invoice_id}.pdf"
                )
                msg.attach(pdf_attachment)
            except Exception as e:  # pragma: no cover
                logger.error("Failed to download PDF for receipt: %s", e)

        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=smtp_user,
            password=smtp_password,
            start_tls=True,
        )
        logger.info("Sent receipt email to %s for invoice %s", recipient_email, invoice.invoice_id)
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to send receipt email: %s", e)
        return False


async def send_simple_email(service: NotificationService, to_email: str, subject: str, body: str) -> bool:
    """Send a simple text email (helper)."""
    try:
        smtp_config = service._get_smtp_config()
        if not smtp_config:
            logger.warning("No email provider configured")
            return False
        smtp_host = smtp_config["host"]
        smtp_port = smtp_config["port"]
        smtp_user = smtp_config["user"]
        smtp_password = smtp_config["password"]
        from_email = getattr(settings, "FROM_EMAIL", smtp_user)

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain"))

        await aiosmtplib.send(
            msg,
            hostname=smtp_host,
            port=smtp_port,
            username=smtp_user,
            password=smtp_password,
            start_tls=True,
        )
        logger.info("Simple email sent to %s", to_email)
        return True
    except Exception as e:  # pragma: no cover
        logger.error("Failed to send email to %s: %s", to_email, e)
        return False
