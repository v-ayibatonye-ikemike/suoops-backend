"""Team management service for multi-user account access."""

import logging
import smtplib
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, status
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.core.config import settings
from app.db.session import get_db
from app.models.models import User
from app.models.team_models import (
    InvitationStatus,
    Team,
    TeamInvitation,
    TeamMember,
    TeamRole,
    generate_invite_token,
    utcnow,
)
from app.models.team_schemas import (
    InvitationCreate,
    InvitationOut,
    InvitationValidation,
    TeamMemberOut,
    TeamOut,
    TeamWithMembersOut,
    UserTeamRole,
)

logger = logging.getLogger(__name__)


class TeamService:
    """Service for managing teams and invitations."""

    def __init__(self, db: Session, user_id: int):
        self.db = db
        self.user_id = user_id
        self._user: User | None = None

    @property
    def user(self) -> User:
        """Get the current user, cached."""
        if self._user is None:
            self._user = self.db.get(User, self.user_id)
            if not self._user:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
        return self._user

    # ========================================================================
    # Team Management
    # ========================================================================

    def get_or_create_team(self, name: str | None = None) -> Team:
        """Get user's team or create one if none exists."""
        team = self._get_owned_team()
        if team:
            return team

        # Create new team with user as admin
        team_name = name or self.user.business_name or f"{self.user.name}'s Team"
        team = Team(
            name=team_name,
            admin_user_id=self.user_id,
        )
        self.db.add(team)
        self.db.commit()
        self.db.refresh(team)
        return team

    def _get_owned_team(self) -> Team | None:
        """Get team where user is admin."""
        stmt = select(Team).where(Team.admin_user_id == self.user_id)
        return self.db.scalar(stmt)

    def _get_member_team(self) -> tuple[Team, TeamMember] | tuple[None, None]:
        """Get team where user is a member (not admin)."""
        stmt = select(TeamMember).options(joinedload(TeamMember.team)).where(TeamMember.user_id == self.user_id)
        membership = self.db.scalar(stmt)
        if membership:
            return membership.team, membership
        return None, None

    def get_user_team_role(self) -> UserTeamRole:
        """Get user's role in their team (if any)."""
        # Check if user is admin of a team
        owned_team = self._get_owned_team()
        if owned_team:
            return UserTeamRole(
                has_team=True,
                is_admin=True,
                team_id=owned_team.id,
                role=TeamRole.ADMIN,
                can_access_settings=True,
                can_edit_inventory=True,
            )

        # Check if user is a member of a team
        member_team, membership = self._get_member_team()
        if member_team and membership:
            return UserTeamRole(
                has_team=True,
                is_admin=False,
                team_id=member_team.id,
                role=TeamRole.MEMBER,
                can_access_settings=False,  # Members cannot access settings
                can_edit_inventory=False,  # Members cannot edit inventory
            )

        # User has no team - they are their own admin
        return UserTeamRole(
            has_team=False,
            is_admin=True,  # Solo users have full access to their own account
            team_id=None,
            role=None,
            can_access_settings=True,
            can_edit_inventory=True,
        )

    def get_team_details(self) -> TeamWithMembersOut | None:
        """Get full team details including members."""
        # First check if user is admin of a team
        team = self._get_owned_team()
        is_admin = team is not None

        # If not admin, check if member
        if not team:
            team, _ = self._get_member_team()

        if not team:
            return None

        # Load team with relationships
        stmt = (
            select(Team)
            .options(
                joinedload(Team.members).joinedload(TeamMember.user),
                joinedload(Team.invitations),
                joinedload(Team.admin_user),
            )
            .where(Team.id == team.id)
        )
        team = self.db.scalar(stmt)
        if not team:
            return None

        # Build admin member info
        admin_out = TeamMemberOut(
            id=0,  # Admin isn't in TeamMember table
            user_id=team.admin_user.id,
            user_name=team.admin_user.name,
            user_email=team.admin_user.email,
            role=TeamRole.ADMIN,
            joined_at=team.created_at,
        )

        # Build member list
        members_out = [
            TeamMemberOut(
                id=m.id,
                user_id=m.user.id,
                user_name=m.user.name,
                user_email=m.user.email,
                role=TeamRole.MEMBER,
                joined_at=m.joined_at,
            )
            for m in team.members
        ]

        # Build pending invitations (only show to admin)
        pending_invitations = []
        if is_admin:
            pending_invitations = [
                InvitationOut(
                    id=inv.id,
                    email=inv.email,
                    status=InvitationStatus(inv.status.value),
                    created_at=inv.created_at,
                    expires_at=inv.expires_at,
                    is_expired=inv.is_expired,
                    is_valid=inv.is_valid,
                )
                for inv in team.invitations
                if inv.status == InvitationStatus.PENDING
            ]

        member_count = len(team.members)

        return TeamWithMembersOut(
            team=TeamOut(
                id=team.id,
                name=team.name,
                admin_user_id=team.admin_user_id,
                max_members=team.max_members,
                member_count=member_count,
                created_at=team.created_at,
            ),
            admin=admin_out,
            members=members_out,
            pending_invitations=pending_invitations,
            can_invite=member_count < team.max_members,
        )

    def update_team_name(self, name: str) -> Team:
        """Update team name (admin only)."""
        team = self._require_admin_access()
        team.name = name
        self.db.commit()
        self.db.refresh(team)
        return team

    # ========================================================================
    # Invitation Management
    # ========================================================================

    def create_invitation(self, data: InvitationCreate) -> TeamInvitation:
        """Create and send a team invitation (admin only).

        Uses SELECT FOR UPDATE on the team row to prevent race conditions
        from concurrent invitation requests.
        """
        team = self._require_admin_access()

        # Lock the team row to prevent concurrent invitation race conditions
        self.db.execute(select(Team).where(Team.id == team.id).with_for_update())

        # Check if team is at capacity
        if len(team.members) >= team.max_members:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Team has reached maximum of {team.max_members} members",
            )

        # Check if user is already a member
        existing_member = self.db.scalar(
            select(TeamMember).join(User).where(TeamMember.team_id == team.id, User.email == data.email)
        )
        if existing_member:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="User is already a team member")

        # Check if admin is inviting themselves
        if data.email == self.user.email:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot invite yourself")

        # Check if there's already an invitation (any status)
        existing_invitation = self.db.scalar(
            select(TeamInvitation).where(
                TeamInvitation.team_id == team.id,
                TeamInvitation.email == data.email,
            )
        )

        if existing_invitation:
            if existing_invitation.status == InvitationStatus.PENDING and existing_invitation.is_valid:
                # Resend email for existing valid pending invitation
                self._send_invitation_email(existing_invitation, team)
                return existing_invitation
            else:
                # Reactivate expired/revoked invitation with new token
                from datetime import timedelta

                existing_invitation.token = generate_invite_token()
                existing_invitation.status = InvitationStatus.PENDING
                existing_invitation.created_at = datetime.now(timezone.utc)
                existing_invitation.expires_at = datetime.now(timezone.utc) + timedelta(days=7)
                existing_invitation.responded_at = None
                existing_invitation.invited_by_user_id = self.user_id
                self.db.commit()
                self.db.refresh(existing_invitation)
                self._send_invitation_email(existing_invitation, team)
                return existing_invitation

        # Create new invitation
        invitation = TeamInvitation(
            team_id=team.id,
            email=data.email,
            token=generate_invite_token(),
            invited_by_user_id=self.user_id,
        )
        self.db.add(invitation)
        self.db.commit()
        self.db.refresh(invitation)

        # Send invitation email
        self._send_invitation_email(invitation, team)

        return invitation

    def revoke_invitation(self, invitation_id: int) -> TeamInvitation:
        """Revoke a pending invitation (admin only)."""
        team = self._require_admin_access()

        invitation = self.db.get(TeamInvitation, invitation_id)
        if not invitation or invitation.team_id != team.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invitation not found")

        if invitation.status != InvitationStatus.PENDING:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Can only revoke pending invitations")

        invitation.status = InvitationStatus.REVOKED
        invitation.responded_at = utcnow()
        self.db.commit()
        self.db.refresh(invitation)
        return invitation

    def _send_invitation_email(self, invitation: TeamInvitation, team: Team) -> None:
        """Send team invitation email via SMTP."""
        try:
            # Provider-agnostic SMTP config (SMTP_* first, Brevo vars as fallback).
            from app.utils.smtp import get_smtp_config

            smtp_host, smtp_port, smtp_user, smtp_password, from_email = get_smtp_config()

            if not all([smtp_user, smtp_password]):
                logger.warning("SMTP not configured. Team invitation email not sent.")
                return

            # Build the acceptance URL
            frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
            accept_url = f"{frontend_url}/team/accept?token={invitation.token}"

            # Format expiry date
            expires_at = invitation.expires_at.strftime("%B %d, %Y at %H:%M UTC") if invitation.expires_at else "7 days"

            # Setup Jinja2 template environment
            template_dir = Path(__file__).parent.parent.parent / "templates" / "email"
            jinja_env = Environment(
                loader=FileSystemLoader(str(template_dir)), autoescape=select_autoescape(["html", "xml"])
            )

            # Render HTML template
            template = jinja_env.get_template("team_invitation.html")
            html_body = template.render(
                team_name=team.name,
                inviter_name=self.user.name,
                accept_url=accept_url,
                expires_at=expires_at,
                current_year=datetime.now(timezone.utc).year,
            )

            # Create plain text fallback
            plain_body = f"""
Team Invitation from {self.user.name}

You've been invited to join {team.name} on SuoOps!

{self.user.name} has invited you to collaborate on invoices, expenses, and business management.

Accept your invitation here:
{accept_url}

This invitation expires on {expires_at}.

If you don't know {self.user.name} or weren't expecting this invitation, you can safely ignore this email.

---
Powered by SuoOps
Professional Invoicing & Expense Management
"""

            # Create email message
            msg = MIMEMultipart("alternative")
            msg["From"] = from_email or "noreply@suoops.com"
            msg["To"] = invitation.email
            msg["Subject"] = f"You're invited to join {team.name} on SuoOps"

            # Attach plain text and HTML versions
            msg.attach(MIMEText(plain_body, "plain"))
            msg.attach(MIMEText(html_body, "html"))

            # Send email via SMTP
            logger.info("Sending team invitation email to %s", invitation.email)
            with smtplib.SMTP(smtp_host, smtp_port, timeout=10) as server:
                server.starttls()
                server.login(smtp_user, smtp_password)
                server.send_message(msg)

            logger.info("Successfully sent team invitation email to %s", invitation.email)

        except smtplib.SMTPException as e:
            logger.error("SMTP error sending team invitation to %s: %s", invitation.email, e)
            # Don't raise - invitation is still created, just email failed
        except Exception as e:
            logger.error("Error sending team invitation email: %s: %s", type(e).__name__, e)
            # Don't raise - invitation is still created, just email failed

    def validate_invitation(self, token: str) -> InvitationValidation:
        """Validate an invitation token (public, for preview)."""
        invitation = self.db.scalar(
            select(TeamInvitation)
            .options(
                joinedload(TeamInvitation.team),
                joinedload(TeamInvitation.invited_by),
            )
            .where(TeamInvitation.token == token)
        )

        if not invitation:
            return InvitationValidation(
                valid=False,
                team_name=None,
                inviter_name=None,
                email=None,
                error="Invalid invitation link",
            )

        if invitation.status != InvitationStatus.PENDING:
            return InvitationValidation(
                valid=False,
                team_name=invitation.team.name,
                inviter_name=invitation.invited_by.name if invitation.invited_by else None,
                email=invitation.email,
                error=f"Invitation has been {invitation.status.value}",
            )

        if invitation.is_expired:
            return InvitationValidation(
                valid=False,
                team_name=invitation.team.name,
                inviter_name=invitation.invited_by.name if invitation.invited_by else None,
                email=invitation.email,
                error="Invitation has expired",
            )

        return InvitationValidation(
            valid=True,
            team_name=invitation.team.name,
            inviter_name=invitation.invited_by.name if invitation.invited_by else None,
            email=invitation.email,
            error=None,
        )

    def accept_invitation(self, token: str) -> TeamMember:
        """Accept an invitation and join the team."""
        invitation = self.db.scalar(
            select(TeamInvitation).options(joinedload(TeamInvitation.team)).where(TeamInvitation.token == token)
        )

        if not invitation:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid invitation")

        if not invitation.is_valid:
            error = "expired" if invitation.is_expired else invitation.status.value
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Invitation is {error}")

        # Verify the current user's email matches invitation (case-insensitive)
        user_email = (self.user.email or "").strip().lower()
        invitation_email = (invitation.email or "").strip().lower()

        if user_email != invitation_email:
            logger.warning(f"Email mismatch: user={self.user.email!r} invitation={invitation.email!r}")
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="This invitation was sent to a different email address"
            )

        # Check if user is already in another team
        existing_team, _ = self._get_member_team()
        if existing_team:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="You are already a member of another team"
            )

        # Check if user owns a team (admins can't join other teams)
        owned_team = self._get_owned_team()
        if owned_team:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="You own a team and cannot join another team"
            )

        # Check team capacity
        team = invitation.team
        if len(team.members) >= team.max_members:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Team has reached maximum capacity")

        # Create team membership
        membership = TeamMember(
            team_id=team.id,
            user_id=self.user_id,
            role=TeamRole.MEMBER,
        )
        self.db.add(membership)

        # Update invitation status
        invitation.status = InvitationStatus.ACCEPTED
        invitation.responded_at = utcnow()

        self.db.commit()
        self.db.refresh(membership)
        return membership

    # ========================================================================
    # Member Management
    # ========================================================================

    def remove_member(self, user_id: int) -> None:
        """Remove a member from the team (admin only)."""
        team = self._require_admin_access()

        if user_id == team.admin_user_id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Cannot remove the team admin")

        membership = self.db.scalar(
            select(TeamMember).where(
                TeamMember.team_id == team.id,
                TeamMember.user_id == user_id,
            )
        )

        if not membership:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Member not found in team")

        self.db.delete(membership)
        self.db.commit()

    def leave_team(self) -> None:
        """Leave the current team (members only)."""
        team, membership = self._get_member_team()

        if not team or not membership:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You are not a member of any team")

        self.db.delete(membership)
        self.db.commit()

    # ========================================================================
    # Helpers
    # ========================================================================

    def _require_admin_access(self) -> Team:
        """Require user to be admin of a team, raise if not."""
        team = self._get_owned_team()
        if not team:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Only team admins can perform this action"
            )
        return team


def get_team_service(
    db: Annotated[Session, Depends(get_db)],
    user_id: int,
) -> TeamService:
    """Factory function to create TeamService."""
    return TeamService(db, user_id)
