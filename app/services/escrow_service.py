"""Storefront order escrow — trust rules, hold windows, and seller payout setup.

This module holds the pure/decision logic + the seller Paystack Transfer
Recipient onboarding. It does NOT move money — the actual hold/release/refund
flow (payment webhook, auto-release worker, refunds) lands in later steps and
uses these helpers.
"""

from __future__ import annotations

import datetime as dt
import logging
import math
import secrets

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import models

logger = logging.getLogger(__name__)


class EscrowError(Exception):
    """Raised when an escrow payout/recipient operation can't be completed."""


# ── Trust rules ────────────────────────────────────────────────────────


def is_trusted_seller(db: Session, user: models.User) -> bool:
    """Whether a seller may skip the escrow hold (normal/instant settlement).

    ALL must hold: active store, not fraud-flagged, ZERO unresolved disputes,
    and the configured tenure + paid-invoice thresholds. Computed per order so
    trust is automatically revoked if a seller starts getting disputes.
    """
    if user.store_status != "active" or user.flagged_for_review:
        return False

    now = dt.datetime.now(dt.timezone.utc)
    created = user.created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=dt.timezone.utc)
    if created is None or (now - created).days < settings.ESCROW_TRUST_MIN_ACCOUNT_AGE_DAYS:
        return False

    paid_invoices = (
        db.query(func.count(models.Invoice.id))
        .filter(
            models.Invoice.issuer_id == user.id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "paid",
        )
        .scalar()
    ) or 0
    if paid_invoices < settings.ESCROW_TRUST_MIN_PAID_INVOICES:
        return False

    # Breadth, not just volume: trust requires many DISTINCT paying customers so
    # a seller can't self-deal (pay their own invoices) into trusted status.
    distinct_customers = (
        db.query(func.count(func.distinct(models.Invoice.customer_id)))
        .filter(
            models.Invoice.issuer_id == user.id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "paid",
        )
        .scalar()
    ) or 0
    if distinct_customers < settings.ESCROW_TRUST_MIN_DISTINCT_CUSTOMERS:
        return False

    # A track record of actually-completed storefront deliveries (released holds).
    deliveries = (
        db.query(func.count(models.StorefrontOrderEscrow.id))
        .filter(
            models.StorefrontOrderEscrow.seller_id == user.id,
            models.StorefrontOrderEscrow.status == "released",
        )
        .scalar()
    ) or 0
    if deliveries < settings.ESCROW_TRUST_MIN_DELIVERIES:
        return False

    # Any disputed/refunded storefront order permanently blocks trust.
    disputes = (
        db.query(func.count(models.StorefrontOrderEscrow.id))
        .filter(
            models.StorefrontOrderEscrow.seller_id == user.id,
            models.StorefrontOrderEscrow.status.in_(["disputed", "refunded"]),
        )
        .scalar()
    ) or 0
    return disputes == 0


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """Great-circle distance in metres between two lat/lng points."""
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlmb = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def detect_order_collusion(
    seller: models.User,
    *,
    buyer_ip: str | None,
    customer_lat: float | None,
    customer_lng: float | None,
    buyer_phone: str | None = None,
) -> str | None:
    """Return a short reason string if a storefront order looks like the seller
    ordering from themselves (trust-farming / laundering), else None.

    Signals: buyer shares the seller's signup IP, the buyer's GPS pin sits on top
    of the seller's own store location, or the buyer's phone is the seller's own
    number.
    """
    reasons: list[str] = []
    if buyer_ip and seller.signup_ip and buyer_ip == seller.signup_ip:
        reasons.append("shared IP")
    # Self-dealing tell: ordering to your own phone number.
    seller_phone = _norm_phone(getattr(seller, "phone", None))
    bphone = _norm_phone(buyer_phone)
    if seller_phone and bphone and seller_phone == bphone:
        reasons.append("buyer is the seller's own number")
    if (
        customer_lat is not None
        and customer_lng is not None
        and seller.storefront_lat is not None
        and seller.storefront_lng is not None
    ):
        try:
            if (
                _haversine_m(customer_lat, customer_lng, seller.storefront_lat, seller.storefront_lng)
                < settings.ESCROW_COLLUSION_RADIUS_M
            ):
                reasons.append("buyer at seller location")
        except (ValueError, TypeError):
            pass
    return ", ".join(reasons) or None


def seller_velocity_hold_reason(db: Session, seller: models.User, order_naira) -> str | None:
    """Hold-for-review reasons from a seller's recent money velocity.

    The in-flight cap resets the moment an order releases, so a bad actor could
    launder small amounts across many days without ever tripping it. This looks
    at the ROLLING window: total settled (released) payout volume and the number
    of recent disputes/refunds. Exceeding either holds NEW orders for admin
    review instead of letting them auto-release.
    """
    reasons: list[str] = []
    window_start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=settings.ESCROW_SELLER_VELOCITY_WINDOW_DAYS)
    try:
        from decimal import Decimal

        order_kobo = int(Decimal(str(order_naira)) * 100)
    except Exception:  # noqa: BLE001
        order_kobo = 0

    settled_kobo = (
        db.query(func.coalesce(func.sum(models.StorefrontOrderEscrow.payout_kobo), 0))
        .filter(
            models.StorefrontOrderEscrow.seller_id == seller.id,
            models.StorefrontOrderEscrow.status == "released",
            models.StorefrontOrderEscrow.released_at >= window_start,
        )
        .scalar()
    ) or 0
    if settled_kobo + order_kobo > settings.ESCROW_SELLER_MAX_SETTLED_NAIRA_UNTRUSTED * 100:
        reasons.append("high recent payout volume")

    recent_disputes = (
        db.query(func.count(models.StorefrontOrderEscrow.id))
        .filter(
            models.StorefrontOrderEscrow.seller_id == seller.id,
            models.StorefrontOrderEscrow.status.in_(["disputed", "refunded"]),
            models.StorefrontOrderEscrow.created_at >= window_start,
        )
        .scalar()
    ) or 0
    if recent_disputes >= settings.ESCROW_SELLER_DISPUTE_HOLD_AT:
        reasons.append(f"{recent_disputes} recent disputes")

    return ", ".join(reasons) or None


def hold_window(same_state: bool) -> dt.timedelta:
    """Dispute/hold window: 12h when buyer & seller share a state, else 3 days."""
    if same_state:
        return dt.timedelta(hours=settings.ESCROW_SAME_STATE_HOLD_HOURS)
    return dt.timedelta(days=settings.ESCROW_CROSS_STATE_HOLD_DAYS)


def add_business_days(start: dt.datetime, days: int) -> dt.datetime:
    """Advance ``start`` by ``days`` working days (skipping Sat/Sun), keeping the
    same time of day. Weekends do not count toward the window."""
    cur = start
    remaining = max(0, int(days))
    while remaining > 0:
        cur = cur + dt.timedelta(days=1)
        if cur.weekday() < 5:  # Mon(0)–Fri(4)
            remaining -= 1
    return cur


def release_due_after(paid_at: dt.datetime, same_state: bool, cross_state_days: int | None = None) -> dt.datetime:
    """When the dispute/hold window closes for a payment at ``paid_at``.

    Same-state orders get a short 12h window (hours-based). Cross-state orders get
    N *working* days — weekends don't count, since couriers and buyers are far
    less active then. ``cross_state_days`` lets the caller scale the window by how
    far apart the states are (Lagos→Abuja < Kaduna→Rivers); it defaults to the
    base ``ESCROW_CROSS_STATE_HOLD_DAYS``.
    """
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=dt.timezone.utc)
    if same_state:
        return paid_at + dt.timedelta(hours=settings.ESCROW_SAME_STATE_HOLD_HOURS)
    days = cross_state_days if cross_state_days is not None else settings.ESCROW_CROSS_STATE_HOLD_DAYS
    return add_business_days(paid_at, days)


# West Africa Time (Nigeria) — Flutterwave settles collections T+1 by ~7am WAT.
_WAT = dt.timezone(dt.timedelta(hours=1))


def next_settlement_after(paid_at: dt.datetime) -> dt.datetime:
    """Earliest time a payout may execute for a payment made at ``paid_at``.

    Sellers settle on a T+1 **business-day** cadence: the daily settlement run
    (07:00 UTC / 08:00 WAT) on the next BUSINESS day after the payment. Weekends
    are skipped because the collection provider's own T+1 settlement doesn't run
    Sat/Sun — a Friday order's funds only land in our balance on Monday, so
    paying out Saturday would draw on money that hasn't settled yet (stuck
    "processing"). Returns a tz-aware UTC datetime.
    """
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=dt.timezone.utc)
    hour = settings.ESCROW_SETTLEMENT_HOUR_UTC
    # Next BUSINESS day (skips Sat/Sun) in WAT — matches the provider's T+1
    # business-day settlement so we never pay out before the funds have landed.
    wat_next_biz = add_business_days(paid_at.astimezone(_WAT), 1)
    wat_day = wat_next_biz.date()
    run_utc = dt.datetime.combine(wat_day, dt.time(hour=hour), tzinfo=dt.timezone.utc)
    # Guard: never before the payment itself (paranoia around DST-free WAT).
    return max(run_utc, paid_at + dt.timedelta(hours=1))


def _norm_state(value: str | None) -> str | None:
    """Normalize a state name for comparison (lowercase, strip a trailing
    'state', drop non-alphanumerics). e.g. 'Lagos State' == 'lagos'."""
    if not value:
        return None
    s = "".join(ch for ch in value.lower() if ch.isalnum() or ch == " ").strip()
    if s.endswith(" state"):
        s = s[: -len(" state")].strip()
    s = s.replace(" ", "")
    return s or None


def _unique_confirmation_code(db: Session, seller_id: int) -> str:
    """A 6-digit buyer release code that doesn't collide with any of this
    seller's currently-live orders (pending/held/disputed), so entering a code
    can never match the wrong order. Falls back after a few tries — collision
    odds are tiny and a stale/terminal order sharing a code is harmless."""
    live = ("pending", "held", "disputed")
    for _ in range(8):
        code = f"{secrets.randbelow(900000) + 100000}"
        clash = (
            db.query(models.StorefrontOrderEscrow.id)
            .filter(
                models.StorefrontOrderEscrow.seller_id == seller_id,
                models.StorefrontOrderEscrow.confirmation_code == code,
                models.StorefrontOrderEscrow.status.in_(live),
            )
            .first()
        )
        if not clash:
            return code
    return f"{secrets.randbelow(900000) + 100000}"


def create_order_escrow(
    db: Session,
    *,
    invoice: models.Invoice,
    seller: models.User,
    gross_naira,
    customer_lat: float | None,
    customer_lng: float | None,
    review_reason: str | None = None,
    no_delivery: bool = False,
) -> models.StorefrontOrderEscrow:
    """Create the PENDING escrow hold for a fresh storefront order.

    Captures the customer's GPS-derived state (server-side) and whether it
    matches the seller's state (drives the 12h vs 3-day window). Generates the
    buyer-only delivery code and flags the order for review if it looks like
    self-dealing. The hold is activated (status 'held', release_due_at set) when
    payment is confirmed. ``no_delivery`` (a service/digital order that isn't
    shipped) uses the fast same-state window since there is nothing in transit.
    """
    from decimal import Decimal

    from app.services.geocode_service import reverse_geocode
    from app.utils.feature_gate import platform_fee_kobo

    customer_state = None
    if customer_lat is not None and customer_lng is not None:
        customer_state, _city = reverse_geocode(customer_lat, customer_lng)

    business_state = seller.storefront_state
    bs, cs = _norm_state(business_state), _norm_state(customer_state)
    same_state: bool | None = (bs == cs) if (bs and cs) else None
    if no_delivery:
        # Nothing ships, so distance is irrelevant — use the fast (same-state)
        # buyer-protection window rather than defaulting to the 3-day one.
        same_state = True

    gross_kobo = int(Decimal(str(gross_naira)) * 100)
    fee_kobo = platform_fee_kobo(gross_naira)
    # The buyer pays the platform fee ON TOP at checkout (goods + fee + delivery),
    # so the seller is paid the FULL goods value. The fee is retained by the
    # platform out of the buyer's payment — it is never deducted from the seller.
    payout_kobo = gross_kobo

    escrow = models.StorefrontOrderEscrow(
        invoice_id=invoice.id,
        seller_id=seller.id,
        status="pending",  # -> 'held' on payment confirmation
        same_state=same_state,
        gross_kobo=gross_kobo,
        fee_kobo=fee_kobo,
        payout_kobo=payout_kobo,
        business_state=business_state,
        customer_state=customer_state,
        customer_lat=customer_lat,
        customer_lng=customer_lng,
        # 6-digit buyer-only delivery code (never shown to the seller). Unique
        # among this seller's live orders so a code can never match a wrong order.
        confirmation_code=_unique_confirmation_code(db, seller.id),
        requires_delivery=not no_delivery,
        held_for_review=bool(review_reason),
        review_reason=review_reason,
    )
    db.add(escrow)
    db.commit()
    db.refresh(escrow)
    if review_reason:
        logger.warning(
            "Storefront order %s flagged for review (seller %s): %s",
            invoice.id,
            seller.id,
            review_reason,
        )
    return escrow


def activate_escrow_on_payment(
    db: Session,
    invoice: models.Invoice,
    *,
    charge_reference: str | None = None,
    card_fingerprint: str | None = None,
    review_reason: str | None = None,
) -> None:
    """Activate a pending storefront-order hold once payment is confirmed.

    Flips ``pending -> held`` and sets ``release_due_at = paid_at + window`` (12h
    same-state, else 3 days; unknown state → the safer cross-state window).
    Captures the charge reference (for refunds) and the funding-card fingerprint.
    If ``review_reason`` is given (e.g. a blocked/over-velocity card) the order is
    held for admin review and never auto-releases. Idempotent — only acts on a
    pending row. No money moves here.
    """
    escrow = (
        db.query(models.StorefrontOrderEscrow)
        .filter(
            models.StorefrontOrderEscrow.invoice_id == invoice.id,
            models.StorefrontOrderEscrow.status == "pending",
        )
        .first()
    )
    if not escrow:
        return

    paid_at = getattr(invoice, "paid_at", None) or dt.datetime.now(dt.timezone.utc)
    if paid_at.tzinfo is None:
        paid_at = paid_at.replace(tzinfo=dt.timezone.utc)

    # Unknown same/different state → treat as cross-state (longer, safer window).
    same = bool(escrow.same_state) if escrow.same_state is not None else False
    escrow.status = "held"
    if same:
        escrow.release_due_at = release_due_after(paid_at, True)
    else:
        # Scale the working-day window by how far apart the two states are.
        from app.services.delivery_zones import cross_state_delivery_days

        days = cross_state_delivery_days(escrow.business_state, escrow.customer_state)
        escrow.release_due_at = release_due_after(paid_at, False, cross_state_days=days)
    # Payouts settle on a T+1 cadence — never before the collection has settled.
    escrow.settle_at = next_settlement_after(paid_at)
    if charge_reference:
        escrow.charge_reference = charge_reference
    if card_fingerprint:
        escrow.card_fingerprint = card_fingerprint
    # A blocked/over-velocity card (or any supplied reason) → hold for review so
    # a card-fraud order can never auto-release to the seller.
    if review_reason:
        escrow.held_for_review = True
        escrow.review_reason = (review_reason or "")[:120]
    db.commit()
    logger.info(
        "Escrow held for order invoice=%s (same_state=%s, release_due_at=%s, settle_at=%s)",
        invoice.id,
        escrow.same_state,
        escrow.release_due_at,
        escrow.settle_at,
    )

    # Best-effort: send the buyer their delivery code so they can release the
    # payment on arrival. It's shown to the buyer at checkout too.
    try:
        customer = getattr(invoice, "customer", None)
        seller = db.query(models.User).filter(models.User.id == escrow.seller_id).first()
        send_delivery_code(
            getattr(customer, "phone", None),
            escrow.confirmation_code or "",
            getattr(seller, "business_name", None) if seller else None,
        )
    except Exception:  # noqa: BLE001
        logger.exception("Failed to dispatch delivery code for invoice %s", invoice.id)


# ── Release (pay the seller) ───────────────────────────────────────────


def release_escrow(db: Session, escrow: models.StorefrontOrderEscrow, *, reason: str = "auto") -> bool:
    """Pay held funds out to the seller (gross − commission) via the configured
    payout provider.

    Transfers are ASYNCHRONOUS on both rails — a queued transfer is not yet
    disbursed — so this is an idempotent state machine, not a fire-and-forget:

    * If a transfer was already initiated (``transfer_reference`` set), reconcile
      its outcome FIRST and never re-send while it's in flight:
        - ``successful`` → mark released.
        - ``pending`` / ``unknown`` → leave 'held', return False (wait for a later
          run to confirm). ``unknown`` is treated as "wait" so a transport blip
          never triggers a double-payment.
        - ``failed`` → retry with a FRESH reference (the old one is burned).
    * A freshly queued transfer is only finalized once confirmed ``successful``;
      otherwise the row stays 'held' and the worker retries.

    Returns True once released (or already released). Returns False when a payout
    is in flight / not yet confirmed. Raises EscrowError on a genuine failure so
    the caller can retry later (the row stays 'held').
    """
    # Serialize concurrent releases (auto-worker vs admin action vs retries) to
    # prevent a DOUBLE PAYOUT: take a row lock and re-read status under it. The
    # loser of the race then sees 'released' and returns without sending again.
    # (with_for_update is a harmless no-op on SQLite used in tests.)
    _eid = getattr(escrow, "id", None)
    if _eid is not None:
        locked = (
            db.query(models.StorefrontOrderEscrow)
            .filter(models.StorefrontOrderEscrow.id == _eid)
            .with_for_update()
            .first()
        )
        if locked is not None:
            escrow = locked

    if escrow.status == "released":
        return True
    if escrow.status != "held":
        return False  # pending / disputed / refunded — not releasable

    # Collusion/anomaly-flagged orders never auto-pay — an admin must decide.
    if escrow.held_for_review:
        raise EscrowError(f"Escrow {escrow.id} held for review — not auto-releasable")

    if escrow.payout_kobo <= 0:
        # Nothing to pay out (shouldn't happen) — close it cleanly.
        escrow.status = "released"
        escrow.released_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        return True

    seller = db.query(models.User).filter(models.User.id == escrow.seller_id).first()
    if not seller:
        raise EscrowError(f"Seller {escrow.seller_id} not found for escrow {escrow.id}")

    # Payouts are frozen for a cooldown after a bank/payout change (anti-takeover).
    frozen = seller.payout_frozen_until
    if frozen is not None:
        if frozen.tzinfo is None:
            frozen = frozen.replace(tzinfo=dt.timezone.utc)
        if frozen > dt.datetime.now(dt.timezone.utc):
            raise EscrowError(f"Payouts frozen for seller {seller.id} until {frozen.isoformat()}")

    from app.services.payouts import (
        PayoutError,
        get_payout_provider,
        get_payout_provider_named,
    )

    # Release through the SAME rail that COLLECTED the order — the held funds sit
    # in that provider's balance (e.g. a Flutterwave-collected order must pay out
    # from Flutterwave, not Paystack). Falls back to the configured default.
    if escrow.charge_reference:
        provider = get_payout_provider_named(_collector_for_charge(db, escrow.charge_reference))
    else:
        provider = get_payout_provider()

    def _finalize(ref: str) -> bool:
        escrow.status = "released"
        escrow.released_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        logger.info(
            "Escrow %s released via %s — %s kobo to seller %s (ref=%s)",
            escrow.id,
            provider.name,
            escrow.payout_kobo,
            seller.id,
            ref,
        )
        return True

    # A reference is only valid on the rail it was sent to. If the payout rail
    # has since changed (e.g. an early Paystack attempt that failed on balance,
    # now correctly routed to Flutterwave), the old reference is void on the new
    # provider — querying it returns 'unknown', which must NOT be read as
    # "in flight". Detect that and start a fresh transfer on the current rail.
    rail_changed = bool(
        escrow.transfer_reference and escrow.transfer_provider and escrow.transfer_provider != provider.name
    )

    # Reconcile an already-initiated transfer (on the SAME rail) before sending new.
    if escrow.transfer_reference and not rail_changed:
        prior = provider.transfer_status(escrow.transfer_reference)
        if prior == "successful":
            return _finalize(escrow.transfer_reference)
        if prior in ("pending", "unknown"):
            # In flight or indeterminate — do NOT re-send; a later run confirms.
            return False
        # prior == "failed" → the reference is burned; retry with a fresh one below.

    # T+1 settlement gate: never START a new payout before the collection has
    # settled. Buyer protection may be over, but the money settles to the seller
    # in the next daily settlement run (funded by settled collections, not float).
    # (An in-flight transfer above is exempt — it's already been sent.)
    settle_at = getattr(escrow, "settle_at", None)
    if settle_at is not None and not escrow.transfer_reference:
        if settle_at.tzinfo is None:
            settle_at = settle_at.replace(tzinfo=dt.timezone.utc)
        if settle_at > dt.datetime.now(dt.timezone.utc):
            return False  # cleared but not yet settle-eligible — pay in the next run

    # First-ever attempt keeps the clean deterministic reference; a retry (after a
    # confirmed-failed transfer OR a rail change) gets a fresh (unburned) reference.
    if not escrow.transfer_reference:
        reference = f"ESCROWREL-{escrow.id}"
    else:
        reference = f"ESCROWREL-{escrow.id}-{int(dt.datetime.now(dt.timezone.utc).timestamp())}"

    payout_reason = f"Storefront order payout ({reason}) — invoice {escrow.invoice_id}"

    # Record intent (reference + rail) before calling the provider so a crash
    # mid-flight is recoverable and the reference is never reconciled cross-rail.
    if escrow.transfer_reference != reference or escrow.transfer_provider != provider.name:
        escrow.transfer_reference = reference
        escrow.transfer_provider = provider.name
        db.commit()

    try:
        result = provider.transfer(
            db,
            seller=seller,
            amount_kobo=int(escrow.payout_kobo),
            reference=reference,
            reason=payout_reason,
        )
    except PayoutError as exc:  # network/transport failure → retry later
        raise EscrowError(f"Transfer request failed: {exc}") from exc

    status = (result.status or "").lower()
    # Only finalize on confirmed disbursement. A confirmed-successful response (or
    # verify call) releases; an accepted/queued transfer stays 'held' until a
    # later run confirms it (avoids marking released before the money moves).
    if status == "successful" or provider.transfer_exists(reference):
        return _finalize(reference)
    if result.ok or status in ("pending", "queued", "new"):
        return False  # accepted/in-flight — confirm on the next run, do NOT re-send
    raise EscrowError(f"Transfer failed for escrow {escrow.id}: {result.message}")


def payout_rail_for(db: Session, escrow: models.StorefrontOrderEscrow) -> str:
    """The payout provider name :func:`release_escrow` would use for this order —
    the rail that COLLECTED it (funds sit there), else the configured default.

    Used to group a seller's due orders by rail before a consolidated payout, so
    a batch only ever sums orders that can be paid from the same balance.
    """
    from app.services.payouts import get_payout_provider, get_payout_provider_named

    if escrow.charge_reference:
        return get_payout_provider_named(_collector_for_charge(db, escrow.charge_reference)).name
    return get_payout_provider().name


def release_seller_batch(
    db: Session,
    escrows: list[models.StorefrontOrderEscrow],
    *,
    provider_name: str,
    reason: str = "auto",
) -> int:
    """Pay a seller ONE consolidated transfer for several due held orders that all
    collected on the SAME rail (``provider_name``).

    Mirrors :func:`release_escrow`'s idempotent state machine, but sums the
    payouts and sends a single provider transfer whose reference is stamped on
    EVERY order in the batch — so the seller sees one credit instead of dozens and
    we pay one transfer fee. Same money-safety guarantees as the per-order path:

    * Row-locks the whole set and re-reads status under the lock.
    * Reconciles any already-initiated transfer FIRST (grouped by its shared
      reference) and never re-sends while one is in flight; ``unknown`` waits.
    * Honors the T+1 ``settle_at`` gate and the seller payout freeze.
    * Records intent (reference + rail) on all rows BEFORE the provider call, so a
      crash is recoverable and no reference is ever reconciled cross-rail.
    * Finalizes a batch only once the transfer is confirmed ``successful``.

    Returns the number of orders actually released in this run (0 while a payout is
    in flight / not yet settle-eligible). Raises EscrowError on a genuine failure
    so the caller can retry later (the rows stay 'held').
    """
    from app.services.payouts import PayoutError, get_payout_provider_named

    if not escrows:
        return 0
    ids = [e.id for e in escrows if getattr(e, "id", None) is not None]
    if not ids:
        return 0

    # Serialize against the per-order worker / admin actions / retries.
    locked = (
        db.query(models.StorefrontOrderEscrow).filter(models.StorefrontOrderEscrow.id.in_(ids)).with_for_update().all()
    )
    now = dt.datetime.now(dt.timezone.utc)

    # Only genuinely releasable rows: still held, not flagged, something to pay.
    eligible = [e for e in locked if e.status == "held" and not e.held_for_review and (e.payout_kobo or 0) > 0]
    if not eligible:
        return 0

    seller = db.query(models.User).filter(models.User.id == eligible[0].seller_id).first()
    if not seller:
        raise EscrowError(f"Seller {eligible[0].seller_id} not found for batch payout")

    frozen = seller.payout_frozen_until
    if frozen is not None:
        if frozen.tzinfo is None:
            frozen = frozen.replace(tzinfo=dt.timezone.utc)
        if frozen > now:
            raise EscrowError(f"Payouts frozen for seller {seller.id} until {frozen.isoformat()}")

    provider = get_payout_provider_named(provider_name)

    def _finalize(group: list[models.StorefrontOrderEscrow], ref: str) -> None:
        for e in group:
            e.status = "released"
            e.released_at = dt.datetime.now(dt.timezone.utc)
        db.commit()
        logger.info(
            "Escrow batch released via %s — %s kobo to seller %s over %d orders (ref=%s)",
            provider.name,
            sum(int(e.payout_kobo) for e in group),
            seller.id,
            len(group),
            ref,
        )

    released = 0

    # ── 1. Reconcile any already-initiated transfers first ────────────────
    # Group stamped rows by their shared reference. A batch sent on a prior run
    # (or a leftover per-order ESCROWREL ref) is confirmed/cleared before we send
    # anything new, so an in-flight transfer is never double-paid. A rail change
    # voids the old reference (unknown on the new provider) → treat as fresh.
    stamped: dict[str, list[models.StorefrontOrderEscrow]] = {}
    fresh: list[models.StorefrontOrderEscrow] = []
    for e in eligible:
        ref = e.transfer_reference
        rail_changed = bool(ref and e.transfer_provider and e.transfer_provider != provider.name)
        if ref and not rail_changed:
            stamped.setdefault(ref, []).append(e)
        else:
            if rail_changed:
                e.transfer_reference = None
            fresh.append(e)

    for ref, group in stamped.items():
        prior = provider.transfer_status(ref)
        if prior == "successful":
            _finalize(group, ref)
            released += len(group)
        elif prior in ("pending", "unknown"):
            continue  # in flight / indeterminate — wait for a later run
        else:  # failed → burn the reference, retry these in the fresh batch
            for e in group:
                e.transfer_reference = None
            fresh.extend(group)

    if not fresh:
        db.commit()
        return released

    # ── 2. Settlement gate — only pay orders whose collection has settled ──
    payable: list[models.StorefrontOrderEscrow] = []
    for e in fresh:
        settle_at = getattr(e, "settle_at", None)
        if settle_at is not None:
            if settle_at.tzinfo is None:
                settle_at = settle_at.replace(tzinfo=dt.timezone.utc)
            if settle_at > now:
                continue  # cleared but not settle-eligible — pay in a later run
        payable.append(e)

    if not payable:
        db.commit()
        return released

    # ── 3. One transfer for the summed payout, stamped on every order ─────
    total_kobo = sum(int(e.payout_kobo) for e in payable)
    batch_ref = (
        f"ESCROWBATCH-{seller.id}-{min(e.id for e in payable)}-" f"{int(now.timestamp())}-{secrets.token_hex(3)}"
    )
    payout_reason = f"Storefront payout ({reason}) — {len(payable)} orders for seller {seller.id}"

    # Record intent on ALL rows before the provider call (crash-recoverable).
    for e in payable:
        e.transfer_reference = batch_ref
        e.transfer_provider = provider.name
    db.commit()

    try:
        result = provider.transfer(
            db,
            seller=seller,
            amount_kobo=total_kobo,
            reference=batch_ref,
            reason=payout_reason,
        )
    except PayoutError as exc:  # network/transport failure → retry later
        raise EscrowError(f"Batch transfer request failed: {exc}") from exc

    status = (result.status or "").lower()
    if status == "successful" or provider.transfer_exists(batch_ref):
        _finalize(payable, batch_ref)
        return released + len(payable)
    if result.ok or status in ("pending", "queued", "new"):
        db.commit()  # keep the stamped intent; confirm/finalize on the next run
        return released  # accepted/in-flight — do NOT re-send
    # Genuine failure — burn the batch reference so a later run retries cleanly.
    for e in payable:
        e.transfer_reference = None
    db.commit()
    raise EscrowError(f"Batch transfer failed for seller {seller.id}: {result.message}")


# ── Refund (return money to the buyer) ─────────────────────────────────


def _collector_for_charge(db: Session, charge_reference: str) -> str:
    """Which provider collected this charge (recorded in the payment metadata).
    Refunds MUST go back through the collecting rail. Defaults to Paystack."""
    from app.models.payment_models import PaymentTransaction

    txn = db.query(PaymentTransaction).filter(PaymentTransaction.reference == charge_reference).one_or_none()
    if txn and txn.payment_metadata:
        return txn.payment_metadata.get("collector") or "paystack"
    return "paystack"


def refund_escrow(db: Session, escrow: models.StorefrontOrderEscrow, *, reason: str = "dispute") -> bool:
    """Refund the buyer for a held/disputed order via the COLLECTING provider.

    A refund reverses the exact original charge, so it routes back through the
    provider that collected the order (Paystack or Flutterwave). The full gross
    amount is refunded (funds were held, never transferred to the seller).
    Idempotent: once ``refunded`` it is a no-op. Raises EscrowError on a genuine
    failure so the caller can retry (row stays in its current state).
    """
    # Lock the row so a refund can't race a release (money out twice) — the loser
    # sees the terminal state and stops. No-op on SQLite (tests).
    _eid = getattr(escrow, "id", None)
    if _eid is not None:
        locked = (
            db.query(models.StorefrontOrderEscrow)
            .filter(models.StorefrontOrderEscrow.id == _eid)
            .with_for_update()
            .first()
        )
        if locked is not None:
            escrow = locked

    if escrow.status == "refunded":
        return True
    if escrow.status == "released":
        raise EscrowError(f"Escrow {escrow.id} already paid out — cannot refund")

    if not escrow.charge_reference:
        raise EscrowError(f"Escrow {escrow.id} has no charge reference to refund")

    from app.services.collections import CollectionError, get_collection_provider_named

    collector = get_collection_provider_named(_collector_for_charge(db, escrow.charge_reference))

    try:
        data = collector.refund(
            reference=escrow.charge_reference,
            # Make the buyer whole: refund everything they paid — goods, the
            # platform service fee (charged on top at checkout) and delivery.
            amount_kobo=(
                int(escrow.gross_kobo)
                + int(getattr(escrow, "fee_kobo", 0) or 0)
                + int(getattr(escrow, "delivery_fee_kobo", 0) or 0)
            ),
            note=f"Storefront buyer protection ({reason}) — invoice {escrow.invoice_id}",
        )
    except CollectionError as exc:  # network/timeout / provider error → retry later
        raise EscrowError(str(exc)) from exc

    refund = data.get("data") or {}
    escrow.status = "refunded"
    escrow.refunded_at = dt.datetime.now(dt.timezone.utc)
    escrow.refund_reference = str(refund.get("id") or escrow.charge_reference)[:100]
    db.commit()
    # The buyer's delivery fee was refunded too. Recover it: cancel the courier
    # booking to reclaim the fee if it hasn't been delivered; if it was already
    # delivered (seller's goods were the problem), the seller absorbs the cost.
    try:
        _settle_refunded_delivery_fee(db, escrow)
    except Exception:  # noqa: BLE001 — never let fee recovery undo the refund
        logger.exception("Delivery-fee settlement failed for escrow %s", getattr(escrow, "id", None))
    refunded_kobo = (
        int(escrow.gross_kobo)
        + int(getattr(escrow, "fee_kobo", 0) or 0)
        + int(getattr(escrow, "delivery_fee_kobo", 0) or 0)
    )
    logger.info(
        "Escrow %s refunded via %s — %s kobo returned to buyer (charge=%s)",
        escrow.id,
        collector.name,
        refunded_kobo,
        escrow.charge_reference,
    )
    return True


def _settle_refunded_delivery_fee(db: Session, escrow: models.StorefrontOrderEscrow) -> None:
    """Recover the refunded delivery fee. Delivered → the seller absorbs it (debit
    their wallet). Not yet delivered → cancel the Shipbubble shipment to reclaim
    the fee. If neither is possible, log for manual review."""
    fee = int(getattr(escrow, "delivery_fee_kobo", 0) or 0)
    if fee <= 0:
        return
    delivered = getattr(escrow, "courier_delivered_at", None) is not None
    order_id = getattr(escrow, "shipbubble_order_id", None)
    if delivered:
        seller = db.query(models.User).filter(models.User.id == escrow.seller_id).first()
        if seller is not None:
            seller.wallet_balance_kobo = int(getattr(seller, "wallet_balance_kobo", 0) or 0) - fee
            db.commit()
            logger.info(
                "Seller %s absorbed delivery fee %s kobo (delivered order refunded, escrow %s)",
                seller.id,
                fee,
                getattr(escrow, "id", None),
            )
            return
        # Seller record is gone (deleted account) — can't debit the wallet, so
        # flag the fee for manual recovery instead of losing it silently.
        _flag_unrecovered_delivery_fee(db, escrow, fee)
        return
    if order_id:
        from app.services.shipping import shipbubble

        if shipbubble.cancel_shipment(order_id):
            logger.info(
                "Reclaimed delivery fee via Shipbubble cancel (order %s, escrow %s)",
                order_id,
                getattr(escrow, "id", None),
            )
            return
    _flag_unrecovered_delivery_fee(db, escrow, fee)


def _flag_unrecovered_delivery_fee(db: Session, escrow: models.StorefrontOrderEscrow, fee: int) -> None:
    """Record an unreclaimed delivery fee on the escrow so it's queryable for
    manual recovery (not just buried in logs)."""
    try:
        escrow.review_reason = (f"delivery fee {fee} kobo not reclaimed — manual recovery")[:120]
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()
    logger.error(
        "Delivery fee %s kobo NOT reclaimed for escrow %s — needs manual recovery",
        fee,
        getattr(escrow, "id", None),
    )


# ── Payout security (account-takeover protection) ──────────────────────


def on_payout_details_changed(db: Session, user: models.User) -> None:
    """Handle a change to a seller's payout/bank details defensively.

    A hijacked account's first move is to reroute payouts, so on any change we:
    invalidate the cached Paystack recipient (forces re-create from the new
    details), freeze escrow payouts for a cooldown, and alert the owner. This
    never blocks the (legitimate) update itself.
    """
    user.paystack_recipient_code = None
    user.payout_frozen_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        hours=settings.ESCROW_PAYOUT_FREEZE_HOURS_ON_BANK_CHANGE
    )
    db.commit()
    logger.info(
        "Payout details changed for user %s — payouts frozen until %s",
        user.id,
        user.payout_frozen_until,
    )

    # Best-effort owner alert on WhatsApp — never let a messaging hiccup break the flow.
    try:
        if user.phone:
            from app.bot.whatsapp_client import WhatsAppClient

            hours = settings.ESCROW_PAYOUT_FREEZE_HOURS_ON_BANK_CHANGE
            msg = (
                "🔒 SuoOps security alert\n\n"
                "Your payout bank details were just changed. For your safety, "
                f"storefront payouts are paused for {hours} hours.\n\n"
                "If this was NOT you, contact support@suoops.com immediately — "
                "your account may be compromised."
            )
            WhatsAppClient(settings.WHATSAPP_API_KEY).send_text(user.phone, msg)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send payout-change alert to user %s", user.id)


def send_delivery_code(user_phone: str | None, code: str, business_name: str | None) -> None:
    """Best-effort WhatsApp delivery of the buyer-only confirmation code."""
    if not (user_phone and code):
        return
    try:
        from app.bot.whatsapp_client import WhatsAppClient

        shop = business_name or "the store"
        msg = (
            f"🛡️ Your SuoOps release code for your order from {shop} is: {code}\n\n"
            "Give this code to the SELLER only after your order is in your hands "
            "— it releases your payment. The delivery rider never needs it. Your "
            "money is safely held until then."
        )
        WhatsAppClient(settings.WHATSAPP_API_KEY).send_text(user_phone, msg)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to send delivery code to buyer")


# ── Buyer reputation (deter false "not delivered" claims) ──────────────


def _norm_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    try:
        from app.utils.phone import normalize_phone

        return normalize_phone(phone.strip())
    except Exception:  # noqa: BLE001
        return phone.strip()


def _buyer_rep_row(db: Session, phone: str) -> models.BuyerReputation:
    rep = db.query(models.BuyerReputation).filter(models.BuyerReputation.phone == phone).first()
    if not rep:
        rep = models.BuyerReputation(phone=phone)
        db.add(rep)
    return rep


def record_buyer_dispute(db: Session, phone: str | None) -> None:
    """Count that this buyer filed a dispute (any report)."""
    p = _norm_phone(phone)
    if not p:
        return
    rep = _buyer_rep_row(db, p)
    rep.disputes = (rep.disputes or 0) + 1
    db.commit()


def record_buyer_false_dispute(db: Session, phone: str | None) -> None:
    """Count a dispute an admin ruled against the buyer (released to seller).

    Flags the buyer once they cross the abuse threshold.
    """
    p = _norm_phone(phone)
    if not p:
        return
    rep = _buyer_rep_row(db, p)
    rep.false_disputes = (rep.false_disputes or 0) + 1
    rep.last_false_dispute_at = dt.datetime.now(dt.timezone.utc)
    if rep.false_disputes >= settings.ESCROW_BUYER_ABUSE_FLAG_AT:
        rep.flagged = True
    db.commit()


def _decay_buyer_flag(db: Session, rep: models.BuyerReputation) -> None:
    """Clear a stale abuse flag: an honest buyer with no false dispute in the
    decay window is un-flagged (their old losses stop haunting them)."""
    if not rep or not rep.flagged:
        return
    last = rep.last_false_dispute_at
    if last is None:
        return
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    window = dt.timedelta(days=settings.ESCROW_BUYER_ABUSE_DECAY_DAYS)
    if dt.datetime.now(dt.timezone.utc) - last > window:
        rep.flagged = False
        db.commit()


def get_buyer_reputation(db: Session, phone: str | None) -> models.BuyerReputation | None:
    p = _norm_phone(phone)
    if not p:
        return None
    rep = db.query(models.BuyerReputation).filter(models.BuyerReputation.phone == p).first()
    _decay_buyer_flag(db, rep)
    return rep


def get_buyer_reputations_bulk(db: Session, phones) -> dict[str, models.BuyerReputation]:
    """Map normalized phone -> BuyerReputation for many phones in ONE query.

    Read-only (no flag decay) — for admin list/queue views, so rendering N rows
    doesn't fire N reputation queries. Decay still happens on the per-order path.
    """
    norm = {p for p in (_norm_phone(x) for x in phones) if p}
    if not norm:
        return {}
    rows = db.query(models.BuyerReputation).filter(models.BuyerReputation.phone.in_(norm)).all()
    return {r.phone: r for r in rows}


def record_seller_circumvention(db: Session, seller: models.User) -> None:
    """Count a seller order-message that tried to move the deal off-platform
    (masked contact/account, or an off-platform payment push). Enough of them
    flags the seller for review, which also revokes trusted status (is_trusted_seller
    checks flagged_for_review), so their future orders go back under escrow hold.
    """
    seller.circumvention_attempts = (seller.circumvention_attempts or 0) + 1
    if seller.circumvention_attempts >= settings.ESCROW_SELLER_CIRCUMVENTION_FLAG_AT:
        seller.flagged_for_review = True
    db.commit()
