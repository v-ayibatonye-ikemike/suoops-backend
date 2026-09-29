import json
import logging
from typing import Any, Awaitable, Callable

try:  # pragma: no cover
    from prometheus_client import Counter

    _PROM_CACHE_HITS = Counter("suoops_cache_hits_native", "Cache hits (native instrumented)")
    _PROM_CACHE_MISSES = Counter("suoops_cache_misses_native", "Cache misses (native instrumented)")
except Exception:  # noqa: BLE001
    _PROM_CACHE_HITS = None
    _PROM_CACHE_MISSES = None

import redis

logger = logging.getLogger(__name__)
_cache_metrics = {"hits": 0, "misses": 0}

_redis: redis.Redis | None = None


def _get_client() -> redis.Redis | None:
    global _redis
    if _redis is not None:
        return _redis
    try:
        from app.db.redis_client import get_redis_client

        _redis = get_redis_client()
        return _redis
    except Exception:  # noqa: BLE001
        logger.warning("Cache disabled (Redis unavailable)")
        return None


def cache_get(key: str) -> Any | None:
    client = _get_client()
    if not client:
        return None
    raw = client.get(key)
    if raw is None:
        _cache_metrics["misses"] += 1
        if _PROM_CACHE_MISSES:
            _PROM_CACHE_MISSES.inc()
        return None
    try:
        _cache_metrics["hits"] += 1
        if _PROM_CACHE_HITS:
            _PROM_CACHE_HITS.inc()
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def cache_set(key: str, value: Any, ttl: int = 60) -> None:
    client = _get_client()
    if not client:
        return
    try:
        client.setex(key, ttl, json.dumps(value))
    except Exception:  # noqa: BLE001
        logger.debug("Failed to set cache key=%s", key)


async def cached(key: str, ttl: int, producer: Callable[[], Awaitable[Any]]):
    data = cache_get(key)
    if data is not None:
        return data
    value = await producer()
    cache_set(key, value, ttl)
    return value


def cache_stats() -> dict[str, int]:
    """Return basic cache hit/miss counters for instrumentation."""
    return dict(_cache_metrics)
