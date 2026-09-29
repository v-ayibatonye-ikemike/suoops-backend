"""Abstract base class for OAuth 2.0 providers.

Implements the OAuth 2.0 authorization code flow.
Subclasses must implement provider-specific details.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlencode

import httpx

from ..exceptions import OAuthTokenError, OAuthUserInfoError

logger = logging.getLogger(__name__)


class OAuthProvider(ABC):
    """
    Abstract base class for OAuth 2.0 providers.

    Implements the OAuth 2.0 authorization code flow.
    Subclasses must implement provider-specific details.
    """

    def __init__(self, client_id: str, client_secret: str, redirect_uri: str):
        """
        Initialize OAuth provider.

        Args:
            client_id: OAuth client ID from provider
            client_secret: OAuth client secret from provider
            redirect_uri: Callback URL for OAuth flow
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

    @property
    @abstractmethod
    def authorization_url(self) -> str:
        """Provider's authorization endpoint."""
        pass

    @property
    @abstractmethod
    def token_url(self) -> str:
        """Provider's token exchange endpoint."""
        pass

    @property
    @abstractmethod
    def user_info_url(self) -> str:
        """Provider's user info endpoint."""
        pass

    @property
    @abstractmethod
    def scopes(self) -> list[str]:
        """Required OAuth scopes."""
        pass

    def get_authorization_url(self, state: str) -> str:
        """
        Generate authorization URL for OAuth flow.

        Args:
            state: CSRF protection token

        Returns:
            Full authorization URL with query parameters
        """
        params = {
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": " ".join(self.scopes),
            "state": state,
            "access_type": "offline",  # Request refresh token
            "prompt": "consent",  # Force consent to get refresh token
        }

        return f"{self.authorization_url}?{urlencode(params)}"

    async def exchange_code_for_token(self, code: str) -> dict[str, Any]:
        """
        Exchange authorization code for access token.

        Args:
            code: Authorization code from OAuth callback

        Returns:
            Token response with access_token, refresh_token, etc.

        Raises:
            OAuthTokenError: If token exchange fails
        """
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": self.redirect_uri,
        }

        # Log sanitized exchange metadata (no secrets)
        code_hash = abs(hash(code))
        logger.info(
            f"Token exchange attempt | "
            f"provider=oauth "
            f"code_hash={code_hash} "
            f"client_id={self.client_id} "
            f"redirect_uri={self.redirect_uri}"
        )

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(self.token_url, data=data, timeout=10.0)
                response.raise_for_status()
                logger.info(f"Token exchange SUCCESS | code_hash={hash(code)}")
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(
                    f"Token exchange failed | "
                    f"code_hash={hash(code)} "
                    f"status={e.response.status_code} "
                    f"response={e.response.text}"
                )
                raise OAuthTokenError(f"Token exchange failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"Token exchange request failed: {str(e)}")
                raise OAuthTokenError("Failed to connect to OAuth provider") from e

    async def get_user_info(self, access_token: str) -> dict[str, Any]:
        """
        Fetch user information using access token.

        Args:
            access_token: OAuth access token

        Returns:
            User profile information

        Raises:
            OAuthUserInfoError: If fetching user info fails
        """
        headers = {"Authorization": f"Bearer {access_token}"}

        async with httpx.AsyncClient() as client:
            try:
                response = await client.get(self.user_info_url, headers=headers, timeout=10.0)
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError as e:
                logger.error(f"User info fetch failed: {e.response.text}")
                raise OAuthUserInfoError(f"User info fetch failed: {e.response.status_code}") from e
            except httpx.RequestError as e:
                logger.error(f"User info request failed: {str(e)}")
                raise OAuthUserInfoError("Failed to connect to OAuth provider") from e

    @abstractmethod
    def extract_user_data(self, user_info: dict[str, Any]) -> dict[str, str]:
        """
        Extract standardized user data from provider response.

        Args:
            user_info: Raw user info from provider

        Returns:
            Standardized user data with keys: email, name, picture
        """
        pass
