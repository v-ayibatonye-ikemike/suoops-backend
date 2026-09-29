"""Ephemeral 'who owes me money' list state for WhatsApp.

When a user types ``owed`` / ``pending`` / ``who owes me``, we send a
numbered list of unpaid invoices and remember the row → invoice_id
mapping in Redis so the next reply (``1 paid``, ``2 remind``) acts on
the right invoice.

Mirrors the Redis-backed pattern from :mod:`app.bot.onboarding_flow`.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

_TTL = 600  # 10 minutes
_KEY_PREFIX = "bot:owed_list:"


def _redis():
    try:
        from app.db.redis_client import get_redis_client

        return get_redis_client()
    except Exception:
        return None


def _key(phone: str) -> str:
    return f"{_KEY_PREFIX}{phone}"


def save_owed_list(phone: str, invoice_ids: list[str]) -> None:
    """Remember the numbered list of invoice IDs for this phone."""
    r = _redis()
    if r is None:
        return
    try:
        r.setex(_key(phone), _TTL, json.dumps(invoice_ids))
    except Exception:
        logger.exception("Failed to save owed-list session for %s", phone)


def get_owed_list(phone: str) -> list[str] | None:
    """Return the numbered list, or None if expired/missing."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_key(phone))
        if not raw:
            return None
        data = json.loads(raw)
        if isinstance(data, list):
            return [str(x) for x in data]
    except Exception:
        logger.exception("Failed to read owed-list session for %s", phone)
    return None


def clear_owed_list(phone: str) -> None:
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_key(phone))
    except Exception:
        logger.exception("Failed to clear owed-list session for %s", phone)
