"""Google OAuth 2.0 / OpenID Connect implementation."""

from typing import Any

from .base import OAuthProvider


class GoogleOAuthProvider(OAuthProvider):
    """Google OAuth 2.0 / OpenID Connect implementation."""

    @property
    def authorization_url(self) -> str:
        return "https://accounts.google.com/o/oauth2/v2/auth"

    @property
    def token_url(self) -> str:
        return "https://oauth2.googleapis.com/token"

    @property
    def user_info_url(self) -> str:
        return "https://www.googleapis.com/oauth2/v2/userinfo"

    @property
    def scopes(self) -> list[str]:
        # Minimal scopes for authentication per Google best practices
        # Request additional scopes (e.g., profile) incrementally when needed
        return ["openid", "email"]

    def extract_user_data(self, user_info: dict[str, Any]) -> dict[str, str]:
        """
        Extract user data from Google user info response.

        Expected fields:
        - email: User's email address
        - name: Full name
        - picture: Profile picture URL
        - verified_email: Email verification status
        """
        return {
            "email": user_info.get("email", ""),
            "name": user_info.get("name", ""),
            "picture": user_info.get("picture", ""),
            "email_verified": user_info.get("verified_email", False),
        }
