"""
PDF Generation Tasks.

Celery tasks for asynchronous PDF generation for invoices and receipts.
"""

from __future__ import annotations

import logging
from typing import Any

from celery import Task

from app.db.session import session_scope
from app.models.models import Invoice
from app.services.pdf_service import PDFService
from app.storage.s3_client import s3_client
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="pdf.generate_invoice",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def generate_invoice_pdf_async(
    self: Task,
    invoice_id: int,
    bank_details: dict[str, Any] | None = None,
    logo_url: str | None = None,
    user_plan: str = "free",
) -> dict[str, Any]:
    """
    Generate invoice PDF asynchronously and update database.

    Args:
        invoice_id: Primary key of invoice to generate PDF for
        bank_details: Optional bank details dict
        logo_url: Optional logo URL
        user_plan: User's subscription plan

    Returns:
        Dict with pdf_url, invoice_id, and status
    """
    logger.info("Starting async PDF generation for invoice %s (plan: %s)", invoice_id, user_plan)

    try:
        with session_scope() as db:
            from sqlalchemy.orm import joinedload, selectinload

            invoice = (
                db.query(Invoice)
                .options(
                    joinedload(Invoice.customer),
                    joinedload(Invoice.issuer),  # Load issuer for business name
                    joinedload(Invoice.created_by),  # Load creator for PDF
                    selectinload(Invoice.lines),
                )
                .filter(Invoice.id == invoice_id)
                .first()
            )

            if not invoice:
                logger.error("Invoice %s not found in database", invoice_id)
                raise ValueError(f"Invoice {invoice_id} not found")

            pdf_service = PDFService(s3_client)

            pdf_url = pdf_service.generate_invoice_pdf(
                invoice=invoice,
                bank_details=bank_details,
                logo_url=logo_url,
                user_plan=user_plan,
            )

            invoice.pdf_url = pdf_url
            db.commit()

            logger.info("PDF generated successfully for invoice %s: %s", invoice_id, pdf_url)

            return {
                "invoice_id": invoice.invoice_id,
                "pdf_url": pdf_url,
                "status": "success",
            }

    except Exception as e:
        logger.error("PDF generation failed for invoice %s: %s", invoice_id, e)

        if self.request.retries < self.max_retries:
            logger.info(
                "Retrying PDF generation (attempt %s/%s)",
                self.request.retries + 1,
                self.max_retries,
            )
            raise self.retry(exc=e) from e

        logger.error("PDF generation failed after %s retries for invoice %s", self.max_retries, invoice_id)
        raise


@celery_app.task(
    bind=True,
    name="pdf.generate_receipt",
    autoretry_for=(Exception,),
    retry_backoff=30,
    retry_jitter=True,
    retry_kwargs={"max_retries": 3},
)
def generate_receipt_pdf_async(
    self: Task,
    invoice_id: int,
) -> dict[str, Any]:
    """
    Generate payment receipt PDF asynchronously.

    Args:
        invoice_id: Primary key of invoice to generate receipt for

    Returns:
        Dict with receipt_pdf_url, invoice_id, and status
    """
    logger.info("Starting async receipt PDF generation for invoice %s", invoice_id)

    try:
        with session_scope() as db:
            from sqlalchemy.orm import joinedload, selectinload

            invoice = (
                db.query(Invoice)
                .options(
                    joinedload(Invoice.customer),
                    joinedload(Invoice.issuer),  # Load issuer for business name
                    joinedload(Invoice.created_by),
                    joinedload(Invoice.status_updated_by),
                    selectinload(Invoice.lines),
                )
                .filter(Invoice.id == invoice_id)
                .first()
            )

            if not invoice:
                logger.error("Invoice %s not found in database", invoice_id)
                raise ValueError(f"Invoice {invoice_id} not found")

            pdf_service = PDFService(s3_client)
            receipt_url = pdf_service.generate_receipt_pdf(invoice)

            invoice.receipt_pdf_url = receipt_url
            db.commit()

            logger.info("Receipt PDF generated successfully for invoice %s: %s", invoice_id, receipt_url)

            return {
                "invoice_id": invoice.invoice_id,
                "receipt_pdf_url": receipt_url,
                "status": "success",
            }

    except Exception as e:
        logger.error("Receipt generation failed for invoice %s: %s", invoice_id, e)

        if self.request.retries < self.max_retries:
            raise self.retry(exc=e) from e

        raise
