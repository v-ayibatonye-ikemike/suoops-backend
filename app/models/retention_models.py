from __future__ import annotations

import datetime as dt

from sqlalchemy import JSON, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


class MerchantLifecycle(Base):
    __tablename__ = "merchant_lifecycle"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    state: Mapped[str] = mapped_column(String(20), default="new")
    state_since: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    last_activity_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    reactivated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    whatsapp_opted_in: Mapped[bool] = mapped_column(default=False, server_default="false")
    consent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    consent_source: Mapped[str | None] = mapped_column(String(30))
    consented_phone: Mapped[str | None] = mapped_column(String(32))


class RetentionExperiment(Base):
    __tablename__ = "retention_experiment"

    # One bounded pilot, not a general-purpose campaign builder.
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=False)
    enabled: Mapped[bool] = mapped_column(default=False, server_default="false")
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[int] = mapped_column(ForeignKey("admin_users.id"))


class RetentionEnrollment(Base):
    __tablename__ = "retention_enrollment"

    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), primary_key=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("retention_experiment.id"), index=True)
    arm: Mapped[str] = mapped_column(String(10))
    initial_state: Mapped[str] = mapped_column(String(20))
    baseline: Mapped[dict] = mapped_column(JSON)


class RetentionContact(Base):
    __tablename__ = "retention_contact"
    __table_args__ = (UniqueConstraint("user_id", "dedupe_key", name="uq_retention_contact"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    experiment_id: Mapped[int] = mapped_column(ForeignKey("retention_experiment.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(40))
    dedupe_key: Mapped[str] = mapped_column(String(120))
    state: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20), default="reserved")
    message_id: Mapped[str | None] = mapped_column(String(255), unique=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(String(300))
