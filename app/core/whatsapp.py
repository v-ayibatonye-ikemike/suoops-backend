"""WhatsApp client factory — single source of truth for client instantiation."""

from __future__ import annotations

import logging

from app.bot.whatsapp_client import WhatsAppClient
from app.core.config import settings

logger = logging.getLogger(__name__)

_client: WhatsAppClient | None = None


def get_whatsapp_client() -> WhatsAppClient:
    """Return a shared WhatsApp client instance.

    Lazily creates the client on first call.  The instance is module-level
    so it's shared across the process (safe — ``WhatsAppClient`` is stateless
    and only holds config; each call opens its own HTTP request).
    """
    global _client
    if _client is None:
        _client = WhatsAppClient(settings.WHATSAPP_API_KEY)
    return _client
