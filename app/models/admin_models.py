"""Admin user models for support dashboard."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import Boolean, DateTime, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class AdminUser(Base):
    """Admin users for support.suoops.com dashboard.

    Separate from regular User model to allow password-based auth
    for admin access while keeping OTP-only for regular users.
    """

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)

    # Permissions
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    is_super_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Permissions for different sections
    can_manage_tickets: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    can_view_users: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    can_view_analytics: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    can_invite_admins: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")

    # Tracking
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        onupdate=utcnow,
    )

    # Who invited this admin (NULL for system-created default admin)
    invited_by_id: Mapped[int | None] = mapped_column(nullable=True)

    # Invite token (for pending invitations)
    invite_token: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    invite_expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<AdminUser {self.email}>"


class AdminLoginAudit(Base):
    """Audit trail of admin authentication events.

    Persisted to the database (unlike the file-based audit log) so it can be
    queried from the admin panel to spot logins from unexpected IPs.
    """

    __tablename__ = "admin_login_audit"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int | None] = mapped_column(index=True, nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # "success" | "failure"
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # machine-readable event, e.g. "login", "login_failed", "password_changed"
    event: Mapped[str] = mapped_column(String(40), nullable=False, server_default="login")
    # short reason on failure (e.g. "bad_password", "inactive", "bad_domain")
    reason: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
        index=True,
    )

    def __repr__(self) -> str:
        return f"<AdminLoginAudit {self.email} {self.status} {self.created_at}>"


class AdminIpAllowlistEntry(Base):
    """An IP address or CIDR range permitted to reach the admin panel.

    Entries here are merged with the optional ``ADMIN_IP_ALLOWLIST`` env var.
    When both the database and the env var are empty the allowlist is disabled
    and the panel is reachable from anywhere (fail open).
    """

    __tablename__ = "admin_ip_allowlist"

    id: Mapped[int] = mapped_column(primary_key=True)
    # Stored normalised (e.g. "203.0.113.10/32" or "203.0.113.0/24").
    cidr: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_by_id: Mapped[int | None] = mapped_column(index=True, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        default=utcnow,
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<AdminIpAllowlistEntry {self.cidr}>"
