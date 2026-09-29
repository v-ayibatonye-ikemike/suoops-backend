"""Pydantic schemas for team management."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, EmailStr, Field


class TeamRole(str, Enum):
    """Team member roles."""

    ADMIN = "admin"
    MEMBER = "member"


class InvitationStatus(str, Enum):
    """Status of team invitations."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    EXPIRED = "expired"
    REVOKED = "revoked"


# ============================================================================
# Team Schemas
# ============================================================================


class TeamCreate(BaseModel):
    """Schema for creating a team (auto-created on first invite)."""

    name: str = Field(..., min_length=1, max_length=255, description="Team/business name")


class TeamOut(BaseModel):
    """Schema for team information."""

    id: int
    name: str
    admin_user_id: int
    max_members: int
    member_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class TeamUpdate(BaseModel):
    """Schema for updating team settings."""

    name: str | None = Field(None, min_length=1, max_length=255)


# ============================================================================
# Team Member Schemas
# ============================================================================


class TeamMemberOut(BaseModel):
    """Schema for team member details."""

    id: int
    user_id: int
    user_name: str
    user_email: str | None
    role: TeamRole
    joined_at: datetime

    model_config = {"from_attributes": True}


class TeamMemberRemove(BaseModel):
    """Schema for removing a team member."""

    user_id: int = Field(..., description="ID of the user to remove from team")


# ============================================================================
# Invitation Schemas
# ============================================================================


class InvitationCreate(BaseModel):
    """Schema for creating a team invitation."""

    email: EmailStr = Field(..., description="Email address to send invitation to")


class InvitationOut(BaseModel):
    """Schema for invitation details."""

    id: int
    email: str
    status: InvitationStatus
    created_at: datetime
    expires_at: datetime
    is_expired: bool
    is_valid: bool

    model_config = {"from_attributes": True}


class InvitationAccept(BaseModel):
    """Schema for accepting an invitation."""

    token: str = Field(..., min_length=32, description="Invitation token from email link")


class InvitationAcceptDirect(BaseModel):
    """Schema for accepting an invitation without authentication.

    This creates a new user account automatically if one doesn't exist.
    """

    token: str = Field(..., min_length=32, description="Invitation token from email link")
    name: str = Field(..., min_length=2, max_length=120, description="Full name for new account")


class InvitationAcceptResponse(BaseModel):
    """Response when accepting invitation directly (includes JWT tokens)."""

    member: TeamMemberOut
    access_token: str
    refresh_token: str
    access_expires_at: str  # ISO timestamp for token expiry
    token_type: str = "bearer"
    is_new_user: bool  # True if account was just created


class InvitationRevoke(BaseModel):
    """Schema for revoking an invitation."""

    invitation_id: int = Field(..., description="ID of the invitation to revoke")


# ============================================================================
# Response Schemas
# ============================================================================


class TeamWithMembersOut(BaseModel):
    """Full team details including members and pending invitations."""

    team: TeamOut
    admin: TeamMemberOut  # Admin info (derived from team.admin_user)
    members: list[TeamMemberOut]
    pending_invitations: list[InvitationOut]
    can_invite: bool  # True if team hasn't reached max members

    model_config = {"from_attributes": True}


class UserTeamRole(BaseModel):
    """Schema for checking user's role in their team."""

    has_team: bool
    is_admin: bool
    team_id: int | None
    role: TeamRole | None
    can_access_settings: bool
    can_edit_inventory: bool

    model_config = {"from_attributes": True}


class InvitationValidation(BaseModel):
    """Schema for validating an invitation token."""

    valid: bool
    team_name: str | None
    inviter_name: str | None
    email: str | None
    error: str | None
