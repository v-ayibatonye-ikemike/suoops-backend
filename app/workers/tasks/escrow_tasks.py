"""Escrow release worker.

Periodically pays out storefront-order holds whose buyer-protection window has
elapsed with no dispute — Transferring the seller their share (gross − 3%). Each
release is idempotent (deterministic Paystack reference), and a failed release
simply stays 'held' and retries on the next run.
"""

from __future__ import annotations

import datetime as dt
import logging
from typing import Any

from celery import Task

from app.db.session import session_scope
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="escrow.release_due_orders",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def release_due_escrow_orders(self: Task) -> dict[str, Any]:
    """Release all 'held' escrow orders whose window has elapsed."""
    from sqlalchemy import and_, or_

    from app.core.config import settings
    from app.models.models import StorefrontOrderEscrow, User
    from app.services.escrow_service import release_escrow

    released = failed = 0
    with session_scope() as db:
        now = dt.datetime.now(dt.timezone.utc)
        due = (
            db.query(StorefrontOrderEscrow)
            .join(User, StorefrontOrderEscrow.seller_id == User.id)
            .filter(
                StorefrontOrderEscrow.status == "held",
                # Never auto-release collusion/anomaly-flagged orders.
                StorefrontOrderEscrow.held_for_review.is_(False),
                # Skip sellers whose payouts are frozen (post bank-change cooldown).
                (User.payout_frozen_until.is_(None)) | (User.payout_frozen_until <= now),
                # Delivery-aware: a booked courier order must be DELIVERED (or the
                # buyer confirmed) before payout — never pay for goods still in
                # transit just because the payment-time window elapsed.
                or_(
                    StorefrontOrderEscrow.shipbubble_order_id.is_(None),
                    StorefrontOrderEscrow.courier_delivered_at.isnot(None),
                    StorefrontOrderEscrow.confirmed_at.isnot(None),
                ),
                or_(
                    # Cleared (protection window elapsed OR buyer confirmed early)
                    # AND settled (T+1 cadence) → pay the seller out. The settle_at
                    # gate keeps payouts on a next-morning settlement, never same
                    # day, funded by settled collections rather than float.
                    and_(
                        or_(
                            and_(
                                StorefrontOrderEscrow.release_due_at.isnot(None),
                                StorefrontOrderEscrow.release_due_at <= now,
                            ),
                            StorefrontOrderEscrow.confirmed_at.isnot(None),
                        ),
                        or_(
                            StorefrontOrderEscrow.settle_at.is_(None),  # legacy rows
                            StorefrontOrderEscrow.settle_at <= now,
                        ),
                    ),
                    # OR a payout was already initiated (e.g. an admin release) and
                    # is in flight — reconcile/confirm it now, don't wait.
                    StorefrontOrderEscrow.transfer_reference.isnot(None),
                ),
            )
            .limit(200)
            .all()
        )
        if settings.ESCROW_BATCH_PAYOUTS:
            # Consolidate each seller's due orders into ONE payout per rail — the
            # seller sees a single credit and we pay one transfer fee. Orders must
            # share a rail (funds sit in the collecting provider's balance), so we
            # group by (seller, payout rail) before releasing.
            from app.services.escrow_service import payout_rail_for, release_seller_batch

            groups: dict[tuple[int, str], list[StorefrontOrderEscrow]] = {}
            for escrow in due:
                try:
                    rail = payout_rail_for(db, escrow)
                except Exception:  # noqa: BLE001 — fall back to default rail
                    rail = "paystack"
                groups.setdefault((escrow.seller_id, rail), []).append(escrow)
            for (seller_id, rail), group in groups.items():
                try:
                    released += release_seller_batch(db, group, provider_name=rail, reason="window elapsed")
                except Exception as exc:  # noqa: BLE001 — keep going; retry next run
                    failed += len(group)
                    db.rollback()
                    logger.warning("Escrow batch release failed for seller %s: %s", seller_id, exc)
        else:
            for escrow in due:
                try:
                    if release_escrow(db, escrow, reason="window elapsed"):
                        released += 1
                except Exception as exc:  # noqa: BLE001 — keep going; retry next run
                    failed += 1
                    db.rollback()
                    logger.warning("Escrow release failed for %s: %s", escrow.id, exc)

        # Courier orders that never reported delivery within the SLA (their
        # release_due_at cap) → flag for admin review (lost parcel / courier
        # failure) instead of leaving them held forever or paying the seller.
        stuck = (
            db.query(StorefrontOrderEscrow)
            .filter(
                StorefrontOrderEscrow.status == "held",
                StorefrontOrderEscrow.held_for_review.is_(False),
                StorefrontOrderEscrow.shipbubble_order_id.isnot(None),
                StorefrontOrderEscrow.courier_delivered_at.is_(None),
                StorefrontOrderEscrow.confirmed_at.is_(None),
                StorefrontOrderEscrow.release_due_at.isnot(None),
                StorefrontOrderEscrow.release_due_at <= now,
            )
            .limit(200)
            .all()
        )
        for e in stuck:
            e.held_for_review = True
            e.review_reason = "courier not delivered within SLA"
        if stuck:
            db.commit()
            logger.warning("Flagged %d undelivered courier orders for review", len(stuck))

    result = {"checked": released + failed, "released": released, "failed": failed}
    logger.info("Escrow release run: %s", result)
    return result


@celery_app.task(
    bind=True,
    name="escrow.cancel_stale_pending",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def cancel_stale_pending_orders(self: Task) -> dict[str, Any]:
    """Cancel abandoned unpaid storefront orders.

    A 'pending' escrow means the buyer never paid (payment confirmation flips it
    to 'held'). Such rows cost the seller nothing, but left around they clutter
    order/admin views — so we cancel ones older than the configured TTL.
    """
    from app.core.config import settings
    from app.models.models import StorefrontOrderEscrow

    canceled = 0
    with session_scope() as db:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=settings.ESCROW_PENDING_ORDER_TTL_HOURS)
        rows = (
            db.query(StorefrontOrderEscrow)
            .filter(
                StorefrontOrderEscrow.status == "pending",
                StorefrontOrderEscrow.created_at < cutoff,
            )
            .limit(500)
            .all()
        )
        for e in rows:
            e.status = "canceled"
            canceled += 1
        if canceled:
            db.commit()
    logger.info("Stale pending escrow cleanup: canceled %d", canceled)
    return {"canceled": canceled}
