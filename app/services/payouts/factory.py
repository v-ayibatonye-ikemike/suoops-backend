"""Select the active payout provider from configuration."""

from __future__ import annotations

from app.core.config import settings

from .base import PayoutProvider


def _provider_by_name(name: str | None) -> PayoutProvider:
    key = (name or "paystack").strip().lower()
    if key == "flutterwave":
        from .flutterwave import FlutterwavePayoutProvider

        return FlutterwavePayoutProvider()
    # Default / fallback — Paystack.
    from .paystack import PaystackPayoutProvider

    return PaystackPayoutProvider()


def get_payout_provider() -> PayoutProvider:
    """Return the configured payout provider (default: Paystack)."""
    return _provider_by_name(settings.ESCROW_PAYOUT_PROVIDER)


def get_payout_provider_named(name: str | None) -> PayoutProvider:
    """Return a specific payout provider by name — used to release escrow through
    the SAME rail that collected the order, so the funds are actually there."""
    return _provider_by_name(name)
