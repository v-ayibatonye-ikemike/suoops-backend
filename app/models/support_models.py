"""Support ticket model for contact form submissions."""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.sql import func

from app.db.base_class import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class TicketStatus(str, enum.Enum):
    """Support ticket status."""

    OPEN = "open"
    IN_PROGRESS = "in_progress"
    WAITING = "waiting"  # Waiting for customer response
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, enum.Enum):
    """Support ticket priority."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


class TicketCategory(str, enum.Enum):
    """Support ticket category."""

    GENERAL = "general"
    BILLING = "billing"
    TECHNICAL = "technical"
    FEATURE = "feature"
    ACCOUNT = "account"
    OTHER = "other"


class SupportTicket(Base):
    """Model for storing support tickets from contact form."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(primary_key=True)

    # Submitter info (may not be a registered user)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(255), index=True)

    # Ticket content
    subject: Mapped[str] = mapped_column(String(500))
    message: Mapped[str] = mapped_column(Text)
    category: Mapped[TicketCategory] = mapped_column(
        Enum(TicketCategory, values_callable=lambda x: [e.value for e in x]),
        default=TicketCategory.GENERAL,
    )

    # Ticket management
    status: Mapped[TicketStatus] = mapped_column(
        Enum(TicketStatus, values_callable=lambda x: [e.value for e in x]),
        default=TicketStatus.OPEN,
        index=True,
    )
    priority: Mapped[TicketPriority] = mapped_column(
        Enum(TicketPriority, values_callable=lambda x: [e.value for e in x]),
        default=TicketPriority.MEDIUM,
        index=True,
    )

    # Assigned admin (optional - just stores user ID, no FK constraint)
    assigned_to_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        index=True,
    )

    # Internal notes (admin only)
    internal_notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Response tracking
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    responded_by_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        index=True,
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        onupdate=utcnow,
    )
    resolved_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    def __repr__(self) -> str:
        return f"<SupportTicket {self.id}: {self.subject[:30]}...>"
