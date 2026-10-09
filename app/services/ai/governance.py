from __future__ import annotations

import datetime as dt
import hashlib
from collections import defaultdict

from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.ai_models import AIFeatureControl, AIFeedback, AITenantPreference, AIUsageEvent

FEATURE_REGISTRY: dict[str, str] = {
    "web_navigation": "Web navigation and guided tasks",
    "daily_briefing": "Commerce Copilot narratives",
    "collection_reminder_draft": "Collections reminder tone",
    "inventory_advice_explanation": "Inventory Adviser explanations",
    "storefront_product_copy": "Storefront product copy",
    "buyer_shopping_assistant": "Buyer shopping ranking",
    "dispute_evidence_summary": "Dispute evidence summaries",
}


def require_known_feature(feature: str) -> str:
    key = feature.strip()
    if key not in FEATURE_REGISTRY:
        raise ValueError("Unknown AI feature")
    return key


def ai_access_allowed(db: Session, *, feature: str, data_owner_id: int) -> tuple[bool, str | None]:
    key = require_known_feature(feature)
    preference = (
        db.query(AITenantPreference)
        .filter(AITenantPreference.data_owner_id == data_owner_id)
        .first()
    )
    if preference and not preference.enabled:
        return False, "tenant_ai_disabled"
    if preference and preference.feature_overrides.get(key) is False:
        return False, "tenant_feature_disabled"

    control = db.query(AIFeatureControl).filter(AIFeatureControl.feature == key).first()
    if not control:
        return True, None
    if not control.enabled:
        return False, "feature_kill_switch"
    allowlist = {int(owner_id) for owner_id in (control.allowlisted_owner_ids or [])}
    if data_owner_id in allowlist:
        return True, None
    bucket = int(hashlib.sha256(f"{key}:{data_owner_id}".encode()).hexdigest()[:8], 16) % 100
    if bucket >= control.rollout_percent:
        return False, "outside_rollout"
    return True, None


def tenant_preferences(db: Session, data_owner_id: int) -> dict:
    preference = (
        db.query(AITenantPreference)
        .filter(AITenantPreference.data_owner_id == data_owner_id)
        .first()
    )
    return {
        "enabled": preference.enabled if preference else True,
        "feature_overrides": dict(preference.feature_overrides or {}) if preference else {},
        "available_features": FEATURE_REGISTRY,
        "updated_at": preference.updated_at if preference else None,
    }


def update_tenant_preferences(
    db: Session,
    *,
    data_owner_id: int,
    actor_user_id: int,
    enabled: bool,
    feature_overrides: dict[str, bool],
) -> dict:
    cleaned = {require_known_feature(key): bool(value) for key, value in feature_overrides.items()}
    preference = (
        db.query(AITenantPreference)
        .filter(AITenantPreference.data_owner_id == data_owner_id)
        .with_for_update()
        .first()
    )
    if not preference:
        preference = AITenantPreference(
            data_owner_id=data_owner_id,
            updated_by_user_id=actor_user_id,
        )
        db.add(preference)
    preference.enabled = enabled
    preference.feature_overrides = cleaned
    preference.updated_by_user_id = actor_user_id
    preference.updated_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(preference)
    return tenant_preferences(db, data_owner_id)


def record_feedback(
    db: Session,
    *,
    data_owner_id: int,
    actor_user_id: int,
    feature: str,
    sentiment: str,
    reason_code: str | None,
    comment: str | None,
    context_id: str | None,
) -> None:
    key = require_known_feature(feature)
    db.add(
        AIFeedback(
            data_owner_id=data_owner_id,
            actor_user_id=actor_user_id,
            feature=key,
            sentiment=sentiment,
            reason_code=reason_code.strip() if reason_code else None,
            comment=comment.strip() if comment else None,
            context_id=context_id.strip() if context_id else None,
        )
    )
    db.commit()


def update_feature_control(
    db: Session,
    *,
    feature: str,
    admin_user_id: int,
    enabled: bool,
    rollout_percent: int,
    allowlisted_owner_ids: list[int],
    reason: str | None,
) -> dict:
    key = require_known_feature(feature)
    control = (
        db.query(AIFeatureControl)
        .filter(AIFeatureControl.feature == key)
        .with_for_update()
        .first()
    )
    if not control:
        control = AIFeatureControl(feature=key)
        db.add(control)
    control.enabled = enabled
    control.rollout_percent = rollout_percent
    control.allowlisted_owner_ids = sorted(set(allowlisted_owner_ids))
    control.reason = reason.strip() if reason else None
    control.updated_by_admin_user_id = admin_user_id
    control.updated_at = dt.datetime.now(dt.timezone.utc)
    db.commit()
    db.refresh(control)
    return _control_out(control)


def governance_overview(db: Session, *, days: int) -> dict:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)
    rows = (
        db.query(
            AIUsageEvent.feature,
            func.count(AIUsageEvent.id),
            func.sum(case((AIUsageEvent.status == "succeeded", 1), else_=0)),
            func.sum(case((AIUsageEvent.status == "failed", 1), else_=0)),
            func.sum(case((AIUsageEvent.status == "blocked", 1), else_=0)),
            func.coalesce(func.sum(AIUsageEvent.input_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.output_tokens), 0),
            func.coalesce(func.sum(AIUsageEvent.estimated_cost_usd), 0),
            func.avg(AIUsageEvent.duration_ms),
        )
        .filter(AIUsageEvent.created_at >= since)
        .group_by(AIUsageEvent.feature)
        .all()
    )
    feedback_rows = (
        db.query(
            AIFeedback.feature,
            AIFeedback.sentiment,
            func.count(AIFeedback.id),
        )
        .filter(AIFeedback.created_at >= since)
        .group_by(AIFeedback.feature, AIFeedback.sentiment)
        .all()
    )
    feedback: dict[str, dict[str, int]] = defaultdict(lambda: {"positive": 0, "negative": 0})
    for feature, sentiment, count in feedback_rows:
        feedback[feature][sentiment] = int(count)
    metrics_by_feature = {row[0]: row for row in rows}
    metrics = []
    total_operations = 0
    total_cost_usd = 0.0
    for feature, label in FEATURE_REGISTRY.items():
        row = metrics_by_feature.get(feature)
        operations = int(row[1]) if row else 0
        succeeded = int(row[2] or 0) if row else 0
        failed = int(row[3] or 0) if row else 0
        blocked = int(row[4] or 0) if row else 0
        attempted_provider_calls = succeeded + failed
        estimated_cost_usd = float(row[7]) if row else 0.0
        total_operations += operations
        total_cost_usd += estimated_cost_usd
        metrics.append(
            {
                "feature": feature,
                "label": label,
                "operations": operations,
                "succeeded": succeeded,
                "failed": failed,
                "blocked": blocked,
                "success_rate": (
                    round(succeeded / attempted_provider_calls * 100, 1)
                    if attempted_provider_calls
                    else 0.0
                ),
                "input_tokens": int(row[5]) if row else 0,
                "output_tokens": int(row[6]) if row else 0,
                "estimated_cost_usd": estimated_cost_usd,
                "average_duration_ms": round(float(row[8]), 1) if row and row[8] is not None else None,
                "positive_feedback": feedback[feature]["positive"],
                "negative_feedback": feedback[feature]["negative"],
            }
        )
    controls_by_feature = {
        control.feature: control
        for control in db.query(AIFeatureControl).filter(AIFeatureControl.feature.in_(FEATURE_REGISTRY)).all()
    }
    controls = [
        _control_out(controls_by_feature[feature]) if feature in controls_by_feature else _default_control(feature)
        for feature in FEATURE_REGISTRY
    ]
    return {
        "period_days": days,
        "master_enabled": settings.AI_ENABLED,
        "provider": settings.AI_PROVIDER,
        "default_model": settings.AI_DEFAULT_MODEL,
        "total_operations": total_operations,
        "total_cost_usd": round(total_cost_usd, 6),
        "disabled_tenants": db.query(func.count(AITenantPreference.id))
        .filter(AITenantPreference.enabled.is_(False))
        .scalar()
        or 0,
        "features": metrics,
        "controls": controls,
    }


def _control_out(control: AIFeatureControl) -> dict:
    return {
        "feature": control.feature,
        "label": FEATURE_REGISTRY[control.feature],
        "enabled": control.enabled,
        "rollout_percent": control.rollout_percent,
        "allowlisted_owner_ids": list(control.allowlisted_owner_ids or []),
        "reason": control.reason,
        "updated_at": control.updated_at,
    }


def _default_control(feature: str) -> dict:
    return {
        "feature": feature,
        "label": FEATURE_REGISTRY[feature],
        "enabled": True,
        "rollout_percent": 100,
        "allowlisted_owner_ids": [],
        "reason": None,
        "updated_at": None,
    }
