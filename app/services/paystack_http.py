"""Shared Paystack HTTP clients.

When ``settings.PAYSTACK_PROXY`` is set, ALL Paystack API traffic egresses
through that static-IP forward proxy so it originates from the IP allow-listed
on the Paystack secret key (Render's shared IPs are dynamic and can't be
whitelisted). Unset → direct connection (unchanged behaviour).

This mirrors the Flutterwave transfer-proxy approach, but is applied to every
Paystack call because Paystack's "Allowed IP addresses" restriction gates the
whole API, not just transfers.
"""

from __future__ import annotations

import httpx

from app.core.config import settings


def paystack_client(timeout: float = 20.0) -> httpx.Client:
    """Sync httpx client for Paystack, routed through PAYSTACK_PROXY when set."""
    proxy = settings.PAYSTACK_PROXY
    if proxy:
        return httpx.Client(timeout=timeout, proxy=proxy)
    return httpx.Client(timeout=timeout)


def paystack_async_client(timeout: float = 15.0) -> httpx.AsyncClient:
    """Async httpx client for Paystack, routed through PAYSTACK_PROXY when set."""
    proxy = settings.PAYSTACK_PROXY
    if proxy:
        return httpx.AsyncClient(timeout=timeout, proxy=proxy)
    return httpx.AsyncClient(timeout=timeout)


def paystack_requests_proxies() -> dict[str, str] | None:
    """Proxy mapping for the stdlib-``requests`` Paystack call site (worker).

    Returns ``None`` when no proxy is configured so callers can pass it straight
    through to ``requests`` (``proxies=None`` means "no proxy").
    """
    proxy = settings.PAYSTACK_PROXY
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}
