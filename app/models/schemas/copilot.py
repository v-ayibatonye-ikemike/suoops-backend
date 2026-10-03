from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class CopilotActionOut(BaseModel):
    id: str
    action_type: str
    title: str
    reason: str
    action_url: str
    status: Literal["proposed", "accepted", "dismissed"]
    created_at: datetime


class CopilotBriefingOut(BaseModel):
    generated_at: datetime
    data_as_of: datetime
    headline: str
    summary: str
    ai_generated: bool
    generation_notice: str | None = None
    facts: dict
    actions: list[CopilotActionOut]
    suggested_questions: list[str]


class CopilotQuestionIn(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class CopilotAnswerOut(BaseModel):
    intent: str
    answer: str
    evidence: list[str]
    generated_at: datetime
    suggested_questions: list[str]


class CopilotDecisionIn(BaseModel):
    decision: Literal["accepted", "dismissed"]
