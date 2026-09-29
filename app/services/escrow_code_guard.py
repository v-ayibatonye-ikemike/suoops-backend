"""Anti-brute-force guard for the buyer delivery code.

The 6-digit ``confirmation_code`` authenticates the buyer to release held funds
early and to open the order chat. A per-IP rate limit alone is bypassable by
rotating IPs, so we ALSO cap total FAILED code attempts per store (seller) in a
rolling window — IP-independent — which makes distributed guessing infeasible.

Redis-backed and fail-open: if Redis is unreachable the per-IP limiter still
applies, so a cache blip can never lock out a store or crash a request.
"""

from __future__ import annotations

import logging

from app.core.config import settings

logger = logging.getLogger(__name__)


def _key(seller_id: int) -> str:
    return f"escrow:codefail:{seller_id}"


def is_code_locked(seller_id: int) -> bool:
    """True if this store has exceeded the failed-code budget for the window."""
    try:
        from app.db.redis_client import get_redis_client

        raw = get_redis_client().get(_key(seller_id))
        return raw is not None and int(raw) >= settings.ESCROW_CODE_MAX_FAILURES
    except Exception:  # noqa: BLE001 — fail open; the per-IP limiter still applies
        return False


def register_code_failure(seller_id: int) -> None:
    """Count one failed (invalid) code attempt against this store."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        k = _key(seller_id)
        n = r.incr(k)
        if n == 1:
            r.expire(k, settings.ESCROW_CODE_FAILURE_WINDOW_SECONDS)
        if n == settings.ESCROW_CODE_MAX_FAILURES:
            logger.warning(
                "Delivery-code brute-force suspected on store seller_id=%s "
                "(%s failed attempts in window) — code entry locked.",
                seller_id,
                n,
            )
    except Exception:  # noqa: BLE001 — never let counting break the request
        logger.debug("Skipped code-failure count (redis unavailable)")


def clear_code_failures(seller_id: int) -> None:
    """Reset the counter after a successful (valid) code — legit buyer activity."""
    try:
        from app.db.redis_client import get_redis_client

        get_redis_client().delete(_key(seller_id))
    except Exception:  # noqa: BLE001
        pass
