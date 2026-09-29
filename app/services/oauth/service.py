"""OAuth Service for managing SSO authentication.

Responsibilities:
- Coordinate OAuth flow
- Provision users from OAuth data
- Generate JWT tokens for authenticated users

Follows SRP: Only handles OAuth-related business logic.
"""

import datetime as dt
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from app.core.security import create_access_token, create_refresh_token
from app.models.models import User
from app.models.oauth_models import OAuthToken
from app.utils.token_encryption import encrypt_token

from .exceptions import OAuthTokenError, OAuthUserInfoError
from .providers import OAuthProvider

logger = logging.getLogger(__name__)


class OAuthService:
    """
    OAuth service for managing SSO authentication.

    Responsibilities:
    - Coordinate OAuth flow
    - Provision users from OAuth data
    - Generate JWT tokens for authenticated users

    Follows SRP: Only handles OAuth-related business logic.
    """

    def __init__(self, db: Session):
        """
        Initialize OAuth service.

        Args:
            db: Database session for user operations
        """
        self.db = db
        self._providers: dict[str, OAuthProvider] = {}

    def register_provider(self, name: str, provider: OAuthProvider) -> None:
        """
        Register an OAuth provider.

        Args:
            name: Provider identifier (e.g., "google")
            provider: OAuthProvider instance
        """
        self._providers[name] = provider
        logger.info(f"Registered OAuth provider: {name}")

    def get_provider(self, name: str) -> OAuthProvider:
        """
        Get registered OAuth provider.

        Args:
            name: Provider identifier

        Returns:
            OAuthProvider instance

        Raises:
            ValueError: If provider not registered
        """
        if name not in self._providers:
            raise ValueError(f"OAuth provider '{name}' not registered")
        return self._providers[name]

    def _store_oauth_tokens(
        self,
        user_id: int,
        provider: str,
        access_token: str,
        refresh_token: str,
        token_response: dict[str, Any],
    ) -> None:
        """
        Store or update encrypted OAuth tokens for user.

        Args:
            user_id: User ID
            provider: OAuth provider name
            access_token: OAuth access token to encrypt
            refresh_token: OAuth refresh token to encrypt
            token_response: Full token response with metadata
        """
        # Calculate token expiration if provided
        expires_at = None
        if "expires_in" in token_response:
            expires_in = token_response["expires_in"]
            expires_at = datetime.now(timezone.utc) + dt.timedelta(seconds=expires_in)

        # Extract scopes if provided
        scopes = None
        if "scope" in token_response:
            scopes = (
                token_response["scope"].split() if isinstance(token_response["scope"], str) else token_response["scope"]
            )

        # Check if token already exists for this user+provider
        existing_token = (
            self.db.query(OAuthToken)
            .filter(
                OAuthToken.user_id == user_id,
                OAuthToken.provider == provider,
            )
            .first()
        )

        if existing_token:
            # Update existing token
            existing_token.access_token_encrypted = encrypt_token(access_token)
            existing_token.refresh_token_encrypted = encrypt_token(refresh_token)
            existing_token.token_type = token_response.get("token_type", "bearer")
            existing_token.expires_at = expires_at
            existing_token.scopes = scopes
            existing_token.updated_at = datetime.now(timezone.utc)
            existing_token.revoked_at = None  # Clear revocation if re-authenticating
            logger.info(f"Updated OAuth tokens for user {user_id} provider {provider}")
        else:
            # Create new token entry
            new_token = OAuthToken(
                user_id=user_id,
                provider=provider,
                access_token_encrypted=encrypt_token(access_token),
                refresh_token_encrypted=encrypt_token(refresh_token),
                token_type=token_response.get("token_type", "bearer"),
                expires_at=expires_at,
                scopes=scopes,
            )
            self.db.add(new_token)
            logger.info(f"Created OAuth tokens for user {user_id} provider {provider}")

        self.db.commit()

    def _get_or_create_user(self, email: str, name: str, oauth_provider: str) -> User:
        """
        Get existing user by OAuth email. New account creation via OAuth is disabled
        — users must register with WhatsApp phone number first.

        Args:
            email: User email from OAuth provider
            name: User full name
            oauth_provider: Provider name (e.g., "google")

        Returns:
            User instance (existing only)

        Raises:
            OAuthUserInfoError: If no account exists for the email
        """
        # Check if user exists
        user = self.db.query(User).filter(User.email == email).first()

        if user:
            # Update last_login timestamp
            user.last_login = datetime.now(timezone.utc)
            logger.info(f"Existing user logged in via {oauth_provider}: {email}")
            self.db.commit()
            self.db.refresh(user)
            return user

        # No account — block registration via OAuth
        logger.info(f"OAuth login rejected — no account for {email} via {oauth_provider}")
        raise OAuthUserInfoError(
            "No account found for this email. Please register with your WhatsApp number first, "
            "then add your email in Settings to enable Google sign-in."
        )

    async def authenticate_with_code(self, provider_name: str, code: str) -> dict[str, str]:
        """
        Authenticate user with OAuth authorization code.

        Complete OAuth flow:
        1. Exchange code for access token
        2. Fetch user info from provider
        3. Provision user in database
        4. Generate JWT tokens

        Args:
            provider_name: OAuth provider identifier
            code: Authorization code from OAuth callback

        Returns:
            JWT tokens: access_token, refresh_token, token_type

        Raises:
            OAuthProviderError: If OAuth flow fails
        """
        provider = self.get_provider(provider_name)

        # Exchange code for tokens
        token_response = await provider.exchange_code_for_token(code)
        access_token = token_response.get("access_token")

        if not access_token:
            raise OAuthTokenError("No access token in response")

        # Fetch user information
        user_info = await provider.get_user_info(access_token)
        user_data = provider.extract_user_data(user_info)

        # Validate required fields
        if not user_data.get("email"):
            raise OAuthUserInfoError("Email not provided by OAuth provider")

        # Provision user
        user = self._get_or_create_user(
            email=user_data["email"],
            name=user_data.get("name", ""),
            oauth_provider=provider_name,
        )

        # Store OAuth tokens securely (encrypted at rest)
        oauth_refresh_token = token_response.get("refresh_token")
        if oauth_refresh_token:
            self._store_oauth_tokens(
                user_id=user.id,
                provider=provider_name,
                access_token=access_token,
                refresh_token=oauth_refresh_token,
                token_response=token_response,
            )
            logger.info(f"Stored encrypted OAuth tokens for {provider_name}: {user.email}")
        else:
            logger.warning(f"No refresh token from {provider_name} for {user.email}")

        # Generate JWT tokens
        access_token_jwt = create_access_token(str(user.id))
        refresh_token_jwt = create_refresh_token(str(user.id))

        logger.info(f"User authenticated via {provider_name}: {user.email}")

        return {
            "access_token": access_token_jwt,
            "refresh_token": refresh_token_jwt,
            "token_type": "bearer",
        }
