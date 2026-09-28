"""Which storefront products get featured today, and in what order.

Design goals (see the module docstring in __init__.py for the full picture):
- Opt-in only — never feature a product without the seller's consent.
- Fair rotation — sellers who haven't been featured recently go first, so
  the same few power-sellers don't dominate every day's batch.
- No repeats — the SAME product isn't re-featured within a cooldown window.
- Only real, sellable products — active, in stock (when stock is tracked),
  has a photo, storefront actually live (not suspended/delisted).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, nullsfirst, or_
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.inventory_models import Product
from app.models.models import SocialPost, User


def get_eligible_products(db: Session, limit: int | None = None) -> list[Product]:
    """Return up to `limit` products to feature today, best candidates first."""
    limit = limit or settings.SOCIAL_PROMOTION_DAILY_LIMIT
    cooldown_cutoff = datetime.now(timezone.utc) - timedelta(
        days=settings.SOCIAL_PROMOTION_REPOST_COOLDOWN_DAYS
    )

    # Products already featured within the cooldown window — skip these so
    # the same item doesn't show up in the feed over and over.
    recently_featured_product_ids = db.query(SocialPost.product_id).filter(
        SocialPost.created_at >= cooldown_cutoff,
        SocialPost.status == "posted",
    )

    # Last time each seller had ANY product featured — drives rotation.
    # A seller who has never been featured (no row at all) sorts first via
    # the outer join + nullsfirst() below.
    last_featured_subq = (
        db.query(
            SocialPost.user_id.label("user_id"),
            func.max(SocialPost.created_at).label("last_featured_at"),
        )
        .filter(SocialPost.status == "posted")
        .group_by(SocialPost.user_id)
        .subquery()
    )

    rows = (
        db.query(Product)
        .join(User, User.id == Product.user_id)
        .outerjoin(last_featured_subq, last_featured_subq.c.user_id == Product.user_id)
        .filter(
            User.storefront_enabled.is_(True),
            User.store_status == "active",
            User.social_promotion_opt_in.is_(True),
            Product.is_active.is_(True),
            Product.exclude_from_social.is_(False),
            Product.image_url.isnot(None),
            # Only require in-stock when the product actually tracks stock —
            # service/digital items default to 0 and would be wrongly excluded.
            or_(Product.track_stock.is_(False), Product.quantity_in_stock > 0),
            Product.id.notin_(recently_featured_product_ids),
        )
        .order_by(nullsfirst(last_featured_subq.c.last_featured_at), Product.id.asc())
        .limit(limit)
        .all()
    )
    return rows
