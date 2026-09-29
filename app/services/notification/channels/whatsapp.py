from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.config import settings

if TYPE_CHECKING:  # pragma: no cover
    from app.models import models
    from app.services.notification.service import NotificationService

logger = logging.getLogger(__name__)


class WhatsAppChannel:
    """Encapsulates WhatsApp messaging for invoices and receipts.

    Centralizes the WhatsApp first-time customer logic:
    - For customers who have opted-in (replied before): send full invoice with payment details
    - For new customers: send template message and mark invoice as pending follow-up

    This ensures consistent behavior whether invoices are created from:
    - Dashboard (via NotificationService)
    - WhatsApp bot (via InvoiceIntentProcessor)
    """

    def __init__(self, service: NotificationService) -> None:
        self._service = service

    async def send_invoice(
        self,
        invoice: models.Invoice,
        recipient_phone: str,
        pdf_url: str | None,
    ) -> bool:
        """Send invoice notification to customer via WhatsApp.

        For RETURNING customers (already opted-in):
        - Send full invoice as regular message with PDF immediately
        - They're within 24-hour window since they've interacted before

        For NEW customers (not opted-in):
        - Send template message (works outside 24-hour window)
        - Mark invoice as pending, PDF sent when they reply "OK"

        Returns True if message was sent successfully.
        """
        logger.info(
            "[WHATSAPP CHANNEL] send_invoice called for %s to phone=%s, pdf_url=%s",
            invoice.invoice_id,
            recipient_phone,
            pdf_url[:50] + "..." if pdf_url else None,
        )
        try:
            if not self._service.whatsapp_key or not self._service.whatsapp_phone_number_id:
                logger.warning("WhatsApp not configured. Set WHATSAPP_API_KEY and WHATSAPP_PHONE_NUMBER_ID")
                return False

            from app.bot.whatsapp_client import WhatsAppClient

            client = WhatsAppClient(self._service.whatsapp_key)

            # Check if customer is already opted-in (returning customer)
            customer = getattr(invoice, "customer", None)
            is_opted_in = False
            if customer:
                is_opted_in = getattr(customer, "whatsapp_opted_in", False)
                logger.info(
                    "[WHATSAPP] Customer check: id=%s, phone=%s, whatsapp_opted_in=%s",
                    getattr(customer, "id", "?"),
                    getattr(customer, "phone", "?"),
                    is_opted_in,
                )
            else:
                logger.warning("[WHATSAPP] No customer object on invoice %s", invoice.invoice_id)

            if is_opted_in:
                # Returning customer - send full invoice directly (they're in 24-hour window)
                logger.info("[WHATSAPP] Customer %s is opted-in, sending full invoice directly", recipient_phone)
                return await self._send_full_invoice(client, invoice, recipient_phone, pdf_url)

            # New customer - use template (works outside 24-hour window)
            logger.info("[WHATSAPP] Customer %s is not opted-in, using template", recipient_phone)

            # Preferred: the invoice_with_payment template carries the PDF in a
            # DOCUMENT header — a first-time customer gets it without replying,
            # and the body carries no bank number (they pay via the link).
            payment_template = getattr(settings, "WHATSAPP_TEMPLATE_INVOICE_PAYMENT", None)
            doc_pdf = pdf_url if (pdf_url or "").startswith("http") else None
            if (
                payment_template
                and doc_pdf
                and self._send_doc_template(client, invoice, recipient_phone, doc_pdf, payment_template)
            ):
                logger.info("[WHATSAPP] invoice_with_payment (PDF attached) sent to %s", recipient_phone)
                return True

            template_sent = await self._send_template_only(client, invoice, recipient_phone)

            if not template_sent:
                return False

            # Don't send PDF here — wait for customer to reply (opt-in).
            # Sending PDF immediately wastes an API call because
            # send_document() silently fails (returns False, no exception)
            # when the customer hasn't messaged within the 24-hour window.
            # The PDF will be delivered via handle_customer_optin() when
            # they reply to the template.
            return True

        except Exception as e:  # pragma: no cover - network failures
            logger.error("Failed to send invoice via WhatsApp: %s", e)
            return False

    def _is_registered_user(self, phone: str, invoice: models.Invoice) -> bool:
        """Check if a phone number belongs to a registered business user."""
        from sqlalchemy.orm import object_session

        from app.models import models
        from app.utils.phone import normalize_phone

        normalized = normalize_phone(phone)

        # Get db session from invoice object
        db = object_session(invoice)
        if not db:
            return False

        # Check if phone exists in users table
        user = db.query(models.User).filter(models.User.phone == normalized).first()
        if user:
            logger.info("[WHATSAPP] Recipient phone %s is a registered user (ID: %s)", phone, user.id)
            return True
        return False

    async def _send_full_invoice(
        self,
        client,
        invoice: models.Invoice,
        recipient_phone: str,
        pdf_url: str | None,
    ) -> bool:
        """Send full invoice with payment details to opted-in customers."""
        business_name = "Business"
        if hasattr(invoice, "issuer") and invoice.issuer:
            business_name = getattr(invoice.issuer, "business_name", None) or business_name

        # Build payment message with bank details if available
        message = self._build_payment_message(invoice, business_name)

        client.send_text(recipient_phone, message)

        # Send PDF if available
        if pdf_url and pdf_url.startswith("http"):
            client.send_document(
                recipient_phone,
                pdf_url,
                f"Invoice_{invoice.invoice_id}.pdf",
                f"Invoice {invoice.invoice_id} - ₦{invoice.amount:,.2f}",
            )

        # Clear pending flag if it was set
        if getattr(invoice, "whatsapp_delivery_pending", False):
            invoice.whatsapp_delivery_pending = False
            # Note: Caller should commit the session

        logger.info("[WHATSAPP] Full invoice sent to opted-in customer %s", recipient_phone)
        return True

    async def _send_template_only(
        self,
        client,
        invoice: models.Invoice,
        recipient_phone: str,
    ) -> bool:
        """Fallback text template used only when the PDF isn't ready to attach.

        Sends the basic invoice template; the PDF is delivered when the customer
        replies. (The preferred path is the invoice_with_payment doc-header
        template in ``send_invoice``.)
        """
        template_name = getattr(settings, "WHATSAPP_TEMPLATE_INVOICE", None)
        if not template_name:
            logger.warning("[WHATSAPP] No basic invoice template configured, cannot notify customer")
            return False

        customer_name = invoice.customer.name if invoice.customer else "valued customer"
        amount_text = f"₦{invoice.amount:,.2f}"

        # Build items text
        items_text = self._build_items_text(invoice)
        items_with_cta = f"{items_text}. Reply 'OK' to receive invoice PDF & payment details"

        components = [
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": customer_name},
                    {"type": "text", "text": invoice.invoice_id},
                    {"type": "text", "text": amount_text},
                    {"type": "text", "text": items_with_cta},
                ],
            }
        ]

        template_sent = client.send_template(
            recipient_phone,
            template_name=template_name,
            language=getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en"),
            components=components,
        )

        if template_sent:
            # Mark invoice as pending follow-up delivery
            invoice.whatsapp_delivery_pending = True
            # Note: Caller should commit the session
            logger.info("[WHATSAPP] Template sent to customer %s, invoice marked pending", recipient_phone)
        else:
            logger.warning("[WHATSAPP] Failed to send template to %s", recipient_phone)

        return template_sent

    def _send_doc_template(
        self,
        client,
        invoice: models.Invoice,
        recipient_phone: str,
        pdf_url: str,
        template_name: str,
    ) -> bool:
        """Send the short invoice template with the PDF as a document header.

        Body params (6): customer_name, business_name, invoice_id, amount, items,
        payment_link. No bank number — the customer pays via the link.
        """
        customer_name = invoice.customer.name if invoice.customer else "valued customer"
        amount_text = f"₦{invoice.amount:,.2f}"
        items_text = self._build_items_text(invoice)
        issuer = getattr(invoice, "issuer", None)
        business_name = getattr(issuer, "business_name", None) or getattr(issuer, "name", None) or "your business"
        frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
        payment_link = f"{frontend_url.rstrip('/')}/pay/{invoice.invoice_id}"

        components = [
            {
                "type": "header",
                "parameters": [
                    {
                        "type": "document",
                        "document": {
                            "link": pdf_url,
                            "filename": f"Invoice_{invoice.invoice_id}.pdf",
                        },
                    }
                ],
            },
            {
                "type": "body",
                "parameters": [
                    {"type": "text", "text": customer_name},
                    {"type": "text", "text": business_name},
                    {"type": "text", "text": invoice.invoice_id},
                    {"type": "text", "text": amount_text},
                    {"type": "text", "text": items_text},
                    {"type": "text", "text": payment_link},
                ],
            },
        ]
        return client.send_template(
            recipient_phone,
            template_name=template_name,
            language=getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en"),
            components=components,
        )

    def _build_payment_message(self, invoice: models.Invoice, business_name: str) -> str:
        """Build payment message with bank details."""
        message = (
            f"📄 New Invoice from {business_name}\n\n"
            f"Invoice ID: {invoice.invoice_id}\n"
            f"Amount: ₦{invoice.amount:,.2f}\n"
            f"Status: {invoice.status.upper()}\n"
        )

        if invoice.due_date:
            message += f"Due: {invoice.due_date.strftime('%B %d, %Y')}\n"

        # Add bank details if available
        issuer = getattr(invoice, "issuer", None)
        if issuer and getattr(issuer, "bank_name", None) and getattr(issuer, "account_number", None):
            message += (
                "\n💳 Payment Details (Bank Transfer):\n"
                f"Bank: {issuer.bank_name}\n"
                f"Account: {issuer.account_number}\n"
            )
            if getattr(issuer, "account_name", None):
                message += f"Name: {issuer.account_name}\n"

        # Add payment link
        frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
        payment_link = f"{frontend_url.rstrip('/')}/pay/{invoice.invoice_id}"
        message += f"\n🔗 View & Pay: {payment_link}"

        return message

    def _build_items_text(self, invoice: models.Invoice) -> str:
        """Build a text representation of invoice line items."""
        if not invoice.lines or len(invoice.lines) == 0:
            return "Invoice items"

        # Limit to first 3 items to keep message short
        lines = invoice.lines[:3]
        parts = []
        for line in lines:
            desc = getattr(line, "description", "Item")
            qty = getattr(line, "quantity", 1)
            parts.append(f"{desc} x{qty}")

        text = ", ".join(parts)
        if len(invoice.lines) > 3:
            text += f" +{len(invoice.lines) - 3} more"

        return text

    async def send_receipt(
        self,
        invoice: models.Invoice,
        recipient_phone: str,
        pdf_url: str | None,
    ) -> bool:
        """Send payment receipt to customer via WhatsApp.

        Preferred: the payment_receipt template with the receipt PDF in a
        DOCUMENT header, so the receipt (and its PDF) is delivered even outside
        the 24-hour window. Falls back to a text message + document if the
        template send fails (e.g. before the header version is approved).
        """
        try:
            if not self._service.whatsapp_key or not self._service.whatsapp_phone_number_id:
                logger.warning("WhatsApp not configured for receipt")
                return False

            import datetime as dt

            from app.bot.whatsapp_client import WhatsAppClient

            client = WhatsAppClient(self._service.whatsapp_key)

            # Try to use receipt template first (works outside 24-hour window)
            template_name = getattr(settings, "WHATSAPP_TEMPLATE_RECEIPT", None)
            has_pdf = bool(pdf_url and pdf_url.startswith("http"))

            # The approved receipt template has a REQUIRED document header, so we
            # can only use it when we have a PDF link. Sending it without the
            # header makes Meta reject the message (params mismatch) AND dings the
            # template's quality rating — so skip straight to the text path when
            # the PDF isn't ready rather than send a malformed template.
            if template_name and not has_pdf:
                logger.warning(
                    "[WHATSAPP] Receipt PDF missing for %s — skipping the %s template "
                    "(it needs a document header); trying a text message instead.",
                    recipient_phone,
                    template_name,
                )

            if template_name and has_pdf:
                # Use payment_receipt template
                customer_name = invoice.customer.name if invoice.customer else "valued customer"
                amount_text = f"₦{invoice.amount:,.2f}"
                date_text = dt.datetime.now().strftime("%b %d, %Y")

                components: list[dict] = []
                # DOCUMENT header carries the receipt PDF (delivered outside 24h).
                if has_pdf:
                    components.append(
                        {
                            "type": "header",
                            "parameters": [
                                {
                                    "type": "document",
                                    "document": {
                                        "link": pdf_url,
                                        "filename": f"Receipt_{invoice.invoice_id}.pdf",
                                    },
                                }
                            ],
                        }
                    )
                components.append(
                    {
                        "type": "body",
                        "parameters": [
                            {"type": "text", "text": customer_name},
                            {"type": "text", "text": invoice.invoice_id},
                            {"type": "text", "text": amount_text},
                            {"type": "text", "text": date_text},
                        ],
                    }
                )

                template_sent = client.send_template(
                    recipient_phone,
                    template_name=template_name,
                    language=getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en"),
                    components=components,
                )

                if template_sent:
                    logger.info("[WHATSAPP] Receipt template sent to %s", recipient_phone)
                    # If the template didn't carry the PDF in a header (no pdf at
                    # send time), try a best-effort document (works inside 24h).
                    if not has_pdf and pdf_url and pdf_url.startswith("http"):
                        client.send_document(
                            recipient_phone,
                            pdf_url,
                            f"Receipt_{invoice.invoice_id}.pdf",
                            f"Payment Receipt - {amount_text}",
                        )
                    return True
                else:
                    logger.warning("[WHATSAPP] Receipt template failed for %s, trying regular message", recipient_phone)

            # Fallback to regular message (may fail if outside 24-hour window)
            receipt_message = (
                "🎉 Payment Received!\n\n"
                "Thank you for your payment!\n\n"
                f"📄 Invoice: {invoice.invoice_id}\n"
                f"💰 Amount Paid: ₦{invoice.amount:,.2f}\n"
                "✅ Status: PAID\n\n"
                "Your receipt is attached below."
            )

            client.send_text(recipient_phone, receipt_message)

            if pdf_url and pdf_url.startswith("http"):
                client.send_document(
                    recipient_phone,
                    pdf_url,
                    f"Receipt_{invoice.invoice_id}.pdf",
                    f"Payment Receipt - ₦{invoice.amount:,.2f}",
                )

            return True
        except Exception as e:  # pragma: no cover - network failures
            logger.error("Failed to send receipt via WhatsApp: %s", e)
            return False
