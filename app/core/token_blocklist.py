"""Redis-backed refresh-token blocklist for server-side revocation on logout.

Tokens are stored by their SHA-256 hash with a TTL matching the token's
remaining lifetime, so the blocklist is self-cleaning.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone

import redis

logger = logging.getLogger(__name__)

_BLOCKLIST_PREFIX = "revoked:"


def _token_hash(token: str) -> str:
    """Return a compact SHA-256 hex digest of the raw JWT."""
    return hashlib.sha256(token.encode()).hexdigest()


def revoke_token(redis_client: redis.Redis, token: str, expires_at: datetime | None = None) -> None:
    """Add a token to the blocklist.

    Args:
        redis_client: Active Redis connection.
        token: The raw JWT string to revoke.
        expires_at: When the token expires (used to set TTL).  If ``None``,
                    a conservative 14-day TTL is used.
    """
    key = f"{_BLOCKLIST_PREFIX}{_token_hash(token)}"
    if expires_at:
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds()
        ttl = max(int(remaining), 1)  # at least 1 second
    else:
        ttl = 14 * 24 * 3600  # 14 days fallback (matches refresh token lifetime)

    try:
        redis_client.setex(key, ttl, "1")
        logger.info("Token revoked (TTL=%ds)", ttl)
    except Exception:  # noqa: BLE001
        logger.warning("Failed to revoke token in Redis — token may remain valid until expiry")


def is_token_revoked(redis_client: redis.Redis, token: str) -> bool:
    """Check whether a token has been revoked."""
    key = f"{_BLOCKLIST_PREFIX}{_token_hash(token)}"
    try:
        return redis_client.exists(key) > 0
    except Exception:  # noqa: BLE001
        # If Redis is unreachable, fail-open to avoid locking out all users.
        # The short access-token lifetime limits the blast radius.
        logger.warning("Redis unavailable for blocklist check — allowing token")
        return False
