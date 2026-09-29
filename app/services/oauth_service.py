"""Backward compatibility redirect for oauth_service.

DEPRECATED: This module has been refactored into app/services/oauth/
for better SRP compliance and code organization.

All imports should continue to work via this redirect module.
New code should import from app.services.oauth directly.
"""

from app.services.oauth import (
    GoogleOAuthProvider,
    # Providers
    OAuthProvider,
    # Exceptions
    OAuthProviderError,
    # Service
    OAuthService,
    OAuthTokenError,
    OAuthUserInfoError,
    # Factory
    create_oauth_service,
)

__all__ = [
    "OAuthProviderError",
    "OAuthTokenError",
    "OAuthUserInfoError",
    "OAuthProvider",
    "GoogleOAuthProvider",
    "OAuthService",
    "create_oauth_service",
]
