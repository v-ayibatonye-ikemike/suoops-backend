from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_models import AIUsageEvent
from app.models.models import User


def month_window(now: dt.datetime | None = None) -> tuple[dt.datetime, dt.datetime]:
    current = now or dt.datetime.now(dt.timezone.utc)
    start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def reserve_operation(db: Session, event: AIUsageEvent, limit: int) -> None:
    """Serialize reservations per data owner so concurrent requests cannot exceed quota."""
    db.query(User.id).filter(User.id == event.data_owner_id).with_for_update().one()
    start, _ = month_window()
    processing_cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=15)
    consumed = (
        db.query(func.count(AIUsageEvent.id))
        .filter(
            AIUsageEvent.data_owner_id == event.data_owner_id,
            AIUsageEvent.counts_toward_quota.is_(True),
            AIUsageEvent.created_at >= start,
            or_(
                AIUsageEvent.status == "succeeded",
                (AIUsageEvent.status == "processing") & (AIUsageEvent.created_at >= processing_cutoff),
            ),
        )
        .scalar()
        or 0
    )
    if consumed >= limit:
        raise ValueError("monthly_ai_quota_exceeded")
    db.add(event)
    db.commit()
    db.refresh(event)


def estimate_cost_usd(input_tokens: int, output_tokens: int) -> Decimal:
    input_cost = Decimal(input_tokens) * Decimal(str(settings.AI_INPUT_COST_PER_MILLION_USD)) / Decimal(1_000_000)
    output_cost = (
        Decimal(output_tokens) * Decimal(str(settings.AI_OUTPUT_COST_PER_MILLION_USD)) / Decimal(1_000_000)
    )
    return (input_cost + output_cost).quantize(Decimal("0.000001"))


def usage_summary(db: Session, data_owner_id: int) -> dict:
    start, end = month_window()
    rows = (
        db.query(
            AIUsageEvent.feature,
            func.count(AIUsageEvent.id).label("operations"),
            func.coalesce(func.sum(AIUsageEvent.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(AIUsageEvent.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0).label("cost"),
        )
        .filter(
            AIUsageEvent.data_owner_id == data_owner_id,
            AIUsageEvent.counts_toward_quota.is_(True),
            AIUsageEvent.status == "succeeded",
            AIUsageEvent.created_at >= start,
            AIUsageEvent.created_at < end,
        )
        .group_by(AIUsageEvent.feature)
        .order_by(func.count(AIUsageEvent.id).desc(), AIUsageEvent.feature)
        .all()
    )
    used = sum(int(row.operations) for row in rows)
    limit = settings.AI_MONTHLY_INCLUDED_OPERATIONS
    return {
        "enabled": settings.AI_ENABLED,
        "period_start": start,
        "period_end": end,
        "included_operations": limit,
        "used_operations": used,
        "remaining_operations": max(0, limit - used),
        "features": [
            {
                "feature": row.feature,
                "operations": int(row.operations),
                "input_tokens": int(row.input_tokens),
                "output_tokens": int(row.output_tokens),
                "estimated_cost_usd": float(row.cost),
            }
            for row in rows
        ],
    }
