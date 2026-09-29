"""Cache service for invoice data using Redis.

This module implements the Repository pattern for caching invoice data.
Follows Dependency Inversion Principle by depending on the BaseKeyValueStore
protocol rather than concrete Redis implementation.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from app.models import models

logger = logging.getLogger(__name__)


class BaseKeyValueStore(Protocol):
    """Protocol defining key-value storage interface (Dependency Inversion Principle)."""

    def get(self, key: str) -> str | None:
        """Retrieve value by key."""
        ...

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        """Store value with optional expiration in seconds."""
        ...

    def delete(self, key: str) -> None:
        """Remove key from store."""
        ...


class InvoiceCacheRepository:
    """Repository for caching invoice data (Repository Pattern + Single Responsibility).

    This class follows SOLID principles:
    - Single Responsibility: Only handles invoice caching
    - Open/Closed: Extensible via BaseKeyValueStore protocol
    - Liskov Substitution: Any BaseKeyValueStore implementation works
    - Interface Segregation: Minimal protocol interface
    - Dependency Inversion: Depends on protocol, not concrete implementation
    """

    INVOICE_KEY_PREFIX = "invoice:"
    INVOICE_LIST_KEY_PREFIX = "invoices:user:"
    DEFAULT_TTL = 3600  # 1 hour (invoices don't change frequently)
    LIST_TTL = 300  # 5 minutes (lists may grow)

    def __init__(self, store: BaseKeyValueStore):
        """Initialize repository with key-value store.

        Args:
            store: Any object implementing BaseKeyValueStore protocol
        """
        self.store = store

    def _invoice_key(self, invoice_id: str) -> str:
        """Generate cache key for single invoice."""
        return f"{self.INVOICE_KEY_PREFIX}{invoice_id}"

    def _invoice_list_key(self, user_id: int) -> str:
        """Generate cache key for user's invoice list."""
        return f"{self.INVOICE_LIST_KEY_PREFIX}{user_id}"

    def _serialize_invoice(self, invoice: models.Invoice) -> str:
        """Serialize invoice model to JSON string.

        Args:
            invoice: Invoice SQLAlchemy model

        Returns:
            JSON string representation
        """
        # Extract customer info from relationship if available
        customer_name = None
        customer_email = None
        customer_phone = None

        if hasattr(invoice, "customer") and invoice.customer:
            customer_name = invoice.customer.name
            customer_email = invoice.customer.email
            customer_phone = invoice.customer.phone

        data = {
            "id": invoice.id,
            "invoice_id": invoice.invoice_id,
            "issuer_id": invoice.issuer_id,
            "customer_id": invoice.customer_id,
            "customer_name": customer_name,
            "customer_email": customer_email,
            "customer_phone": customer_phone,
            "amount": str(invoice.amount),
            "status": invoice.status,
            "due_date": invoice.due_date.isoformat() if invoice.due_date else None,
            "created_at": invoice.created_at.isoformat(),
            "paid_at": invoice.paid_at.isoformat() if invoice.paid_at else None,
            "pdf_url": invoice.pdf_url,
            "receipt_pdf_url": invoice.receipt_pdf_url,
            "invoice_type": invoice.invoice_type,
            "category": invoice.category,
            "vendor_name": invoice.vendor_name,
            "channel": invoice.channel,
            "notes": invoice.notes,
        }
        return json.dumps(data)

    def _deserialize_invoice(self, data: str) -> dict[str, Any]:
        """Deserialize JSON string to invoice dict.

        Args:
            data: JSON string

        Returns:
            Dictionary representation of invoice
        """
        return json.loads(data)

    def get_invoice(self, invoice_id: str) -> dict[str, Any] | None:
        """Retrieve invoice from cache.

        Args:
            invoice_id: Unique invoice identifier

        Returns:
            Invoice dict if found, None otherwise
        """
        try:
            key = self._invoice_key(invoice_id)
            cached = self.store.get(key)

            if cached:
                logger.debug("Cache HIT for invoice %s", invoice_id)
                return self._deserialize_invoice(cached)

            logger.debug("Cache MISS for invoice %s", invoice_id)
            return None

        except Exception as e:
            # Redis is optional - log as warning not error to avoid Sentry noise
            logger.warning("Cache read error for invoice %s: %s", invoice_id, e)
            return None

    def set_invoice(self, invoice: models.Invoice, ttl: int | None = None) -> None:
        """Store invoice in cache.

        Args:
            invoice: Invoice model to cache
            ttl: Time-to-live in seconds (defaults to DEFAULT_TTL)
        """
        try:
            key = self._invoice_key(invoice.id)
            value = self._serialize_invoice(invoice)
            expiry = ttl or self.DEFAULT_TTL

            self.store.set(key, value, ex=expiry)
            logger.debug("Cached invoice %s (TTL: %ss)", invoice.id, expiry)

        except Exception as e:
            # Redis is optional - log as warning not error to avoid Sentry noise
            logger.warning("Cache write error for invoice %s: %s", invoice.id, e)

    def invalidate_invoice(self, invoice_id: str) -> None:
        """Remove invoice from cache.

        Args:
            invoice_id: Unique invoice identifier
        """
        try:
            key = self._invoice_key(invoice_id)
            self.store.delete(key)
            logger.debug("Invalidated cache for invoice %s", invoice_id)

        except Exception as e:
            # Redis is optional - log as warning not error to avoid Sentry noise
            logger.warning("Cache invalidation error for invoice %s: %s", invoice_id, e)

    def invalidate_user_invoices(self, user_id: int) -> None:
        """Remove user's invoice list from cache.

        Args:
            user_id: User ID
        """
        try:
            key = self._invoice_list_key(user_id)
            self.store.delete(key)
            logger.debug("Invalidated invoice list cache for user %s", user_id)

        except Exception as e:
            # Redis is optional - log as warning not error to avoid Sentry noise
            logger.warning("Cache list invalidation error for user %s: %s", user_id, e)

    def get_invoice_list(self, user_id: int) -> list[dict[str, Any]] | None:
        """Retrieve user's invoice list from cache.

        Args:
            user_id: User ID

        Returns:
            List of invoice dicts if found, None otherwise
        """
        try:
            key = self._invoice_list_key(user_id)
            cached = self.store.get(key)

            if cached:
                logger.debug("Cache HIT for user %s invoice list", user_id)
                return json.loads(cached)

            logger.debug("Cache MISS for user %s invoice list", user_id)
            return None

        except Exception as e:
            # Redis is optional - log as warning not error to avoid Sentry noise
            logger.warning("Cache read error for user %s invoice list: %s", user_id, e)
            return None

    def set_invoice_list(self, user_id: int, invoices: list[models.Invoice], ttl: int | None = None) -> None:
        """Store user's invoice list in cache.

        Args:
            user_id: User ID
            invoices: List of invoice models
            ttl: Time-to-live in seconds (defaults to LIST_TTL)
        """
        try:
            key = self._invoice_list_key(user_id)
            data = [self._deserialize_invoice(self._serialize_invoice(inv)) for inv in invoices]
            value = json.dumps(data)
            expiry = ttl or self.LIST_TTL

            self.store.set(key, value, ex=expiry)
            logger.debug("Cached %s invoices for user %s (TTL: %ss)", len(invoices), user_id, expiry)

        except Exception as e:
            # Redis is optional - log as warning not error to avoid Sentry noise
            logger.warning("Cache write error for user %s invoice list: %s", user_id, e)
