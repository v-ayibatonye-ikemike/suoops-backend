from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CollectionDraftOut(BaseModel):
    id: str
    invoice_id: str
    customer_name: str
    amount: float
    currency: str
    days_overdue: int
    channel: Literal["email", "whatsapp", "unavailable"]
    recipient_masked: str
    subject: str | None = None
    message: str
    priority_score: int = Field(ge=0, le=100)
    priority_level: Literal["low", "medium", "high", "critical"]
    reasons: list[str]
    explanation: str
    ai_generated: bool
    status: Literal["draft", "sent", "dismissed", "failed"]
    created_at: datetime
    sent_at: datetime | None = None
    can_send: bool


class CollectionPrioritiesOut(BaseModel):
    generated_at: datetime
    cooldown_days: int
    drafts: list[CollectionDraftOut]
    total_overdue_amount: float
    eligible_count: int


class CollectionDraftUpdateIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    subject: str | None = Field(default=None, max_length=180)
    message: str = Field(min_length=10, max_length=2000)


class CollectionMetricsOut(BaseModel):
    sent_reminders: int
    recovered_invoices: int
    recovered_amount: float
    recovery_rate: float
