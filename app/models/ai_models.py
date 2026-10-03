from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base_class import Base


class AIUsageEvent(Base):
    """One metered AI provider operation without storing prompt content."""

    __tablename__ = "ai_usage_event"
    __table_args__ = (
        Index("ix_ai_usage_owner_created", "data_owner_id", "created_at"),
        Index("ix_ai_usage_owner_status_created", "data_owner_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    data_owner_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    actor_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), index=True, nullable=True)
    actor_admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"),
        index=True,
        nullable=True,
    )
    counts_toward_quota: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        server_default="true",
        nullable=False,
    )
    feature: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    provider: Mapped[str] = mapped_column(String(40), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(20), index=True, nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), default=Decimal("0"), server_default="0", nullable=False
    )
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), server_default=func.now(), index=True
    )
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AIFeatureControl(Base):
    """Platform rollout and kill-switch state for one allowlisted AI feature."""

    __tablename__ = "ai_feature_control"

    id: Mapped[int] = mapped_column(primary_key=True)
    feature: Mapped[str] = mapped_column(String(80), unique=True, index=True, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    rollout_percent: Mapped[int] = mapped_column(Integer, default=100, server_default="100", nullable=False)
    allowlisted_owner_ids: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    reason: Mapped[str | None] = mapped_column(String(300), nullable=True)
    updated_by_admin_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("admin_users.id"),
        nullable=True,
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
        nullable=True,
    )


class AITenantPreference(Base):
    """Merchant-controlled AI opt-out and per-feature preferences."""

    __tablename__ = "ai_tenant_preference"

    id: Mapped[int] = mapped_column(primary_key=True)
    data_owner_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        unique=True,
        index=True,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true", nullable=False)
    feature_overrides: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    updated_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        onupdate=lambda: dt.datetime.now(dt.timezone.utc),
        nullable=True,
    )


class AIFeedback(Base):
    """User quality signal for an AI feature without retaining prompt content."""

    __tablename__ = "ai_feedback"
    __table_args__ = (Index("ix_ai_feedback_feature_created", "feature", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    data_owner_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True, nullable=False)
    actor_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    feature: Mapped[str] = mapped_column(String(80), nullable=False)
    sentiment: Mapped[str] = mapped_column(String(10), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(40), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    context_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: dt.datetime.now(dt.timezone.utc),
        server_default=func.now(),
        index=True,
    )


class AICopilotBriefing(Base):
    """Cached daily narrative generated only from deterministic commerce facts."""

    __tablename__ = "ai_copilot_briefing"
    __table_args__ = (UniqueConstraint("data_owner_id", "briefing_date", name="uq_ai_briefing_owner_date"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    data_owner_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    briefing_date: Mapped[dt.date] = mapped_column(Date, index=True, nullable=False)
    facts_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    headline: Mapped[str] = mapped_column(String(180), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    generation_notice: Mapped[str | None] = mapped_column(String(180), nullable=True)
    generated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), server_default=func.now()
    )


class AIProposedAction(Base):
    """A reversible user decision record; domain execution is handled separately."""

    __tablename__ = "ai_proposed_action"
    __table_args__ = (
        UniqueConstraint("data_owner_id", "dedupe_key", name="uq_ai_action_owner_dedupe"),
        Index("ix_ai_action_owner_status_created", "data_owner_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    data_owner_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    proposed_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False)
    action_type: Mapped[str] = mapped_column(String(60), index=True, nullable=False)
    title: Mapped[str] = mapped_column(String(180), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    action_url: Mapped[str] = mapped_column(String(300), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(140), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="proposed", server_default="proposed", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), server_default=func.now()
    )
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("user.id"), nullable=True)


class AICollectionDraft(Base):
    """Merchant-reviewed payment reminder tied to one real overdue invoice."""

    __tablename__ = "ai_collection_draft"
    __table_args__ = (
        UniqueConstraint("data_owner_id", "dedupe_key", name="uq_ai_collection_owner_dedupe"),
        Index("ix_ai_collection_owner_status_created", "data_owner_id", "status", "created_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    public_id: Mapped[str] = mapped_column(String(36), unique=True, index=True, nullable=False)
    data_owner_id: Mapped[int] = mapped_column(ForeignKey("user.id"), index=True, nullable=False)
    created_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False)
    invoice_id: Mapped[int] = mapped_column(ForeignKey("invoice.id", ondelete="CASCADE"), index=True, nullable=False)
    channel: Mapped[str] = mapped_column(String(20), nullable=False)
    recipient_masked: Mapped[str] = mapped_column(String(255), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(180), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    priority_score: Mapped[int] = mapped_column(Integer, nullable=False)
    priority_level: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_codes: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    explanation: Mapped[str] = mapped_column(String(600), nullable=False)
    ai_generated: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    dedupe_key: Mapped[str] = mapped_column(String(140), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="draft", server_default="draft", index=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: dt.datetime.now(dt.timezone.utc), server_default=func.now()
    )
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dismissed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
