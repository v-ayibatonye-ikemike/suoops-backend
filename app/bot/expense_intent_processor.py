"""
Expense intent processor for WhatsApp messages.

Handles expense-related messages: text, voice, and photo receipts.
"""

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.bot.whatsapp_client import WhatsAppClient
from app.models import models
from app.services.expense_nlp_service import ExpenseNLPService
from app.services.expense_ocr_service import ExpenseOCRService
from app.services.expense_service import record_expense_invoice
from app.utils.currency_fmt import fmt_money, get_user_currency

logger = logging.getLogger(__name__)


class ExpenseIntentProcessor:
    """Process expense-related WhatsApp messages"""

    def __init__(self, db: Session, client: WhatsAppClient):
        self.db = db
        self.client = client
        self.nlp_service = ExpenseNLPService()
        self.ocr_service = ExpenseOCRService(db)

    async def handle(
        self,
        sender: str,
        parse: dict[str, Any],
        message: dict[str, Any],
    ) -> bool:
        """
        Handle expense intent from parsed message.

        Args:
            sender: WhatsApp phone number
            parse: Parsed message from NLP service
            message: Raw WhatsApp message

        Returns:
            True if the message was handled as an expense, False otherwise.
        """
        # Check if this is an expense-related message
        if not self._is_expense_message(parse, message):
            return False

        # Get user from phone number (handle all Nigerian phone format variants)
        from app.models.models import User
        from app.utils.phone import get_phone_variants

        phone_candidates = get_phone_variants(sender)
        user = self.db.query(User).filter(User.phone.in_(list(phone_candidates))).first()

        if not user:
            self.client.send_text(
                sender,
                "❌ Your number isn't linked to an account yet.\n\n"
                "Register free at suoops.com to start tracking invoices & expenses!",
            )
            return True  # Handled (sent error message)

        try:
            # Process based on message type
            msg_type = message.get("type", "text")

            if msg_type == "image":
                await self._handle_photo_receipt(user.id, sender, message)
            else:
                await self._handle_text_expense(user.id, sender, parse, message)

            return True  # Handled (created expense or sent confirmation)

        except ValueError as e:
            # Handle validation errors (e.g., couldn't extract amount)
            error_msg = str(e)
            logger.warning("Validation error processing expense for %s: %s", sender, error_msg)

            if "amount" in error_msg.lower():
                self.client.send_text(
                    sender,
                    "❌ I couldn't find an amount in your message.\n\n"
                    "*How to record an expense:*\n"
                    "• `Expense: ₦5,000 for transport`\n"
                    "• `Spent 3000 naira on data`\n"
                    "• `₦2,500 fuel`\n\n"
                    "Please include an amount (e.g., ₦5,000 or 5000 naira).",
                )
            else:
                self.client.send_text(
                    sender,
                    "❌ I couldn't process that expense.\n\n"
                    "*How to record an expense:*\n"
                    "• `Expense: ₦5,000 for transport`\n"
                    "• `Spent 3000 naira on data`\n\n"
                    "Please check the format and try again.",
                )
            return True  # Handled (sent error message)

        except Exception as e:
            # Handle unexpected errors gracefully
            error_str = str(e).lower()
            logger.error("Error processing expense for %s: %s", sender, e, exc_info=True)

            # Database constraint errors
            if "not-null" in error_str or "notnull" in error_str:
                self.client.send_text(
                    sender,
                    "❌ Something was missing from your expense.\n\n"
                    "*Please try again with this format:*\n"
                    "`Expense: ₦5,000 for [description]`\n\n"
                    "Example: `Expense: ₦3,500 for transport to Lekki`",
                )
            # Connection/timeout errors
            elif "connection" in error_str or "timeout" in error_str:
                self.client.send_text(sender, "❌ Network issue. Please try again in a moment.")
            # Generic fallback - user-friendly message
            else:
                self.client.send_text(
                    sender,
                    "❌ Sorry, I couldn't process that expense.\n\n"
                    "*Tips:*\n"
                    "• Make sure to include an amount (e.g., ₦5,000)\n"
                    "• Add a description (e.g., for transport)\n\n"
                    "*Example:*\n"
                    "`Expense: ₦2,500 for data subscription`\n\n"
                    "Or send a photo of your receipt 📸",
                )
            return True  # Handled (sent error message)

    def _is_expense_message(
        self,
        parse: dict[str, Any],
        message: dict[str, Any],
    ) -> bool:
        """
        Check if message is about an expense.

        Triggers:
        - Contains "expense" keyword
        - Contains amount + expense keywords
        - Image message (assume receipt)

        Exclusions:
        - If intent is create_invoice, skip expense processing
        """
        # Skip if this is an invoice request
        intent = getattr(parse, "intent", None) if hasattr(parse, "intent") else parse.get("intent")
        if intent == "create_invoice":
            return False

        msg_type = message.get("type", "text")

        # Images are assumed to be receipts
        if msg_type == "image":
            return True

        # Check text content
        text = message.get("text", "").lower()

        # Skip if message contains "invoice" - likely an invoice request
        if "invoice" in text:
            return False

        expense_keywords = ["expense", "spent", "paid for", "bought", "purchase", "cost", "receipt"]

        # Check for expense keywords
        for keyword in expense_keywords:
            if keyword in text:
                return True

        # Check if contains amount (₦ or naira)
        if "₦" in text or "naira" in text or "ngn" in text:
            # Check for expense-related words nearby
            expense_context = ["for", "on", "rent", "data", "transport", "fuel", "supplies", "equipment", "repair"]
            for word in expense_context:
                if word in text:
                    return True

        return False

    async def _handle_text_expense(
        self,
        user_id: int,
        sender: str,
        parse: dict[str, Any],
        message: dict[str, Any],
    ) -> None:
        """
        Handle text-based expense message.

        Examples:
        - "Expense: ₦2,000 for internet data on Nov 10"
        - "₦5,000 market rent"
        - "Spent 3000 naira on transport"
        """
        text = message.get("text", "")

        # Extract expense details using NLP
        expense_data = self.nlp_service.extract_expense(text)

        # Ensure date is never None (database constraint)
        from datetime import date as date_type

        expense_date = expense_data["date"] or date_type.today()

        # Record as a unified expense-invoice so it shows on the dashboard too.
        invoice = record_expense_invoice(
            self.db,
            user_id=user_id,
            amount=expense_data["amount"],
            category=expense_data["category"],
            description=expense_data["description"],
            merchant=expense_data["merchant"],
            expense_date=expense_date,
            input_method="text",
            channel="whatsapp",
        )

        # Send confirmation
        await self._send_confirmation(sender, invoice)

    async def _handle_photo_receipt(
        self,
        user_id: int,
        sender: str,
        message: dict[str, Any],
    ) -> None:
        """
        Handle receipt photo message.

        Steps:
        1. Download image from WhatsApp
        2. OCR extraction
        3. Create expense record
        4. Send confirmation
        """
        media_id = message.get("image_id")
        if not media_id:
            self.client.send_text(sender, "❌ Could not get image from message")
            return

        # Check monthly OCR quota before downloading/processing
        from app.services.ocr_service import check_ocr_quota, record_ocr_use

        if not check_ocr_quota(user_id):
            self.client.send_text(
                sender,
                "📸 You've reached your monthly scan limit (10 receipts/month).\n\n"
                "Type expenses manually instead:\n"
                "• `Expense: ₦5,000 for transport`",
            )
            return

        # Download image from WhatsApp (resolve media ID → URL → bytes)
        try:
            media_url = await self.client.get_media_url(media_id)
            image_bytes = await self.client.download_media(media_url)
        except Exception as e:
            logger.error("Failed to download image %s: %s", media_id, e)
            self.client.send_text(sender, "❌ Could not download image. Please try again.")
            return

        # Send processing message
        self.client.send_text(sender, "📸 Processing receipt... please wait.")

        # Process receipt with OCR
        try:
            expense = await self.ocr_service.process_receipt(
                user_id=user_id,
                image_bytes=image_bytes,
                channel="whatsapp",
            )
            record_ocr_use(user_id)

            # Send confirmation
            await self._send_confirmation(sender, expense, is_photo=True)

        except Exception as e:
            logger.error(f"OCR processing failed: {e}", exc_info=True)
            self.client.send_text(
                sender,
                "❌ Could not read this receipt.\n\n"
                "📸 *Tips for better results:*\n"
                "• Use a clear, well-lit photo\n"
                "• Make sure the amount is visible\n\n"
                "Or type the expense manually:\n"
                "• `Expense: ₦5,000 for transport`",
            )

    async def _send_confirmation(
        self,
        sender: str,
        expense: "models.Invoice",
        is_photo: bool = False,
    ) -> None:
        """Send expense confirmation to user (expense stored as an Invoice)."""

        # Expense date is stored as the invoice due_date; fall back to created_at.
        when = expense.due_date or expense.created_at
        date_str = when.strftime("%b %d, %Y") if when else "Today"

        # Format category
        category_display = (expense.category or "other").replace("_", " ").title()

        # Description lives on the first line item.
        description = None
        try:
            if expense.lines:
                description = expense.lines[0].description
        except Exception:  # noqa: BLE001
            description = None

        # Resolve user's preferred display currency
        currency = get_user_currency(self.db, expense.issuer_id)

        # Build message
        icon = "📸" if is_photo else "✅"
        message = (
            f"{icon} Expense added!\n\n"
            f"💰 Amount: {fmt_money(expense.amount, currency)}\n"
            f"📅 Date: {date_str}\n"
            f"📂 Category: {category_display}\n"
        )

        if description:
            message += f"📝 Description: {description}\n"

        if expense.merchant:
            message += f"🏪 Merchant: {expense.merchant}\n"

        if is_photo and not expense.verified:
            message += "\n⚠️ Please review OCR results for accuracy"

        self.client.send_text(sender, message)
