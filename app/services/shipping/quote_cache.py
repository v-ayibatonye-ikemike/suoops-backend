"""Redis cache + per-store daily cap for the public delivery-rate quote endpoint.

The quote endpoint is public and each cache miss makes several *paid* Shipbubble
API calls (address validations + rate fetch). To stop it being used to burn the
Shipbubble quota (or as a free address-validation oracle), we cache identical
quotes for a short window and cap the number of fresh fetches per store per day.

Fail-open: if Redis is unavailable, quotes still work (just uncached) — a cache
blip must never block a real checkout.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)


def _key(slug: str, lat: float | None, lng: float | None, cart_sig: str) -> str:
    # Round coordinates to ~110m so near-identical requests share a cache entry.
    latr = round(lat, 3) if lat is not None else "?"
    lngr = round(lng, 3) if lng is not None else "?"
    raw = f"{slug}|{latr}|{lngr}|{cart_sig}"
    return "dq:" + hashlib.sha1(raw.encode()).hexdigest()


def get_cached(slug: str, lat: float | None, lng: float | None, cart_sig: str) -> dict[str, Any] | None:
    """Return a cached quote response for this store+location+cart, or None."""
    try:
        from app.db.redis_client import get_redis_client

        raw = get_redis_client().get(_key(slug, lat, lng, cart_sig))
        return json.loads(raw) if raw else None
    except Exception:  # noqa: BLE001 — fail open (uncached)
        return None


def set_cached(slug: str, lat: float | None, lng: float | None, cart_sig: str, value: dict[str, Any]) -> None:
    """Cache a quote response briefly (SHIPBUBBLE_QUOTE_CACHE_SECONDS)."""
    try:
        from app.db.redis_client import get_redis_client

        get_redis_client().setex(
            _key(slug, lat, lng, cart_sig),
            settings.SHIPBUBBLE_QUOTE_CACHE_SECONDS,
            json.dumps(value),
        )
    except Exception:  # noqa: BLE001
        pass


def store_quota_ok(slug: str) -> bool:
    """Count one fresh (cache-miss) quote against the store's daily budget.
    Returns False once the store exceeds ``SHIPBUBBLE_QUOTE_DAILY_CAP_PER_STORE``.
    Fail-open — never block a checkout on a Redis blip."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()
        k = f"dq:cap:{slug}"
        n = r.incr(k)
        if n == 1:
            r.expire(k, 86400)
        return int(n) <= settings.SHIPBUBBLE_QUOTE_DAILY_CAP_PER_STORE
    except Exception:  # noqa: BLE001
        return True
