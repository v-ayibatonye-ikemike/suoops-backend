"""Daily curated social-media promotion — features opted-in storefront
products on SuoOps's Facebook Page + Instagram Business account.

See app.services.social_marketing for the full design (eligibility/rotation,
caption generation, Meta Graph API client).
"""
from __future__ import annotations

import logging
from typing import Any

from app.db.session import session_scope
from app.services.social_marketing.service import run_daily_social_promotion
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    name="social_marketing.run_daily_promotion",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=300,
    time_limit=360,
)
def run_daily_promotion() -> dict[str, Any]:
    with session_scope() as db:
        return run_daily_social_promotion(db)
