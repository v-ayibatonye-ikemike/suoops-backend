"""Orchestrates the daily curated social-promotion run: pick eligible
products, generate captions, post to Facebook + Instagram, record results.

Each (product, platform) attempt is independent — one platform failing (or
being unconfigured) never blocks or hides the other's result. Every attempt,
successful or not, is recorded as a SocialPost row for auditability.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.orm import Session

from app.models.models import SocialPost

from .caption_service import build_storefront_link, generate_caption
from .eligibility_service import get_eligible_products
from .meta_client import MetaGraphClient, MetaPostingError

logger = logging.getLogger(__name__)

_PLATFORMS = ("facebook", "instagram")


def run_daily_social_promotion(db: Session, client: MetaGraphClient | None = None) -> dict[str, Any]:
    """Run today's curated batch. Returns a summary dict for logging/tests."""
    client = client or MetaGraphClient()
    products = get_eligible_products(db)

    summary: dict[str, Any] = {"products_selected": len(products), "attempted": 0, "posted": 0, "failed": 0}

    for product in products:
        user = product.user
        if not user or not user.storefront_slug:
            continue  # shouldn't happen given the eligibility filter, but stay defensive
        business_name = user.business_name or user.name

        for platform in _PLATFORMS:
            link = build_storefront_link(user.storefront_slug, platform)
            caption = generate_caption(product, business_name, link)
            summary["attempted"] += 1

            try:
                if platform == "facebook":
                    external_id = client.post_to_facebook_page(product.image_url, caption)
                else:
                    external_id = client.post_to_instagram(product.image_url, caption)
                db.add(
                    SocialPost(
                        product_id=product.id,
                        user_id=user.id,
                        platform=platform,
                        status="posted",
                        caption=caption,
                        image_url=product.image_url,
                        utm_link=link,
                        external_post_id=external_id,
                    )
                )
                summary["posted"] += 1
            except MetaPostingError as exc:
                logger.warning(
                    "Social post failed | product=%s platform=%s error=%s",
                    product.id,
                    platform,
                    exc,
                )
                db.add(
                    SocialPost(
                        product_id=product.id,
                        user_id=user.id,
                        platform=platform,
                        status="failed",
                        caption=caption,
                        image_url=product.image_url,
                        utm_link=link,
                        error_message=str(exc),
                    )
                )
                summary["failed"] += 1
            db.commit()

    logger.info("Daily social promotion run complete: %s", summary)
    return summary
