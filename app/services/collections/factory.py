"""Select the active collection provider from configuration."""

from __future__ import annotations

from app.core.config import settings

from .base import CollectionProvider


def _provider_by_name(name: str | None) -> CollectionProvider:
    key = (name or "paystack").strip().lower()
    if key == "flutterwave":
        from .flutterwave import FlutterwaveCollectionProvider

        return FlutterwaveCollectionProvider()
    from .paystack import PaystackCollectionProvider

    return PaystackCollectionProvider()


def get_collection_provider() -> CollectionProvider:
    """Return the configured escrow collection provider (default: Paystack)."""
    return _provider_by_name(settings.ESCROW_COLLECTOR_PROVIDER)


def get_collection_provider_named(name: str | None) -> CollectionProvider:
    """Return a specific collection provider by name — used for refunds, which
    must follow the provider that COLLECTED the order (recorded per order)."""
    return _provider_by_name(name)
