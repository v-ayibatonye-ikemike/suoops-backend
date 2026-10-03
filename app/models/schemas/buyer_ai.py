from __future__ import annotations

from pydantic import BaseModel, Field


class BuyerShoppingRequest(BaseModel):
    query: str = Field(min_length=2, max_length=300)
    cart_product_ids: list[int] = Field(default_factory=list, max_length=20)


class BuyerProductMatchOut(BaseModel):
    product_id: int
    name: str
    price: float
    original_price: float
    discount_percent: int
    category: str | None
    fulfilment_type: str
    reason: str


class BuyerShoppingResponse(BaseModel):
    answer: str
    matches: list[BuyerProductMatchOut]
    detected_budget: float | None
    ai_ranked: bool
    notice: str | None = None
