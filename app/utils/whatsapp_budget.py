"""WhatsApp daily send budget tracker.

Caps outbound WhatsApp messages per day to control costs.
Meta charges ~$0.04 per marketing conversation and ~$0.02 per
utility conversation in Nigeria. Without a cap, 50K users could
generate $13K+/month in WhatsApp costs alone.

Budget is tracked in Redis with a daily key that auto-expires.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

# Default daily cap — override with WHATSAPP_DAILY_BUDGET env var.
# At $0.04/msg, 200/day = ~$8/day = ~$240/month.
DEFAULT_DAILY_BUDGET = 200

# High-value sends (payment alerts, confirmations) use a separate
# "priority" counter that doesn't eat into the marketing budget.
PRIORITY_DAILY_BUDGET = 100

_BUDGET_KEY = "wa:budget:{date}"
_PRIORITY_KEY = "wa:priority:{date}"


def _get_budget_limit() -> int:
    return int(getattr(settings, "WHATSAPP_DAILY_BUDGET", DEFAULT_DAILY_BUDGET))


def _get_today_key(prefix: str) -> str:
    return prefix.format(date=datetime.now(timezone.utc).strftime("%Y-%m-%d"))


def can_send_whatsapp(priority: bool = False) -> bool:
    """Check if we're within today's WhatsApp budget.

    Args:
        priority: True for high-value messages (payment alerts,
                  confirmations). These use a separate counter.

    Returns True if under the daily cap.
    """
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()

        if priority:
            key = _get_today_key(_PRIORITY_KEY)
            limit = PRIORITY_DAILY_BUDGET
        else:
            key = _get_today_key(_BUDGET_KEY)
            limit = _get_budget_limit()

        current = int(r.get(key) or 0)
        return current < limit
    except Exception:
        # If Redis is down, allow sends (fail open)
        return True


def record_whatsapp_send(priority: bool = False) -> int:
    """Record a WhatsApp send against today's budget.

    Returns the new count for today.
    """
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()

        if priority:
            key = _get_today_key(_PRIORITY_KEY)
        else:
            key = _get_today_key(_BUDGET_KEY)

        pipe = r.pipeline()
        pipe.incr(key)
        pipe.expire(key, 90000)  # 25 hours — auto-cleanup
        result = pipe.execute()
        return result[0]
    except Exception:
        return 0


def get_budget_status() -> dict:
    """Get current budget usage (for admin dashboard)."""
    try:
        from app.db.redis_client import get_redis_client

        r = get_redis_client()

        marketing_key = _get_today_key(_BUDGET_KEY)
        priority_key = _get_today_key(_PRIORITY_KEY)

        return {
            "marketing_used": int(r.get(marketing_key) or 0),
            "marketing_limit": _get_budget_limit(),
            "priority_used": int(r.get(priority_key) or 0),
            "priority_limit": PRIORITY_DAILY_BUDGET,
        }
    except Exception:
        return {
            "marketing_used": 0,
            "marketing_limit": _get_budget_limit(),
            "priority_used": 0,
            "priority_limit": PRIORITY_DAILY_BUDGET,
        }
