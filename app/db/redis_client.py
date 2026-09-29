"""
Centralized Redis client manager with connection pooling.
Prevents connection limit issues by reusing a single connection pool.
"""

import logging

import redis
from redis.connection import ConnectionPool

from app.core.config import settings
from app.core.redis_utils import prepare_redis_url

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None
_client: redis.Redis | None = None


def get_redis_pool() -> ConnectionPool:
    """Get or create a shared Redis connection pool."""
    global _pool
    if _pool is not None:
        return _pool

    redis_url = prepare_redis_url(settings.REDIS_URL)
    if not redis_url:
        raise RuntimeError("REDIS_URL is not configured")

    # Parse connection parameters
    pool_kwargs = {
        "max_connections": 10,  # Shared across rate limiter, app cache; Celery has its own pool
        "socket_timeout": 5,
        "socket_connect_timeout": 5,
        "retry_on_timeout": True,
        "health_check_interval": 30,
        "decode_responses": True,  # Return strings instead of bytes
    }

    # Note: prepare_redis_url already adds ssl_cert_reqs and ssl_ca_certs as query params
    # No need to add them again in pool_kwargs - this can cause conflicts

    _pool = ConnectionPool.from_url(redis_url, **pool_kwargs)
    logger.info("Redis connection pool created (max_connections=10)")
    return _pool


def get_redis_client() -> redis.Redis:
    """Get or create a Redis client using the shared connection pool."""
    global _client
    if _client is not None:
        return _client

    pool = get_redis_pool()
    _client = redis.Redis(connection_pool=pool)

    # Test connection
    try:
        _client.ping()
        logger.info("Redis client connected successfully")
    except Exception as e:
        logger.error("Redis connection failed: %s", e)
        raise

    return _client


def close_redis_pool():
    """Close the Redis connection pool. Called on app shutdown."""
    global _pool, _client
    if _client is not None:
        _client.close()
        _client = None
    if _pool is not None:
        _pool.disconnect()
        _pool = None
    logger.info("Redis pool closed")
