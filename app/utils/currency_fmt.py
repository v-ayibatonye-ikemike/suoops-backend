"""Centralised currency formatting for WhatsApp bot messages.

Provides ``fmt_money()`` which formats an amount in the user's preferred
currency (NGN or USD) using the live exchange rate.

Usage
-----
    from app.utils.currency_fmt import fmt_money, get_user_currency

    currency = get_user_currency(db, issuer_id)           # "NGN" or "USD"
    text = fmt_money(50_000, currency)                     # "₦50,000" or "$37"
    text = fmt_money(1_200_000, currency, compact=True)    # "₦1.2M" or "$891"
"""

from __future__ import annotations

import logging
from decimal import Decimal

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── Live rate (lazy import to avoid circular deps) ───────────────────
_FALLBACK_RATE = Decimal("1600")


def _get_rate() -> Decimal:
    """Return live NGN-per-1-USD rate, with silent fallback."""
    try:
        from app.services.exchange_rate import get_ngn_usd_rate

        return get_ngn_usd_rate()
    except Exception:
        logger.warning("Failed to fetch exchange rate, using fallback %s", _FALLBACK_RATE)
        return _FALLBACK_RATE


def _to_usd(ngn_amount: float, rate: Decimal) -> float:
    """Convert an NGN amount to USD using the given rate."""
    if rate <= 0:
        return 0.0
    return float(Decimal(str(ngn_amount)) / rate)


# ── Public API ───────────────────────────────────────────────────────


def fmt_money(
    amount: float | int | Decimal,
    currency: str = "NGN",
    *,
    compact: bool = False,
    decimals: bool = False,
    convert: bool = True,
) -> str:
    """Format *amount* for display.

    Parameters
    ----------
    amount : float
        Monetary value.
    currency : str
        ``"NGN"`` (default) or ``"USD"``.
    compact : bool
        If True, use shortened form for large values (e.g. ₦1.2M / $891).
    decimals : bool
        If True, always show 2 decimal places (₦50,000.00).
    convert : bool
        If True (default), treat *amount* as NGN and convert to USD when
        currency is ``"USD"``.  Set to False when the amount is **already**
        denominated in the target currency (e.g. a USD invoice stored as
        USD in the database).
    """
    amt = float(amount or 0)

    if currency == "USD":
        if convert:
            rate = _get_rate()
            usd = _to_usd(amt, rate)
        else:
            usd = amt
        if compact and usd >= 1_000_000:
            return f"${usd / 1_000_000:,.1f}M"
        if compact or not decimals:
            return f"${usd:,.0f}"
        return f"${usd:,.2f}"

    # NGN (default)
    if compact and amt >= 1_000_000:
        return f"₦{amt / 1_000_000:,.1f}M"
    if decimals:
        return f"₦{amt:,.2f}"
    if compact:
        return f"₦{amt:,.0f}"
    return f"₦{amt:,.0f}"


def fmt_money_full(amount: float | int | Decimal, currency: str = "NGN", *, convert: bool = True) -> str:
    """Always show 2 decimal places: ₦50,000.00 / $37.13."""
    return fmt_money(amount, currency, decimals=True, convert=convert)


def get_user_currency(db: Session, user_id: int) -> str:
    """Look up the user's preferred display currency (``NGN`` or ``USD``).

    Falls back to ``"NGN"`` if the user is not found or has no preference.
    """
    from app.models.models import User

    user = db.query(User).filter(User.id == user_id).first()
    if user and getattr(user, "preferred_currency", None):
        return user.preferred_currency  # type: ignore[return-value]
    return "NGN"
