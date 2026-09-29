"""Simple Fernet-based encryption utilities for pilot column encryption.

In production (APP_ENV=prod), ENCRYPTION_KEY is REQUIRED — missing key will
raise an error to prevent storing sensitive data as plaintext.
In dev/test, functions fall back to passthrough (no-op) for convenience.
"""

from __future__ import annotations

import base64
import logging
from functools import lru_cache

try:
    from cryptography.fernet import Fernet, InvalidToken
except Exception:  # pragma: no cover
    Fernet = None  # type: ignore
    InvalidToken = Exception  # type: ignore

logger = logging.getLogger(__name__)


def _is_production() -> bool:
    from app.core.config import settings

    return settings.ENV.lower() in ("prod", "production")


@lru_cache
def _get_cipher() -> Fernet | None:
    from app.core.config import settings

    key = settings.ENCRYPTION_KEY
    if not key or Fernet is None:
        if _is_production():
            logger.error(
                "ENCRYPTION_KEY is not set in PRODUCTION — sensitive data will NOT be encrypted. "
                "Set ENCRYPTION_KEY env var immediately."
            )
        return None
    try:
        # Support raw 32-byte key or urlsafe base64 encoded
        if len(key) == 32:
            key = base64.urlsafe_b64encode(key.encode()).decode()
        return Fernet(key)
    except Exception:  # noqa: BLE001
        logger.error("Invalid ENCRYPTION_KEY; encryption disabled")
        return None


def encrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    cipher = _get_cipher()
    if not cipher:
        if _is_production():
            raise RuntimeError(
                "ENCRYPTION_KEY is not configured — refusing to store sensitive data as "
                "plaintext in production. Set the ENCRYPTION_KEY environment variable."
            )
        return value
    try:
        return cipher.encrypt(value.encode()).decode()
    except Exception:  # noqa: BLE001
        logger.error("Encryption failed for value — returning plaintext")
        return value


def decrypt_value(value: str | None) -> str | None:
    if value is None:
        return None
    cipher = _get_cipher()
    if not cipher:
        return value
    try:
        return cipher.decrypt(value.encode()).decode()
    except InvalidToken:
        logger.debug("Decryption token invalid; returning ciphertext")
        return value
    except Exception:  # noqa: BLE001
        logger.debug("Decryption failed; returning ciphertext")
        return value
