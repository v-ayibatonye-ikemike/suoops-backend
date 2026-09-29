import logging
import threading
import time

import redis
from limits.storage import MemoryStorage, RedisStorage
from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

try:  # pragma: no cover
    from prometheus_client import Counter

    _PROM_RATE_LIMIT = Counter("suoops_rate_limit_exceeded_events", "Rate limit exceeded events (handler invocations)")
except Exception:  # noqa: BLE001
    _PROM_RATE_LIMIT = None
from app.api.rate_limit_strategies import get_plan_from_token
from app.core.config import settings

logger = logging.getLogger(__name__)


def get_user_identifier(request: Request) -> str:
    """Get unique identifier for rate limiting that includes user plan.

    Uses JWT token to extract plan for dynamic rate limiting (Strategy pattern).
    Falls back to IP address for unauthenticated requests.

    Returns:
        Identifier in format: 'ip:plan' or just 'ip' for unauthenticated

    Example:
        >>> get_user_identifier(request)
        '192.168.1.1:pro'  # Authenticated PRO user
        '10.0.0.1:free'    # Unauthenticated (treated as free)
    """
    ip_address = get_remote_address(request)

    # Extract Bearer token from Authorization header
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]  # Remove 'Bearer ' prefix

    # Get user's plan from token (defaults to 'free' if invalid/missing)
    plan = get_plan_from_token(token)

    # Include plan in identifier for per-plan rate limiting
    return f"{ip_address}:{plan}"


def _should_use_redis() -> bool:
    """Whether Redis should back the rate limiter (production with a Redis URL).

    Accept both redis:// and rediss:// — Render's managed Redis is reached over the
    private network as redis://, same as the app's main Redis pool.
    """
    if settings.ENV.lower() != "prod":
        return False
    redis_url = settings.REDIS_URL
    return bool(redis_url) and redis_url.startswith(("redis://", "rediss://"))


class ResilientStorage(MemoryStorage):
    """
    Rate-limiter storage that prefers Redis but falls back to in-memory on failure.

    Unlike a fixed Redis or memory storage, this:
      • Lazily initialises the underlying Redis storage on first use (so a Redis
        blip at gunicorn boot does not permanently degrade rate limiting).
      • Retries Redis initialisation periodically (every ``_retry_interval``
        seconds) when currently in fallback mode.
      • Catches Redis exceptions on every operation and transparently falls back
        to the in-memory parent class for that call.
      • Logs (rate-limited) when fallback is in effect so operators can see it.

    Inherits from MemoryStorage so any method we don't explicitly override
    automatically uses the in-memory implementation.
    """

    _retry_interval: float = 30.0  # seconds between Redis init retry attempts
    _log_throttle_interval: float = 60.0  # seconds between fallback log lines

    def __init__(self) -> None:
        super().__init__()
        self._redis_storage: RedisStorage | None = None
        self._redis_init_lock = threading.Lock()
        self._last_redis_attempt: float = 0.0
        self._last_fallback_log: float = 0.0

    # ── Redis lifecycle ──────────────────────────────────────────────
    def _try_init_redis(self) -> RedisStorage | None:
        """Attempt to (re)build the Redis-backed storage. Returns None on failure."""
        if not _should_use_redis():
            return None
        now = time.monotonic()
        if now - self._last_redis_attempt < self._retry_interval:
            return self._redis_storage  # don't hammer Redis if it just failed
        with self._redis_init_lock:
            # Re-check inside the lock
            now = time.monotonic()
            if self._redis_storage is not None:
                return self._redis_storage
            if now - self._last_redis_attempt < self._retry_interval:
                return self._redis_storage
            self._last_redis_attempt = now
            try:
                from app.db.redis_client import get_redis_client

                client = get_redis_client()
                # Storage URI is informational only here; pool drives the connection.
                self._redis_storage = RedisStorage(
                    uri=settings.REDIS_URL or "redis://",
                    connection_pool=client.connection_pool,
                )
                logger.info("Rate limiter Redis storage initialised (lazy)")
                return self._redis_storage
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Rate limiter Redis init failed (%s); using in-memory fallback. " "Will retry in %.0fs.",
                    exc,
                    self._retry_interval,
                )
                self._redis_storage = None
                return None

    def _redis(self) -> RedisStorage | None:
        if self._redis_storage is not None:
            return self._redis_storage
        return self._try_init_redis()

    def _log_fallback(self, op: str, exc: BaseException) -> None:
        now = time.monotonic()
        if now - self._last_fallback_log < self._log_throttle_interval:
            return
        self._last_fallback_log = now
        # ERROR (not just warning) so it surfaces in alerting: in production this
        # means rate limits degrade to per-worker, weakening brute-force defenses.
        logger.error(
            "Rate limiter Redis %s failed (%s); falling back to in-memory — "
            "rate limits are now PER-WORKER until Redis recovers.",
            op,
            exc,
        )
        try:  # best-effort external alert
            import sentry_sdk

            sentry_sdk.capture_message(f"Rate limiter Redis fallback active ({op}): {exc}", level="error")
        except Exception:  # noqa: BLE001 — never let alerting break the request
            pass
        # Drop reference so next call retries init on the schedule.
        self._redis_storage = None

    # ── Storage methods used by FixedWindow / MovingWindow strategies ─
    def incr(self, key, expiry, amount=1):
        r = self._redis()
        if r is not None:
            try:
                return r.incr(key, expiry, amount=amount)
            except Exception as exc:  # noqa: BLE001
                self._log_fallback("incr", exc)
        return super().incr(key, expiry, amount=amount)

    def get(self, key):
        r = self._redis()
        if r is not None:
            try:
                return r.get(key)
            except Exception as exc:  # noqa: BLE001
                self._log_fallback("get", exc)
        return super().get(key)

    def get_expiry(self, key):
        r = self._redis()
        if r is not None:
            try:
                return r.get_expiry(key)
            except Exception as exc:  # noqa: BLE001
                self._log_fallback("get_expiry", exc)
        return super().get_expiry(key)

    def check(self):
        r = self._redis()
        if r is not None:
            try:
                return r.check()
            except Exception as exc:  # noqa: BLE001
                self._log_fallback("check", exc)
        return super().check()

    def reset(self):
        r = self._redis()
        if r is not None:
            try:
                return r.reset()
            except Exception as exc:  # noqa: BLE001
                self._log_fallback("reset", exc)
        return super().reset()

    def clear(self, key):
        r = self._redis()
        if r is not None:
            try:
                return r.clear(key)
            except Exception as exc:  # noqa: BLE001
                self._log_fallback("clear", exc)
        return super().clear(key)


def _build_limiter() -> Limiter:
    """Construct the slowapi Limiter, using Redis-backed resilient storage in prod."""
    if _should_use_redis():
        storage = ResilientStorage()
        # Pre-warm so we surface errors at boot (but don't block — failure just
        # means the first few requests use memory until retry succeeds).
        storage._try_init_redis()
        if storage._redis_storage is None:
            logger.warning("Rate limiter starting with in-memory fallback; Redis will be retried.")
        # slowapi's Limiter only accepts a URI string in __init__; build with
        # memory:// then swap in our resilient storage and rebuild the strategy.
        lim = Limiter(key_func=get_user_identifier, storage_uri="memory://")
        from limits.strategies import STRATEGIES

        lim._storage = storage
        strategy_name = lim._strategy or "fixed-window"
        lim._limiter = STRATEGIES[strategy_name](storage)
        logger.info("Rate limiter initialised with ResilientStorage (Redis + memory fallback)")
        return lim

    if settings.ENV.lower() != "prod":
        logger.info("Rate limiter using in-memory storage (dev/test mode)")
    else:
        logger.warning("Rate limiter using in-memory storage in PRODUCTION — REDIS_URL not configured")
    return Limiter(key_func=get_user_identifier, storage_uri="memory://")


limiter = _build_limiter()


# ── Monkey-patch slowapi to fail-open on Redis errors ─────────────
# When Redis reaches max connections, slowapi crashes the entire request.
# We wrap the check to catch connection errors and let the request through
# rather than killing the service.
_original_check = limiter._check_request_limit


def _resilient_check(request, func, in_middleware):
    try:
        return _original_check(request, func, in_middleware)
    except redis.exceptions.ConnectionError as e:
        logger.warning("Rate limiter Redis unavailable, allowing request: %s", e)
    except Exception as e:
        if "max number of clients" in str(e).lower():
            logger.warning("Rate limiter Redis max clients, allowing request: %s", e)
        else:
            raise


limiter._check_request_limit = _resilient_check

RATE_LIMITS = {
    # Auth flows (same for all users - before authentication)
    "signup_request": "5/minute" if settings.ENV.lower() == "prod" else "50/minute",
    "signup_verify": "10/minute",
    "login_request": "10/minute",
    "login_verify": "10/minute",
    "otp_resend": "10/minute",
    "otp_status": "60/minute",
    "refresh": "20/minute",
    # OAuth
    "oauth_login": "30/minute",
    "oauth_callback": "60/minute",
    # OCR (uses dynamic limits based on plan)
    "ocr_parse": "10/minute",  # FREE fallback
    "ocr_create_invoice": "10/minute",  # FREE fallback
    # Webhooks
    "webhook_whatsapp_verify": "120/minute",
    "webhook_whatsapp_inbound": "300/minute",
    "webhook_paystack": "60/minute",
    "webhook_ses": "300/minute",
}


def get_dynamic_limit(request: Request) -> str:
    """Get dynamic rate limit based on user's subscription plan.

    Follows Strategy pattern - delegates to plan-specific strategies.
    Used for authenticated endpoints where plan affects limits.

    Args:
        request: Starlette request object

    Returns:
        Rate limit string (e.g., '60/minute' for PRO users)

    Example usage in route:
        @limiter.limit(get_dynamic_limit)
        async def create_invoice(...):
            ...
    """
    from app.api.rate_limit_strategies import get_plan_from_token, get_rate_limit_strategy

    # Extract Bearer token
    auth_header = request.headers.get("Authorization", "")
    token = None
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]

    # Get plan and corresponding strategy
    plan = get_plan_from_token(token)
    strategy = get_rate_limit_strategy(plan)

    return strategy.get_limit()


_rate_limit_lock = threading.Lock()
_rate_limit_counters: dict[str, int] = {"exceeded": 0}


def increment_rate_limit_exceeded():
    with _rate_limit_lock:
        _rate_limit_counters["exceeded"] += 1
    if _PROM_RATE_LIMIT:
        _PROM_RATE_LIMIT.inc()


def rate_limit_stats() -> dict[str, int]:
    with _rate_limit_lock:
        return dict(_rate_limit_counters)
