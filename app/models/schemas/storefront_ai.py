from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class StorefrontFunnelOut(BaseModel):
    views_lifetime: int
    orders_30_days: int
    paid_orders_30_days: int
    abandoned_orders_30_days: int
    lifetime_conversion_rate: float
    explanation: str


class StorefrontListingAdviceOut(BaseModel):
    product_id: int
    product_name: str
    quality_score: int
    issues: list[str]
    units_sold_30_days: int
    recommendation: Literal["improve_listing", "feature", "promote", "healthy", "out_of_stock"]
    explanation: str
    current_discount_percent: int
    max_safe_discount_percent: int
    suggested_discount_percent: int
    featured: bool
    bundle_label: str | None = None


class StorefrontBundleSuggestionOut(BaseModel):
    title: str
    product_ids: list[int]
    product_names: list[str]
    supporting_orders: int
    reason: str


class StorefrontReengagementDraftOut(BaseModel):
    notification_id: int
    product_id: int
    product_name: str
    recipient_masked: str
    message: str


class StorefrontAdviceOut(BaseModel):
    generated_at: datetime
    quality_score: int
    headline: str
    summary: str
    funnel: StorefrontFunnelOut
    listings: list[StorefrontListingAdviceOut]
    bundle_suggestions: list[StorefrontBundleSuggestionOut]
    reengagement_drafts: list[StorefrontReengagementDraftOut]


class StorefrontCopyDraftOut(BaseModel):
    product_id: int
    description: str
    ai_generated: bool
    generation_notice: str | None = None


class StorefrontCopyApplyIn(BaseModel):
    description: str = Field(min_length=20, max_length=800)


class StorefrontMerchandisingIn(BaseModel):
    product_ids: list[int] = Field(max_length=5)


class StorefrontPromotionIn(BaseModel):
    discount_percent: int = Field(ge=0, le=20)


class StorefrontBundleIn(BaseModel):
    product_ids: list[int] = Field(min_length=2, max_length=5)
    title: str = Field(min_length=3, max_length=120)
    active: bool = True


class StorefrontProductActionOut(BaseModel):
    product_id: int
    product_name: str
    description: str | None = None
    featured: bool
    discount_percent: int
    bundle_label: str | None = None


class StorefrontMerchandisingOut(BaseModel):
    products: list[StorefrontProductActionOut]
    notice: str
