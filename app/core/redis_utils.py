"""Utility helpers for consistent Redis TLS configuration."""

from __future__ import annotations

import ssl
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import certifi

from app.core.config import settings

_BASE_DIR = Path(__file__).resolve().parents[1]
_DEFAULT_REDIS_CA = _BASE_DIR / "certs" / "redis-ca.pem"
_ALLOWED_CERT_REQS = {"none", "optional", "required"}


def _add_query_param(url: str, key: str, value: str | None) -> str:
    parsed = urlparse(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if value is None:
        query.pop(key, None)
    else:
        query[key] = [value]
    new_query = urlencode(query, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def get_ca_cert_path() -> str:
    """Return CA bundle path, defaulting to the bundled managed-Redis cert."""
    if settings.REDIS_SSL_CA_CERTS:
        return settings.REDIS_SSL_CA_CERTS
    if _DEFAULT_REDIS_CA.exists():
        return str(_DEFAULT_REDIS_CA)
    return certifi.where()


def normalize_cert_reqs(value: str | None = None) -> str:
    """Normalize ssl_cert_reqs string while preserving host requirements.

    Some managed Redis instances require ``ssl_cert_reqs=none`` to finish the
    TLS handshake. Enforce stronger verification by setting
    ``REDIS_SSL_CERT_REQS`` explicitly in the environment.
    """
    candidate = (value or settings.REDIS_SSL_CERT_REQS or "none").lower()
    if candidate not in _ALLOWED_CERT_REQS:
        candidate = "none"
    return candidate


def map_cert_reqs(value: str | None = None) -> int:
    """Map ssl_cert_reqs string to ssl module constant."""
    normalized = normalize_cert_reqs(value)
    mapping = {
        "none": ssl.CERT_NONE,
        "optional": ssl.CERT_OPTIONAL,
        "required": ssl.CERT_REQUIRED,
    }
    return mapping[normalized]


def prepare_redis_url(url: str | None, add_ssl_params: bool = True) -> str | None:
    """Append TLS query params to Redis URL when using rediss.

    Args:
        url: The Redis URL
        add_ssl_params: If True, add ssl_cert_reqs to URL. Set False for Celery
                       which uses broker_use_ssl config instead.
    """
    if not url:
        return url
    if url.startswith("rediss://") and add_ssl_params:
        # Only add ssl_cert_reqs, not ssl_ca_certs (which can cause issues)
        url = _add_query_param(url, "ssl_cert_reqs", normalize_cert_reqs())
    return url


def get_ssl_context() -> ssl.SSLContext | None:
    """Return a properly configured SSL context for managed Redis (rediss://)."""
    url = settings.REDIS_URL
    if not url or not url.startswith("rediss://"):
        return None

    # Create SSL context
    context = ssl.create_default_context()
    cert_reqs = map_cert_reqs()
    context.check_hostname = cert_reqs != ssl.CERT_NONE
    context.verify_mode = cert_reqs

    if cert_reqs != ssl.CERT_NONE:
        ca_certs = get_ca_cert_path()
        context.load_verify_locations(ca_certs)

    return context


def get_ssl_options() -> dict[str, Any] | None:
    """Return ssl options dict for redis/Celery when using TLS.

    For managed Redis (rediss://) with Python 3.12+, we need
    ssl_cert_reqs=CERT_NONE when the provider uses self-signed certs.
    """
    url = settings.REDIS_URL
    if not url or not url.startswith("rediss://"):
        return None

    # Create a minimal SSL context that doesn't verify certificates
    # This is required for managed Redis providers that use self-signed certs
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE

    # Kombu/Celery expects an ssl_context or specific keys
    return {
        "ssl_cert_reqs": ssl.CERT_NONE,
        "ssl_context": context,
    }
