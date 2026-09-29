from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, Integer, String

from app.db.base_class import Base


class AlertEvent(Base):
    """Lightweight alert/event record for operational monitoring.

    Stored in DB so we can later surface in an admin dashboard or forward to an external
    log/metrics system without adding external service dependencies initially.
    """

    __tablename__ = "alert_events"

    id = Column(Integer, primary_key=True, index=True)
    category = Column(String(50), nullable=False, index=True)
    severity = Column(String(20), nullable=False, default="error")
    message = Column(String(500), nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
