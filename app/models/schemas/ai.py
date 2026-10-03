from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AIUsageFeatureOut(BaseModel):
    feature: str
    operations: int
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float


class AIUsageOut(BaseModel):
    enabled: bool
    period_start: datetime
    period_end: datetime
    included_operations: int = Field(ge=0)
    used_operations: int = Field(ge=0)
    remaining_operations: int = Field(ge=0)
    features: list[AIUsageFeatureOut]


class AIAvailabilityOut(BaseModel):
    enabled: bool
    provider: str
    default_model: str
    structured_outputs: bool = True
    prompt_storage_enabled: bool = False
