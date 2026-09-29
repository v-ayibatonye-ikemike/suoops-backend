"""Factory function for creating configured OAuth service."""

import logging

from sqlalchemy.orm import Session

from app.core.config import settings

from .providers import GoogleOAuthProvider
from .service import OAuthService

logger = logging.getLogger(__name__)


def create_oauth_service(db: Session) -> OAuthService:
    """
    Factory function to create configured OAuth service.

    Automatically registers all enabled OAuth providers.

    Args:
        db: Database session

    Returns:
        Configured OAuthService instance
    """
    service = OAuthService(db)

    # Register Google OAuth if configured
    if settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET:
        google_provider = GoogleOAuthProvider(
            client_id=settings.GOOGLE_CLIENT_ID,
            client_secret=settings.GOOGLE_CLIENT_SECRET,
            redirect_uri=f"{settings.BACKEND_URL}/auth/oauth/google/callback",
        )
        service.register_provider("google", google_provider)
        logger.info("Google OAuth provider enabled")
    else:
        logger.warning("Google OAuth not configured (missing client ID/secret)")

    return service
