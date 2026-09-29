"""
Expense OCR service for processing receipt photos.

Uses existing OCR infrastructure to extract expense details from receipt images.
Auto-categorizes based on merchant/description and stores receipt evidence.
"""

import logging
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import TypedDict

from sqlalchemy.orm import Session

from app.models import models
from app.services.expense_nlp_service import ExpenseNLPService
from app.services.expense_service import record_expense_invoice
from app.services.ocr_service import OCRService
from app.storage.s3_client import S3Client

logger = logging.getLogger(__name__)


class ReceiptData(TypedDict):
    """Parsed receipt information"""

    amount: Decimal
    date: date | None
    category: str
    description: str
    merchant: str | None
    raw_text: str
    confidence: str


class ExpenseOCRService:
    """Process receipt photos to create expense records"""

    def __init__(self, db: Session):
        self.db = db
        self.ocr_service = OCRService()
        self.nlp_service = ExpenseNLPService()
        self.s3_client = S3Client()

    async def process_receipt(
        self,
        user_id: int,
        image_bytes: bytes,
        channel: str = "whatsapp",
    ) -> "models.Invoice":
        """
        Process receipt photo and create an expense invoice.

        Steps:
        1. Upload receipt image to S3
        2. OCR extraction
        3. Parse and categorize
        4. Record the expense (as a unified expense-invoice)

        Args:
            user_id: User ID
            image_bytes: Receipt image bytes
            channel: Input channel (whatsapp, email)

        Returns:
            Created expense Invoice record
        """
        # 1. Upload receipt to S3
        receipt_url = await self._upload_receipt(user_id, image_bytes)

        # 2. OCR extraction
        ocr_result = await self.ocr_service.parse_receipt(image_bytes, context="business expense receipt")

        if not ocr_result.get("success"):
            # Clean up S3 file since we won't create a record
            try:
                await self.s3_client.delete_file(receipt_url)
            except Exception:
                logger.warning("Failed to clean up S3 file %s after OCR failure", receipt_url)
            logger.error("OCR failed for user %s: %s", user_id, ocr_result.get("error"))
            raise ValueError(f"Could not read receipt: {ocr_result.get('error', 'Unknown error')}")

        # 3. Parse receipt data
        receipt_data = self._parse_ocr_result(ocr_result)

        # 4. Record the expense as a unified expense-invoice.
        try:
            invoice = record_expense_invoice(
                self.db,
                user_id=user_id,
                amount=receipt_data["amount"],
                category=receipt_data["category"],
                description=receipt_data["description"],
                merchant=receipt_data["merchant"],
                expense_date=receipt_data["date"] or date.today(),
                input_method="photo",
                channel=channel,
                receipt_url=receipt_url,
                receipt_text=receipt_data["raw_text"],
                notes=f"OCR confidence: {receipt_data['confidence']}",
            )
        except Exception:
            self.db.rollback()
            # Clean up orphaned S3 file
            try:
                await self.s3_client.delete_file(receipt_url)
            except Exception:
                logger.warning("Failed to clean up S3 file %s after DB failure", receipt_url)
            raise

        logger.info(
            "Created expense from receipt for user %s: ₦%s, category=%s",
            user_id,
            invoice.amount,
            invoice.category,
        )

        return invoice

    async def _upload_receipt(self, user_id: int, image_bytes: bytes) -> str:
        """
        Upload receipt image to S3.

        Returns:
            S3 URL of uploaded receipt
        """
        # Generate filename with timestamp
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"receipts/user_{user_id}/{timestamp}_receipt.jpg"

        # Upload to S3
        receipt_url = await self.s3_client.upload_file(
            data=image_bytes,
            key=filename,
            content_type="image/jpeg",
        )

        return receipt_url

    def _parse_ocr_result(self, ocr_result: dict) -> ReceiptData:
        """
        Parse OCR result into structured receipt data.

        Args:
            ocr_result: Result from OCR service

        Returns:
            Structured receipt data
        """
        # Extract basic info from OCR
        amount_str = ocr_result.get("amount", "0")
        try:
            amount = Decimal(amount_str)
        except (InvalidOperation, ValueError):
            amount = Decimal("0")

        merchant = ocr_result.get("business_name") or None
        raw_text = ocr_result.get("raw_text", "")
        confidence = ocr_result.get("confidence", "medium")

        # Try to parse date from OCR result
        date_str = ocr_result.get("date")
        expense_date = None
        if date_str:
            try:
                expense_date = datetime.fromisoformat(date_str).date()
            except (ValueError, AttributeError):
                pass

        # Build description from items or raw text
        items = ocr_result.get("items", [])
        if items:
            # Use item descriptions
            descriptions = [item.get("description", "") for item in items if item.get("description")]
            description = ", ".join(descriptions[:3])  # Top 3 items
        else:
            # Use NLP to extract description from raw text
            description = self.nlp_service._clean_description(raw_text)

        # Categorize based on merchant or description
        category_text = f"{merchant or ''} {description} {raw_text}".lower()
        category = self.nlp_service._categorize(category_text)

        return ReceiptData(
            amount=amount,
            date=expense_date,
            category=category,
            description=description[:500] if description else "Receipt expense",
            merchant=merchant[:200] if merchant else None,
            raw_text=raw_text,
            confidence=confidence,
        )

    async def reprocess_receipt(
        self,
        expense_id: int,
        user_id: int,
    ) -> "models.Invoice":
        """
        Reprocess an existing receipt (e.g., after OCR improvements).

        Args:
            expense_id: Expense-invoice ID
            user_id: User ID (for verification)

        Returns:
            The expense Invoice record
        """
        expense = (
            self.db.query(models.Invoice)
            .filter(
                models.Invoice.id == expense_id,
                models.Invoice.issuer_id == user_id,
                models.Invoice.invoice_type == "expense",
            )
            .first()
        )

        if not expense:
            raise ValueError("Expense not found")

        if not expense.receipt_url:
            raise ValueError("No receipt image to reprocess")

        # Download receipt from S3
        # (Would need S3Client.download_file method)
        # For now, just log
        logger.info("Would reprocess receipt for expense %s", expense_id)

        return expense
