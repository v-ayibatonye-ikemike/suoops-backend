"""Simple WhatsApp message queue abstraction.

For MVP this attempts to use Celery for async processing; if Celery is unavailable,
processes messages synchronously for immediate response.
"""

from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAX_FALLBACK_BUFFER = 100  # Cap to prevent unbounded memory growth
_fallback_buffer: list[dict[str, Any]] = []


try:  # pragma: no cover - connection attempt
    from app.db.redis_client import get_redis_client

    _redis = get_redis_client()
    _redis.ping()
    _ENABLED = True
except Exception:  # noqa: BLE001
    _redis = None
    _ENABLED = False
    logger.warning("Redis not available, using in-memory WhatsApp queue fallback")

KEY = "whatsapp:inbound"


def _process_synchronously(payload: dict[str, Any]) -> None:
    """Process WhatsApp message synchronously when Celery is unavailable."""
    import asyncio

    from app.bot.nlp_service import NLPService
    from app.bot.whatsapp_adapter import WhatsAppClient, WhatsAppHandler
    from app.db.session import session_scope

    try:
        with session_scope() as db:
            handler = WhatsAppHandler(
                client=WhatsAppClient(settings.WHATSAPP_API_KEY),
                nlp=NLPService(),
                db=db,
            )
            asyncio.run(handler.handle_incoming(payload))
        logger.info("WhatsApp message processed synchronously")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to process WhatsApp message synchronously")


def _flush_fallback_queue() -> None:
    if not _fallback_buffer:
        return
    # Import task here to avoid circular imports
    from app.workers.tasks import process_whatsapp_inbound

    pending = list(_fallback_buffer)
    _fallback_buffer.clear()
    for idx, entry in enumerate(pending):
        try:
            process_whatsapp_inbound.delay(entry)
        except Exception:  # noqa: BLE001
            # Push remaining back for later retry
            _fallback_buffer.extend(pending[idx:])
            logger.warning("Celery still unavailable; %s WhatsApp payloads pending", len(_fallback_buffer))
            break


def enqueue_message(payload: dict[str, Any]) -> None:
    # Import task here to avoid circular imports
    from app.workers.tasks import process_whatsapp_inbound

    # Prefer asynchronous Celery worker
    try:
        process_whatsapp_inbound.delay(payload)
        _flush_fallback_queue()
        return
    except Exception:  # noqa: BLE001
        logger.warning("Celery dispatch failed; processing synchronously instead")

    # Celery unavailable - process synchronously for immediate response
    _process_synchronously(payload)

    # Trim buffer if it has grown beyond cap (defensive)
    if len(_fallback_buffer) > _MAX_FALLBACK_BUFFER:
        dropped = len(_fallback_buffer) - _MAX_FALLBACK_BUFFER
        del _fallback_buffer[:dropped]
        logger.warning("Dropped %d oldest fallback-queue entries (cap=%d)", dropped, _MAX_FALLBACK_BUFFER)


def drain_fallback() -> list[dict]:  # utility for tests
    items = list(_fallback_buffer)
    _fallback_buffer.clear()
    return items
