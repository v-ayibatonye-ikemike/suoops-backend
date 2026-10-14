import datetime as dt
import logging

from app.db.session import session_scope
from app.models.models import User
from app.services.merchant_retention import (
    UTC,
    merchant_query,
    refresh_lifecycle,
    retention_facts,
    send_retention_contact,
)
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(name="retention.refresh_merchants", soft_time_limit=300, time_limit=360)
def refresh_merchants() -> dict:
    """Refresh every business in bounded batches; never bulk-enroll or implicitly send."""
    after_id = 0
    stats = {"processed": 0, "accepted": 0, "unknown": 0, "skipped": 0, "failed": 0}
    while True:
        with session_scope() as db:
            ids = [row.id for row in merchant_query(db).filter(User.id > after_id).order_by(User.id).limit(50).all()]
        if not ids:
            break
        for user_id in ids:
            try:
                with session_scope() as db:
                    now = dt.datetime.now(UTC)
                    user = merchant_query(db).filter_by(id=user_id).with_for_update().first()
                    if user is None:
                        continue
                    refresh_lifecycle(db, retention_facts(db, user, now), now)
                    db.commit()
                    outcome = send_retention_contact(db, user_id, now)
                    stats["accepted"] += outcome == "accepted"
                    stats["unknown"] += outcome == "unknown"
                    stats["skipped"] += outcome not in {"accepted", "unknown"}
                    stats["processed"] += 1
            except Exception:
                logger.exception("Retention processing failed for business %s", user_id)
                stats["failed"] += 1
        after_id = ids[-1]
    return stats
