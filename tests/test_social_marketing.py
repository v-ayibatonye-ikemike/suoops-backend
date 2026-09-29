"""Tests for the social-marketing auto-promotion feature.

Covers: eligibility/rotation logic, caption fallback (no LLM), and the
orchestration service with a mocked Meta client (no real network calls).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.inventory_models import Product
from app.models.models import SocialPost, User
from app.services.social_marketing.caption_service import build_storefront_link, generate_caption
from app.services.social_marketing.eligibility_service import get_eligible_products
from app.services.social_marketing.meta_client import MetaPostingError
from app.services.social_marketing.service import run_daily_social_promotion

engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()


def _make_user(db_session, *, opt_in: bool = True, store_status: str = "active") -> User:
    suffix = uuid.uuid4().hex[:8]
    user = User(
        phone=f"+239{suffix}",
        name="SocialTestUser",
        business_name="Corner Shop",
        email=f"social-{suffix}@example.com",
        storefront_enabled=True,
        storefront_slug=f"shop-{suffix}",
        store_status=store_status,
        social_promotion_opt_in=opt_in,
    )
    db_session.add(user)
    db_session.commit()
    return user


def _make_product(
    db_session,
    user: User,
    *,
    name: str = "Bag of rice",
    image_url: str | None = "http://img/rice.jpg",
    is_active: bool = True,
    exclude_from_social: bool = False,
    track_stock: bool = True,
    quantity_in_stock: int = 10,
) -> Product:
    suffix = uuid.uuid4().hex[:6]
    product = Product(
        user_id=user.id,
        sku=f"SKU-{suffix}",
        name=name,
        description="A very good product",
        image_url=image_url,
        selling_price=Decimal("5000"),
        is_active=is_active,
        exclude_from_social=exclude_from_social,
        track_stock=track_stock,
        quantity_in_stock=quantity_in_stock,
    )
    db_session.add(product)
    db_session.commit()
    return product


class _FakeMetaClient:
    """Stub Meta client — no real network calls."""

    def __init__(self, *, fb_fails: bool = False, ig_fails: bool = False):
        self.fb_fails = fb_fails
        self.ig_fails = ig_fails
        self.facebook_calls: list[tuple[str, str]] = []
        self.instagram_calls: list[tuple[str, str]] = []

    def post_to_facebook_page(self, image_url: str, caption: str) -> str:
        self.facebook_calls.append((image_url, caption))
        if self.fb_fails:
            raise MetaPostingError("simulated Facebook failure")
        return "fb_post_123"

    def post_to_instagram(self, image_url: str, caption: str) -> str:
        self.instagram_calls.append((image_url, caption))
        if self.ig_fails:
            raise MetaPostingError("simulated Instagram failure")
        return "ig_media_456"


# ── Caption service ────────────────────────────────────────────────────


def test_caption_falls_back_without_openai_key(monkeypatch, db_session):
    monkeypatch.setattr("app.services.social_marketing.caption_service.settings.OPENAI_API_KEY", None)
    user = _make_user(db_session)
    product = _make_product(db_session, user)
    link = build_storefront_link(user.storefront_slug, "facebook")

    caption = generate_caption(product, "Corner Shop", link)

    assert product.name in caption
    assert link in caption
    assert "utm_source=facebook" in link


# ── Eligibility / curation ──────────────────────────────────────────────
# NOTE: these tests share one module-level in-memory SQLite DB with every
# other test in this file (the same convention used across this test suite —
# see test_business_snapshot.py etc.), and get_eligible_products() is a
# platform-wide query by design (no user_id scoping — it has to look across
# all sellers to rotate fairly). So assertions check membership/order of
# THIS test's own products within the result, never exact list equality
# against the whole (possibly polluted-by-other-tests) result set.


def test_eligibility_excludes_non_opted_in_stores(db_session):
    user = _make_user(db_session, opt_in=False)
    product = _make_product(db_session, user)

    eligible_ids = {p.id for p in get_eligible_products(db_session, limit=1000)}

    assert product.id not in eligible_ids


def test_eligibility_excludes_suspended_stores(db_session):
    user = _make_user(db_session, store_status="suspended")
    product = _make_product(db_session, user)

    eligible_ids = {p.id for p in get_eligible_products(db_session, limit=1000)}

    assert product.id not in eligible_ids


def test_eligibility_excludes_products_without_image_or_excluded(db_session):
    user = _make_user(db_session)
    no_photo = _make_product(db_session, user, name="No photo", image_url=None)
    opted_out = _make_product(db_session, user, name="Opted out", exclude_from_social=True)
    included = _make_product(db_session, user, name="Eligible one")

    eligible_ids = {p.id for p in get_eligible_products(db_session, limit=1000)}

    assert included.id in eligible_ids
    assert no_photo.id not in eligible_ids
    assert opted_out.id not in eligible_ids


def test_eligibility_excludes_out_of_stock_tracked_items(db_session):
    user = _make_user(db_session)
    out_of_stock = _make_product(db_session, user, name="Out of stock", track_stock=True, quantity_in_stock=0)
    # Untracked (service/digital) items default to 0 stock but should NOT be excluded.
    untracked = _make_product(db_session, user, name="Service item", track_stock=False, quantity_in_stock=0)

    eligible_ids = {p.id for p in get_eligible_products(db_session, limit=1000)}

    assert untracked.id in eligible_ids
    assert out_of_stock.id not in eligible_ids


def test_eligibility_skips_recently_featured_products(db_session):
    user = _make_user(db_session)
    product = _make_product(db_session, user)
    db_session.add(
        SocialPost(
            product_id=product.id,
            user_id=user.id,
            platform="facebook",
            status="posted",
            caption="already posted",
            image_url=product.image_url,
            utm_link="http://x",
            created_at=datetime.now(timezone.utc) - timedelta(days=2),
        )
    )
    db_session.commit()

    eligible_ids = {p.id for p in get_eligible_products(db_session, limit=1000)}

    assert product.id not in eligible_ids


def test_eligibility_rotates_never_featured_sellers_first(db_session):
    seller_a = _make_user(db_session)
    seller_b = _make_user(db_session)
    product_a = _make_product(db_session, seller_a, name="A's product")
    product_b = _make_product(db_session, seller_b, name="B's product")

    # Seller A was featured recently (but a DIFFERENT product, so A's new
    # product isn't blocked by the per-product cooldown — only by rotation).
    other_product_a = _make_product(db_session, seller_a, name="A's older product")
    db_session.add(
        SocialPost(
            product_id=other_product_a.id,
            user_id=seller_a.id,
            platform="facebook",
            status="posted",
            caption="x",
            image_url="http://img",
            utm_link="http://x",
            created_at=datetime.now(timezone.utc) - timedelta(days=5),
        )
    )
    db_session.commit()

    # Pull a large batch and find each product's relative position — seller B
    # (never featured) must rank ahead of seller A (featured 5 days ago).
    eligible_ids = [p.id for p in get_eligible_products(db_session, limit=1000)]

    assert product_a.id in eligible_ids
    assert product_b.id in eligible_ids
    assert eligible_ids.index(product_b.id) < eligible_ids.index(product_a.id)


# ── Orchestration ────────────────────────────────────────────────────────


def test_run_daily_promotion_records_posted_and_failed_independently(monkeypatch, db_session):
    user = _make_user(db_session)
    product = _make_product(db_session, user)

    # Isolate this test from the eligibility/rotation layer entirely — with
    # a fixed daily batch size and many other tests sharing this DB, THIS
    # test's product isn't guaranteed to make the cut on its own merits.
    # What's under test here is the posting/recording behaviour, not
    # selection, so force the batch to be exactly this one product.
    monkeypatch.setattr(
        "app.services.social_marketing.service.get_eligible_products",
        lambda db, limit=None: [product],
    )

    client = _FakeMetaClient(ig_fails=True)
    summary = run_daily_social_promotion(db_session, client=client)

    assert summary == {"products_selected": 1, "attempted": 2, "posted": 1, "failed": 1}

    posts = db_session.query(SocialPost).filter(SocialPost.product_id == product.id).all()
    by_platform = {p.platform: p for p in posts}
    assert by_platform["facebook"].status == "posted"
    assert by_platform["facebook"].external_post_id == "fb_post_123"
    assert by_platform["instagram"].status == "failed"
    assert by_platform["instagram"].error_message


def test_run_daily_promotion_never_exceeds_the_daily_limit(db_session):
    """However much is eligible platform-wide (including leftovers from other
    tests sharing this DB), a single run never attempts more than
    2 * SOCIAL_PROMOTION_DAILY_LIMIT platform posts (facebook + instagram
    per selected product) — the whole point of curating a batch, not a
    firehose."""
    from app.core.config import settings

    user = _make_user(db_session)
    for i in range(settings.SOCIAL_PROMOTION_DAILY_LIMIT + 5):
        _make_product(db_session, user, name=f"Bulk product {i}")

    client = _FakeMetaClient()
    summary = run_daily_social_promotion(db_session, client=client)

    assert summary["attempted"] <= 2 * settings.SOCIAL_PROMOTION_DAILY_LIMIT
    assert summary["products_selected"] <= settings.SOCIAL_PROMOTION_DAILY_LIMIT
