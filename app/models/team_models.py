"""Team and team member models for multi-user account access."""

from __future__ import annotations

import datetime as dt
import enum
import secrets
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base_class import Base

if TYPE_CHECKING:
    from app.models.models import User


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def generate_invite_token() -> str:
    """Generate a secure random token for invitations."""
    return secrets.token_urlsafe(32)


class TeamRole(str, enum.Enum):
    """Team member roles with different permission levels."""

    ADMIN = "admin"  # Full access - account owner
    MEMBER = "member"  # Limited access - can view but not edit settings/inventory


class InvitationStatus(str, enum.Enum):
    """Status of team invitations."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


class Team(Base):
    """
    Team/organization model representing a business account.

    The user who creates the account becomes the admin.
    Admin can invite up to 5 additional team members.
    """

    __tablename__ = "team"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    # The admin user who owns this team (account creator)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,  # One team per user
        index=True,
    )

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    # Maximum number of team members (excluding admin)
    max_members: Mapped[int] = mapped_column(Integer, default=5, server_default="5")

    # Relationships
    admin_user: Mapped[User] = relationship("User", foreign_keys=[admin_user_id], backref="owned_team")

    members: Mapped[list[TeamMember]] = relationship(
        "TeamMember",
        back_populates="team",
        cascade="all, delete-orphan",
    )

    invitations: Mapped[list[TeamInvitation]] = relationship(
        "TeamInvitation",
        back_populates="team",
        cascade="all, delete-orphan",
    )


class TeamMember(Base):
    """
    Team membership linking users to teams.

    Note: Admin user is NOT in this table - they are tracked via Team.admin_user_id.
    This table only contains invited members.
    """

    __tablename__ = "team_member"
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member_team_user"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), nullable=False, index=True)

    role: Mapped[TeamRole] = mapped_column(
        Enum(
            TeamRole,
            values_callable=lambda x: [e.value for e in x],
            name="teamrole",
            create_constraint=False,
        ),
        default=TeamRole.MEMBER,
        server_default="member",
        nullable=False,
    )

    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    # Relationships
    team: Mapped[Team] = relationship("Team", back_populates="members")
    user: Mapped[User] = relationship("User", backref="team_memberships")


class TeamInvitation(Base):
    """
    Pending team invitations sent via email.

    Invitations expire after 7 days and can be revoked by admin.
    """

    __tablename__ = "team_invitation"
    __table_args__ = (UniqueConstraint("team_id", "email", name="uq_team_invitation_team_email"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("team.id", ondelete="CASCADE"), nullable=False, index=True)

    # Email of the person being invited
    email: Mapped[str] = mapped_column(String(255), nullable=False, index=True)

    # Secure token for accepting the invitation
    token: Mapped[str] = mapped_column(String(64), default=generate_invite_token, unique=True, index=True)

    status: Mapped[InvitationStatus] = mapped_column(
        Enum(
            InvitationStatus,
            values_callable=lambda x: [e.value for e in x],
            name="invitationstatus",
            create_constraint=False,
        ),
        default=InvitationStatus.PENDING,
        server_default="pending",
        nullable=False,
    )

    # Who sent the invitation (for audit trail)
    invited_by_user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="SET NULL"), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    # Invitation expires after 7 days
    expires_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: utcnow() + dt.timedelta(days=7), nullable=False
    )

    # When the invitation was accepted/revoked
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Relationships
    team: Mapped[Team] = relationship("Team", back_populates="invitations")
    invited_by: Mapped[User | None] = relationship("User", foreign_keys=[invited_by_user_id])

    @property
    def is_expired(self) -> bool:
        """Check if the invitation has expired."""
        return utcnow() > self.expires_at

    @property
    def is_valid(self) -> bool:
        """Check if the invitation can still be accepted."""
        return self.status == InvitationStatus.PENDING and not self.is_expired
