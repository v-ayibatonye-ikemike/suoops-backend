from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.services.invoice_components import (
    InventoryIntegrationMixin,
    InvoiceCreationMixin,
    InvoiceQueryMixin,
    InvoiceQuotaMixin,
    InvoiceStatusMixin,
)

if TYPE_CHECKING:
    from app.services.cache_service import InvoiceCacheRepository
    from app.services.pdf_service import PDFService

logger = logging.getLogger(__name__)


class InvoiceService(
    InvoiceQuotaMixin,
    InvoiceCreationMixin,
    InvoiceQueryMixin,
    InvoiceStatusMixin,
    InventoryIntegrationMixin,
):
    """Facade that wires mixins with shared dependencies."""

    def __init__(self, db: Session, pdf_service: PDFService, cache: InvoiceCacheRepository | None = None):
        """Core invoice workflow with optional caching layer.

        Args:
            db: Database session
            pdf_service: PDF generation service
            cache: Optional invoice cache repository (follows Dependency Inversion Principle)
        """
        self.db = db
        self.pdf_service = pdf_service
        self.cache = cache

    # Dependency factory remains below


def build_invoice_service(db: Session, user_id: int | None = None) -> InvoiceService:
    """Factory function to construct InvoiceService with dependencies.

    Simple bank transfer model - no payment platform integration needed.

    Args:
        db: Database session
        user_id: Business owner's user ID (optional, not used in simple model)

    Returns:
        InvoiceService configured with PDF generation and optional caching
    """
    from app.core.config import settings
    from app.db.redis_client import get_redis_client
    from app.services.cache_service import InvoiceCacheRepository
    from app.services.pdf_service import PDFService
    from app.storage.s3_client import S3Client

    pdf = PDFService(S3Client())

    # Add cache if Redis is configured
    cache = None
    if getattr(settings, "REDIS_URL", None):
        try:
            redis_client = get_redis_client()
            cache = InvoiceCacheRepository(redis_client)
        except Exception as e:
            logger.warning("Failed to initialize invoice cache: %s", e)

    return InvoiceService(db, pdf, cache=cache)
