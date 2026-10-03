from __future__ import annotations

import datetime as dt

from pydantic import BaseModel, Field


class AITenantPreferencesUpdateIn(BaseModel):
    enabled: bool
    feature_overrides: dict[str, bool] = Field(default_factory=dict)


class AITenantPreferencesOut(BaseModel):
    enabled: bool
    feature_overrides: dict[str, bool]
    available_features: dict[str, str]
    updated_at: dt.datetime | None = None


class AIFeedbackIn(BaseModel):
    feature: str = Field(min_length=1, max_length=80)
    sentiment: str = Field(pattern="^(positive|negative)$")
    reason_code: str | None = Field(default=None, max_length=40)
    comment: str | None = Field(default=None, max_length=500)
    context_id: str | None = Field(default=None, max_length=100)


class AIFeedbackOut(BaseModel):
    accepted: bool


class AIFeatureControlUpdateIn(BaseModel):
    enabled: bool
    rollout_percent: int = Field(ge=0, le=100)
    allowlisted_owner_ids: list[int] = Field(default_factory=list, max_length=200)
    reason: str | None = Field(default=None, max_length=300)


class AIFeatureControlOut(BaseModel):
    feature: str
    label: str
    enabled: bool
    rollout_percent: int
    allowlisted_owner_ids: list[int]
    reason: str | None
    updated_at: dt.datetime | None


class AIFeatureMetricOut(BaseModel):
    feature: str
    label: str
    operations: int
    succeeded: int
    failed: int
    blocked: int
    success_rate: float
    input_tokens: int
    output_tokens: int
    estimated_cost_usd: float
    average_duration_ms: float | None
    positive_feedback: int
    negative_feedback: int


class AIGovernanceOverviewOut(BaseModel):
    period_days: int
    master_enabled: bool
    provider: str
    default_model: str
    total_operations: int
    total_cost_usd: float
    disabled_tenants: int
    features: list[AIFeatureMetricOut]
    controls: list[AIFeatureControlOut]
