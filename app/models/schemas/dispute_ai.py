from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class DisputeTimelineEventOut(BaseModel):
    occurred_at: dt.datetime
    event: str
    detail: str
    source: str


class DisputeEvidenceOut(BaseModel):
    label: str
    detail: str
    source: str


class DisputeAssistantOut(BaseModel):
    escrow_id: int
    invoice_id: str | None
    status: str
    amount_naira: float
    neutral_summary: str
    timeline: list[DisputeTimelineEventOut]
    evidence: list[DisputeEvidenceOut]
    missing_evidence: list[str]
    review_flags: list[str]
    reviewer_questions: list[str]
    ai_generated: bool
    generation_notice: str | None = None
    decision_notice: str
