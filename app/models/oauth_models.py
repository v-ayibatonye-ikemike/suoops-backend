"""
OAuth Token Storage Models.

Stores encrypted OAuth refresh tokens and access tokens for user accounts.
Enables token refresh, revocation, and Cross-Account Protection integration.

Security:
- All tokens encrypted at rest using Fernet
- One token set per user per provider (enforced by unique index)
- Tracks revocation status for cleanup
- Records token expiration for refresh logic
"""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import JSON, Column, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.db.base_class import Base

if TYPE_CHECKING:
    pass


class OAuthToken(Base):
    """
    Encrypted OAuth tokens for user accounts.

    Stores refresh tokens and access tokens obtained from OAuth providers.
    Enables offline access, token refresh, and proper revocation handling.

    Attributes:
        id: Primary key
        user_id: Foreign key to users table
        provider: OAuth provider name (e.g., "google", "microsoft")
        access_token_encrypted: Encrypted OAuth access token
        refresh_token_encrypted: Encrypted OAuth refresh token
        token_type: Token type (usually "bearer")
        expires_at: When the access token expires (for refresh scheduling)
        scopes: JSON array of granted OAuth scopes
        created_at: When token was first stored
        updated_at: When token was last refreshed
        revoked_at: When token was revoked (NULL if active)
    """

    __tablename__ = "oauth_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey("user.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(String(50), nullable=False, index=True)

    # Encrypted with Fernet (see app.utils.token_encryption)
    access_token_encrypted = Column(Text, nullable=False)
    refresh_token_encrypted = Column(Text, nullable=False)

    token_type = Column(String(50), default="bearer", nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    scopes = Column(JSON, nullable=True)  # ["openid", "email", "profile"]

    # Timestamps
    created_at = Column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        onupdate=func.now(),
        nullable=True,
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)

    # Relationships
    user = relationship("User", back_populates="oauth_tokens")

    # Indexes
    __table_args__ = (
        # One token set per user per provider
        Index(
            "ix_oauth_tokens_user_provider",
            "user_id",
            "provider",
            unique=True,
        ),
        # Query active tokens efficiently
        Index("ix_oauth_tokens_revoked", "revoked_at"),
        # Query expiring tokens for refresh
        Index("ix_oauth_tokens_expires", "expires_at"),
    )

    def __repr__(self) -> str:
        """String representation (no sensitive data)."""
        revoked = " (REVOKED)" if self.revoked_at else ""
        return f"<OAuthToken(id={self.id}, " f"user_id={self.user_id}, " f"provider={self.provider}{revoked})>"

    @property
    def is_revoked(self) -> bool:
        """Check if token has been revoked."""
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        """Check if access token has expired (if expires_at is set)."""
        if not self.expires_at:
            return False
        return datetime.now(self.expires_at.tzinfo or None) >= self.expires_at
