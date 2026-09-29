"""Common dependencies for role-based access control."""

from typing import Annotated, TypeAlias

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db

CurrentUserDep: TypeAlias = Annotated[int, Depends(get_current_user_id)]
DbDep: TypeAlias = Annotated[Session, Depends(get_db)]


def require_admin_role(current_user_id: CurrentUserDep, db: DbDep) -> int:
    """
    Verify user has admin privileges (can modify settings).

    Access rules:
    - Solo users (no team) have full access to their account
    - Team admins have full access
    - Team members (invited users) have read-only access

    Raises HTTPException 403 if user is a team member (not admin).
    Returns the user_id if access is granted.
    """
    from app.models.team_models import Team, TeamMember

    # Check if user is admin of a team (they have full access)
    team = db.scalar(select(Team).where(Team.admin_user_id == current_user_id))
    if team:
        return current_user_id

    # Check if user is a member of a team (they have read-only access)
    membership = db.scalar(select(TeamMember).where(TeamMember.user_id == current_user_id))
    if membership:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "role_restricted",
                "message": "Only team admins can modify settings",
                "required_role": "admin",
                "current_role": "member",
            },
        )

    # Solo user (no team) has full access
    return current_user_id


AdminUserDep: TypeAlias = Annotated[int, Depends(require_admin_role)]


def get_data_owner_id(current_user_id: CurrentUserDep, db: DbDep) -> int:
    """
    Get the user_id to use for data access (invoices, expenses, inventory, etc.).

    This implements the "shared workspace" model:
    - Solo users (no team): access their own data
    - Team admins: access their own data (which is the team's data)
    - Team members: access the team ADMIN's data (shared workspace)

    Returns:
        The user_id to use when querying data. For team members,
        this is the team admin's user_id, not their own.
    """
    from app.models.team_models import Team, TeamMember

    # Check if user is admin of a team (use their own ID)
    team = db.scalar(select(Team).where(Team.admin_user_id == current_user_id))
    if team:
        return current_user_id

    # Check if user is a member of a team (use admin's ID)
    membership = db.scalar(select(TeamMember).options().where(TeamMember.user_id == current_user_id))
    if membership:
        # Get the team to find the admin's user_id
        team = db.get(Team, membership.team_id)
        if team:
            return team.admin_user_id

    # Solo user (no team) - use their own ID
    return current_user_id


DataOwnerDep: TypeAlias = Annotated[int, Depends(get_data_owner_id)]
