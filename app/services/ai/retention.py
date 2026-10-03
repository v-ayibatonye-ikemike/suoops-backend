from __future__ import annotations

import datetime as dt

from sqlalchemy.orm import Session

from app.models.ai_models import (
    AICollectionDraft,
    AICopilotBriefing,
    AIFeedback,
    AIProposedAction,
    AIUsageEvent,
)


def purge_expired_ai_data(
    db: Session,
    *,
    now: dt.datetime | None = None,
    dry_run: bool = False,
) -> dict[str, int]:
    current = now or dt.datetime.now(dt.timezone.utc)
    operational_cutoff = current - dt.timedelta(days=548)
    briefing_cutoff = current - dt.timedelta(days=30)
    targets = {
        "usage_events": (AIUsageEvent, AIUsageEvent.created_at < operational_cutoff),
        "feedback": (AIFeedback, AIFeedback.created_at < operational_cutoff),
        "briefings": (AICopilotBriefing, AICopilotBriefing.generated_at < briefing_cutoff),
        "proposed_actions": (AIProposedAction, AIProposedAction.created_at < operational_cutoff),
        "collection_drafts": (AICollectionDraft, AICollectionDraft.created_at < operational_cutoff),
    }
    counts: dict[str, int] = {}
    for key, (model, predicate) in targets.items():
        query = db.query(model).filter(predicate)
        counts[key] = query.count() if dry_run else query.delete(synchronize_session=False)
    if not dry_run:
        db.commit()
    return counts
