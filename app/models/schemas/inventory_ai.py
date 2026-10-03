from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class InventoryRecommendationOut(BaseModel):
    product_id: int
    product_name: str
    sku: str
    unit: str
    current_stock: int
    incoming_stock: int
    units_sold_30_days: int
    daily_sales_velocity: float
    days_of_stock: float | None
    demand_trend: Literal["rising", "steady", "falling", "no_sales"]
    recommendation: Literal["reorder_now", "watch", "healthy", "slow_stock", "insufficient_data"]
    recommended_order_quantity: int
    estimated_order_cost: float | None
    explanation: str
    reason_codes: list[str]


class InventoryAdviceOut(BaseModel):
    generated_at: datetime
    lookback_days: int
    target_cover_days: int
    headline: str
    summary: str
    ai_generated: bool
    generation_notice: str | None = None
    reorder_count: int
    slow_stock_count: int
    estimated_reorder_cost: float
    recommendations: list[InventoryRecommendationOut]


class InventoryPurchaseOrderIn(BaseModel):
    product_ids: list[int] = Field(min_length=1, max_length=50)


class InventoryPurchaseOrderLineOut(BaseModel):
    product_id: int
    product_name: str
    quantity: int
    unit_cost: float | None
    total_cost: float | None


class InventoryPurchaseOrderOut(BaseModel):
    id: int
    order_number: str
    status: Literal["draft"]
    total_amount: float
    lines: list[InventoryPurchaseOrderLineOut]
    created: bool
    notice: str
