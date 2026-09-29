"""Rolling 24h cumulative refund guard for admins.

Per-transaction step-up (``ESCROW_ADMIN_STEPUP_NAIRA``) stops a single large
refund, but a hijacked admin session could still script many *small* refunds,
each below the threshold, to drain funds. This module tracks the running total
each admin has refunded in a rolling 24h window (Redis, IP-independent) so that
once the cumulative total crosses the same threshold, every further refund
requires a fresh step-up OTP.

Redis-backed and fail-open: if Redis is unreachable the per-transaction step-up
and rate limiter still apply, so a cache blip can never wrongly block a refund.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_WINDOW_SECONDS = 24 * 60 * 60  # rolling 24h


def _key(admin_id: int) -> str:
    return f"admin:refundtotal:{admin_id}"


def refund_total_24h(admin_id: int) -> float:
    """Naira an admin has refunded in the current rolling 24h window."""
    try:
        from app.db.redis_client import get_redis_client

        raw = get_redis_client().get(_key(admin_id))
        return float(raw) if raw is not None else 0.0
    except Exception:  # noqa: BLE001 — fail open; step-up threshold still applies
        return 0.0


def record_refund(admin_id: int, amount_naira: float) -> None:
    """Add a completed refund to this admin's rolling 24h total."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        k = _key(admin_id)
        new_total = r.incrbyfloat(k, max(0.0, float(amount_naira)))
        # (Re)set the TTL so the window rolls forward with activity.
        r.expire(k, _WINDOW_SECONDS)
        logger.info("Admin %s cumulative 24h refunds now ₦%.0f", admin_id, float(new_total))
    except Exception:  # noqa: BLE001 — never let counting break the refund
        logger.debug("Skipped admin refund-total count (redis unavailable)")
