"""Card-fraud risk helpers for storefront escrow.

A stable per-card fingerprint (the provider's card signature/token) lets us spot
one card funding many orders and enforce a temporary blocklist — mitigating the
"pay with a stolen card, receive goods, then charge back" laundering pattern.
Orders paid with a blocked/over-velocity card are HELD FOR REVIEW (never
auto-released), not silently refunded, so an admin makes the call.
"""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import models

logger = logging.getLogger(__name__)


def extract_fingerprint(provider: str | None, raw: dict | None) -> str | None:
    """Derive a stable per-card fingerprint from a provider verify payload."""
    if not raw:
        return None
    data = raw.get("data") or {}
    p = (provider or "").lower()
    if p == "paystack":
        sig = (data.get("authorization") or {}).get("signature")
        return f"ps:{sig}" if sig else None
    if p == "flutterwave":
        card = data.get("card") or {}
        token = card.get("token")
        if token:
            return f"fw:{token}"
        first6, last4 = card.get("first_6digits"), card.get("last_4digits")
        if first6 and last4:
            return f"fw:{first6}{last4}"
    return None


def is_card_blocked(db: Session, fingerprint: str | None) -> bool:
    if not fingerprint:
        return False
    row = db.query(models.BlockedCard).filter(models.BlockedCard.fingerprint == fingerprint).first()
    if not row:
        return False
    until = row.blocked_until
    if until is None:
        return True  # indefinite block
    if until.tzinfo is None:
        until = until.replace(tzinfo=dt.timezone.utc)
    return until > dt.datetime.now(dt.timezone.utc)


def block_card(
    db: Session,
    fingerprint: str | None,
    *,
    reason: str,
    days: int | None = None,
    provider: str | None = None,
) -> None:
    if not fingerprint:
        return
    days = days if days is not None else settings.CARD_BLOCK_DAYS_ON_REFUND
    until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=days)
    row = db.query(models.BlockedCard).filter(models.BlockedCard.fingerprint == fingerprint).first()
    if row:
        row.blocked_until = until
        row.reason = (reason or "")[:120]
        if provider:
            row.provider = provider
    else:
        db.add(
            models.BlockedCard(
                fingerprint=fingerprint,
                provider=provider,
                reason=(reason or "")[:120],
                blocked_until=until,
            )
        )
    db.commit()
    logger.warning("Card %s blocked until %s (%s)", fingerprint, until.isoformat(), reason)


def recent_order_count_for_card(db: Session, fingerprint: str | None) -> int:
    """How many storefront orders this card has funded in the last 24h."""
    if not fingerprint:
        return 0
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    return (
        db.query(func.count(models.StorefrontOrderEscrow.id))
        .filter(
            models.StorefrontOrderEscrow.card_fingerprint == fingerprint,
            models.StorefrontOrderEscrow.created_at >= since,
        )
        .scalar()
    ) or 0


def card_hold_reason(db: Session, fingerprint: str | None) -> str | None:
    """Return a hold-for-review reason if this card is blocked or over-velocity."""
    if not fingerprint:
        return None
    if is_card_blocked(db, fingerprint):
        return "card blocked (prior chargeback/refund)"
    if recent_order_count_for_card(db, fingerprint) >= settings.CARD_MAX_ORDERS_PER_DAY:
        return "one card funding many orders"
    return None
