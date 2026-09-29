from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import settings
from app.services.notification.channels.email import EmailChannel
from app.services.notification.channels.whatsapp import WhatsAppChannel

if TYPE_CHECKING:  # pragma: no cover
    from app.models import models

logger = logging.getLogger(__name__)


class NotificationService:
    """Facade for sending notifications via Email and WhatsApp.

    Public methods preserved for compatibility while delegating to channel classes.
    """

    def __init__(self) -> None:
        # WhatsApp setup
        self.whatsapp_key = getattr(settings, "WHATSAPP_API_KEY", None)
        self.whatsapp_phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", None)
        # Brevo API key for email (also used for sender config)
        self.brevo_api_key = getattr(settings, "BREVO_API_KEY", None)
        self.brevo_sender_name = getattr(settings, "BREVO_SENDER_NAME", "SuoOps")
        # Channels (Email + WhatsApp only)
        self.email = EmailChannel(self)
        self.whatsapp = WhatsAppChannel(self)

    def _get_smtp_config(self) -> dict[str, str | int] | None:
        """Provider-agnostic SMTP config (generic SMTP_* first, Brevo vars as
        fallback) so switching email providers is a pure env change.

        Returns:
            dict with host, port, user, password, from_email — or None if unset.
        """
        from app.utils.smtp import get_smtp_config

        host, port, user, password, from_email = get_smtp_config()
        if not user or not password:
            logger.warning("Email not configured. Set SMTP_HOST/SMTP_USER/SMTP_PASSWORD (or legacy Brevo vars).")
            return None
        return {
            "host": host,
            "port": port,
            "user": user,
            "password": password,
            "from_email": from_email,
            "provider": "SMTP",
        }

    # --- Email ---
    async def send_invoice_email(
        self,
        invoice: models.Invoice,
        recipient_email: str,
        pdf_url: str | None = None,
        subject: str = "New Invoice",
    ) -> bool:
        return await self.email.send_invoice(invoice, recipient_email, pdf_url, subject)

    async def send_receipt_email(
        self,
        invoice: models.Invoice,
        recipient_email: str,
        pdf_url: str | None = None,
    ) -> bool:
        return await self.email.send_receipt(invoice, recipient_email, pdf_url)

    async def send_email(self, to_email: str, subject: str, body: str) -> bool:
        return await self.email.send_simple(to_email, subject, body)

    # --- WhatsApp ---
    async def send_invoice_whatsapp(
        self,
        invoice: models.Invoice,
        recipient_phone: str,
        pdf_url: str | None = None,
    ) -> bool:
        return await self.whatsapp.send_invoice(invoice, recipient_phone, pdf_url)

    async def send_receipt_whatsapp(
        self,
        invoice: models.Invoice,
        recipient_phone: str,
        pdf_url: str | None = None,
    ) -> bool:
        return await self.whatsapp.send_receipt(invoice, recipient_phone, pdf_url)

    # --- Composite ---
    async def send_invoice_notification(
        self,
        invoice: models.Invoice,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        pdf_url: str | None = None,
    ) -> dict[str, bool]:
        """Send invoice notification via Email and/or WhatsApp."""
        logger.info(
            "[NOTIFY SERVICE] send_invoice_notification called - invoice=%s, email=%s, phone=%s, pdf_url=%s",
            invoice.invoice_id,
            customer_email,
            customer_phone[:6] + "..." if customer_phone else None,
            pdf_url[:50] + "..." if pdf_url else None,
        )
        results = {"email": False, "whatsapp": False}
        if customer_email:
            results["email"] = await self.send_invoice_email(invoice, customer_email, pdf_url)
        if customer_phone:
            results["whatsapp"] = await self.send_invoice_whatsapp(invoice, customer_phone, pdf_url)
        logger.info(
            "[NOTIFY SERVICE] Invoice notification complete - invoice=%s, Email=%s, WhatsApp=%s",
            invoice.invoice_id,
            results["email"],
            results["whatsapp"],
        )
        return results

    async def send_receipt_notification(
        self,
        invoice: models.Invoice,
        customer_email: str | None = None,
        customer_phone: str | None = None,
        pdf_url: str | None = None,
    ) -> dict[str, bool]:
        """Send receipt notification via Email and/or WhatsApp."""
        results = {"email": False, "whatsapp": False}
        if customer_email:
            results["email"] = await self.send_receipt_email(
                invoice,
                customer_email,
                invoice.receipt_pdf_url or pdf_url,
            )
        if customer_phone:
            results["whatsapp"] = await self.send_receipt_whatsapp(
                invoice,
                customer_phone,
                invoice.receipt_pdf_url or pdf_url,
            )
        logger.info(
            "Receipt notification sent - Email: %s, WhatsApp: %s",
            results["email"],
            results["whatsapp"],
        )
        return results
