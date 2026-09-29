"""
OAuth 2.0 / SSO Authentication Routes.

Endpoints:
- GET  /auth/oauth/{provider}/login - Initiate OAuth flow
- GET  /auth/oauth/{provider}/callback - Handle OAuth callback
- GET  /auth/oauth/providers - List available providers

Follows SRP: Only handles HTTP layer for OAuth.
Business logic delegated to OAuthService.
"""

import logging
import secrets
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import redis
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy.orm import Session

from app.api.rate_limit import RATE_LIMITS, limiter
from app.api.routes_auth import _set_refresh_cookie, get_current_user_id
from app.core.config import settings
from app.core.csrf import get_csrf_token, set_csrf_cookie
from app.core.security import TokenType, decode_token
from app.db.session import get_db
from app.metrics import oauth_login_success
from app.models import schemas
from app.services.oauth_service import OAuthProviderError, create_oauth_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/auth/oauth", tags=["oauth"])


# Redis client for OAuth state storage (shared across dynos)
_redis_client: redis.Redis | None = None


def _get_redis_client() -> redis.Redis:
    """Get or lazily create a Redis client for state management.

    Uses centralized connection pool to avoid hitting connection limits.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client

    try:
        from app.db.redis_client import get_redis_client

        _redis_client = get_redis_client()
        logger.info("OAuth using shared Redis client")
        return _redis_client
    except Exception as e:
        logger.error("Failed to get Redis client for OAuth: %s", e)
        raise


def _generate_state() -> str:
    """
    Generate cryptographically secure state token for CSRF protection.

    Returns:
        Random 32-character hex string
    """
    return secrets.token_hex(16)


def _store_oauth_state(state: str, redirect_uri: str) -> None:
    """
    Store OAuth state token in Redis with redirect URI.

    Args:
        state: State token to store
        redirect_uri: Frontend redirect URI to store with state
    """
    try:
        redis_client = _get_redis_client()
        key = f"oauth:state:{state}"
        # Store for 10 minutes (OAuth flow should complete quickly)
        redis_client.setex(key, 600, redirect_uri)
        # Do NOT log raw state token in production to avoid leaking CSRF token.
        if settings.ENV.lower() != "prod":  # safe to debug in non-prod
            logger.debug("Stored OAuth state token (length=%s)", len(state))
    except Exception as e:
        logger.error("Failed to store OAuth state in Redis: %s. OAuth may fail.", e)
        # Continue anyway - OAuth state validation will fail gracefully


def _build_redirect_with_params(base_url: str, params: dict[str, str]) -> str:
    """Append query parameters to an existing URL safely."""
    parsed = urlparse(base_url)
    existing_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    existing_params.update(params)
    new_query = urlencode(existing_params)
    return urlunparse(parsed._replace(query=new_query))


def _get_redirect_uri(state: str, consume: bool = True) -> str | None:
    """
    Validate OAuth state token and return redirect URI from Redis.

    Args:
        state: State token from OAuth callback
        consume: Whether to delete the state entry after retrieval

    Returns:
        Redirect URI if state is valid, None otherwise
    """
    try:
        redis_client = _get_redis_client()
        key = f"oauth:state:{state}"

        redirect_uri = redis_client.get(key)

        if redirect_uri is None:
            return None

        if consume:
            redis_client.delete(key)
            if settings.ENV.lower() != "prod":
                logger.debug("Consumed OAuth state token (length=%s)", len(state))
        else:
            if settings.ENV.lower() != "prod":
                logger.debug("Validated OAuth state without consuming (length=%s)", len(state))

        return redirect_uri
    except Exception as e:
        logger.error("Failed to retrieve OAuth state from Redis: %s", e)
        return None  # Treat as invalid state


def _extract_access_expiry(access_token: str) -> datetime:
    """Derive expiry timestamp from a signed access token."""
    payload = decode_token(access_token, expected_type=TokenType.ACCESS)
    raw_exp = payload.get("exp")

    if isinstance(raw_exp, (int, float)):
        return datetime.fromtimestamp(raw_exp, tz=timezone.utc)
    if isinstance(raw_exp, str):
        try:
            parsed = datetime.fromisoformat(raw_exp)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError as exc:  # pragma: no cover - defensive fallback
            raise ValueError("Unable to parse token expiry") from exc
    if isinstance(raw_exp, datetime):
        return raw_exp if raw_exp.tzinfo else raw_exp.replace(tzinfo=timezone.utc)

    raise ValueError("Unsupported exp claim in access token")


@router.get("/providers", response_model=schemas.OAuthProvidersOut)
def list_oauth_providers(db: Annotated[Session, Depends(get_db)]) -> dict:
    """
    List available OAuth providers.

    Returns list of configured SSO providers with their capabilities.

    Returns:
        {
            "providers": [
                {
                    "name": "google",
                    "display_name": "Google",
                    "enabled": true,
                    "supports_refresh": true
                }
            ]
        }
    """
    providers = []

    # Google provider
    if settings.GOOGLE_CLIENT_ID:
        providers.append(
            {
                "name": "google",
                "display_name": "Google",
                "enabled": True,
                "supports_refresh": True,
                "icon_url": "https://www.google.com/favicon.ico",
            }
        )

    return {"providers": providers}


@router.get("/{provider}/login")
@limiter.limit(RATE_LIMITS["oauth_login"])
def oauth_login(
    provider: str,
    request: Request,  # included for rate limiter key_func
    redirect_uri: str | None = Query(None, description="Frontend redirect after auth"),
    db: Session = Depends(get_db),
) -> RedirectResponse:
    """
    Initiate OAuth login flow.

    Redirects user to OAuth provider's authorization page.

    Args:
        provider: OAuth provider name (e.g., "google")
        redirect_uri: Optional frontend URL to redirect after successful auth

    Returns:
        Redirect to OAuth provider's authorization page

    Example:
        GET /auth/oauth/google/login?redirect_uri=https://app.suoops.com/dashboard
    """
    try:
        oauth_service = create_oauth_service(db)
        oauth_provider = oauth_service.get_provider(provider)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e

    # Generate CSRF protection state and store in Redis
    state = _generate_state()
    _store_oauth_state(state, redirect_uri or settings.FRONTEND_URL)

    # Get authorization URL
    auth_url = oauth_provider.get_authorization_url(state)

    logger.info("OAuth login start | provider=%s", provider)
    return RedirectResponse(url=auth_url)


@router.get("/{provider}/callback", response_model=schemas.OAuthCallbackOut)
@limiter.limit(RATE_LIMITS["oauth_callback"])
async def oauth_callback(
    provider: str,
    request: Request,
    code: str = Query(..., description="Authorization code from OAuth provider"),
    state: str = Query(..., description="CSRF protection token"),
    db: Session = Depends(get_db),
) -> dict:
    """
    Handle OAuth provider callback.

    Completes OAuth flow:
    1. Validates CSRF state
    2. Exchanges code for tokens
    3. Fetches user info
    4. Creates/updates user
    5. Returns JWT tokens

    Args:
        provider: OAuth provider name
        code: Authorization code from provider
        state: CSRF state token

    Returns:
        {
            "access_token": "eyJ...",
            "refresh_token": "eyJ...",
            "token_type": "bearer",
            "redirect_uri": "https://app.suoops.com/dashboard"
        }

    Example:
        GET /auth/oauth/google/callback?code=4/xxx&state=abc123
    """
    accept_header = (request.headers.get("accept", "") or "").lower()
    sec_fetch_mode = (request.headers.get("sec-fetch-mode", "") or "").lower()
    referer = request.headers.get("referer", "") or ""
    origin = request.headers.get("origin", "") or ""
    expects_json = "application/json" in accept_header or (sec_fetch_mode and sec_fetch_mode != "navigate")

    # For browser navigations we redirect back to the frontend with original parameters
    if not expects_json:
        redirect_uri = _get_redirect_uri(state, consume=False)
        if redirect_uri is None:
            logger.warning(
                "Invalid OAuth state (non-JSON request): %s | headers accept=%s mode=%s origin=%s referer=%s",
                state,
                accept_header,
                sec_fetch_mode,
                origin,
                referer,
            )
            raise HTTPException(status_code=400, detail="Invalid or expired OAuth state (navigate phase).")

        redirect_with_params = _build_redirect_with_params(redirect_uri, {"code": code, "state": state})
        logger.info("OAuth navigate phase redirect | provider=%s origin=%s referer=%s", provider, origin, referer)
        return RedirectResponse(url=redirect_with_params)

    # Validate CSRF state and get redirect URI (without consuming yet)
    redirect_uri = _get_redirect_uri(state, consume=False)
    if redirect_uri is None:
        logger.warning(
            "Invalid or consumed OAuth state (JSON phase): %s | headers accept=%s mode=%s origin=%s referer=%s",
            state,
            accept_header,
            sec_fetch_mode,
            origin,
            referer,
        )
        # Return JSON error for fetch requests to avoid CORS issues with redirects
        raise HTTPException(status_code=401, detail="OAuth state expired or invalid. Please try logging in again.")

    try:
        oauth_service = create_oauth_service(db)
        tokens = await oauth_service.authenticate_with_code(provider, code)

        access_token = tokens.get("access_token")
        refresh_token = tokens.get("refresh_token")

        if not access_token or not refresh_token:
            logger.error("OAuth provider response missing tokens")
            _get_redirect_uri(state, consume=True)  # Consume state to prevent replay
            raise HTTPException(status_code=500, detail="Failed to issue authentication tokens")

        access_expires_at = _extract_access_expiry(access_token)

        payload_model = schemas.OAuthCallbackOut(
            access_token=access_token,
            refresh_token=refresh_token,
            access_expires_at=access_expires_at,
            token_type=tokens.get("token_type", "bearer"),
            redirect_uri=redirect_uri,
        )

        response = JSONResponse(content=jsonable_encoder(payload_model))
        _set_refresh_cookie(response, refresh_token)

        # Set CSRF token on successful OAuth authentication
        csrf_token = get_csrf_token(request)
        secure = settings.ENV.lower() in {"prod", "production"}
        set_csrf_cookie(response, csrf_token, secure=secure)

        # Only consume state after successful authentication
        _get_redirect_uri(state, consume=True)

        logger.info("OAuth callback success | provider=%s", provider)
        oauth_login_success()
        return response

    except OAuthProviderError as e:
        logger.error("OAuth authentication failed | provider=%s error=%s", provider, e)
        _get_redirect_uri(state, consume=True)  # Consume state to prevent replay
        # Pass through the specific error message (e.g. "No account found...")
        raise HTTPException(
            status_code=400,
            detail=str(e),
        )
    except Exception:
        logger.exception("OAuth callback unexpected error | provider=%s", provider)
        _get_redirect_uri(state, consume=True)  # Consume state to prevent replay
        # Return JSON error for unexpected errors
        raise HTTPException(status_code=500, detail="An error occurred during authentication. Please try again.")


@router.post("/{provider}/revoke")
def revoke_oauth_access(
    provider: str,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
) -> dict:
    """
    Revoke OAuth access for user.

    Removes OAuth provider link from user account.
    User can still login with password if set.

    Args:
        provider: OAuth provider to unlink
        current_user_id: Authenticated user ID

    Returns:
        {"message": "OAuth access revoked"}

    Note: Not fully implemented - requires OAuth token storage
    """
    logger.info("OAuth revocation requested for provider %s", provider)

    # TODO: Implement OAuth token storage and revocation
    # For now, just acknowledge the request
    return {
        "message": f"OAuth {provider} access revocation is pending implementation",
        "status": "not_implemented",
    }
