from datetime import datetime, timedelta, timezone
from typing import Annotated

import sentry_sdk
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app import metrics
from app.api.rate_limit import RATE_LIMITS, limiter
from app.core.audit import log_audit_event, log_failure
from app.core.config import settings
from app.core.csrf import get_csrf_token, set_csrf_cookie
from app.core.security import TokenExpiredError, TokenValidationError, decode_token
from app.models import schemas
from app.services.auth_service import AuthService, TokenBundle, get_auth_service

router = APIRouter()

AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]


# Allow more generous registration throughput outside prod to keep tests/dev smooth.
REGISTER_RATE_LIMIT = "5/minute" if settings.ENV.lower() == "prod" else "50/minute"


@router.post("/signup/request", response_model=schemas.MessageOut)
@limiter.limit(RATE_LIMITS["signup_request"])
def request_signup(request: Request, payload: schemas.SignupStart, svc: AuthServiceDep):
    """Request signup OTP.

    The code is delivered to the user's EMAIL (WhatsApp only as a fallback). The
    WhatsApp number is verified separately after signup, before the dashboard.
    """
    from app.core.admin_security import get_client_ip

    try:
        channel = svc.start_signup(
            payload,
            ip=get_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
        metrics.otp_signup_requested()
        if channel == "email":
            return schemas.MessageOut(detail="OTP sent to email")
        return schemas.MessageOut(detail="OTP sent to WhatsApp")

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _bundle_to_response(
    bundle: TokenBundle,
    request: Request | None = None,
    include_refresh_cookie: bool = True,
) -> JSONResponse:
    token_out = schemas.TokenOut(
        access_token=bundle.access_token,
        access_expires_at=bundle.access_expires_at,
        refresh_token=bundle.refresh_token,
    )
    response = JSONResponse(content=jsonable_encoder(token_out))
    if include_refresh_cookie:
        _set_refresh_cookie(response, bundle.refresh_token)

    # Set CSRF token on successful authentication
    if request:
        csrf_token = get_csrf_token(request)
        secure = settings.ENV.lower() in {"prod", "production"}
        set_csrf_cookie(response, csrf_token, secure=secure)

    return response


REFRESH_COOKIE_NAME = "whatsinvoice.refresh"


def _cookie_settings() -> dict[str, object]:
    secure = settings.ENV.lower() in {"prod", "production"}
    lifespan = timedelta(days=14)
    max_age = int(lifespan.total_seconds())
    expires = datetime.now(timezone.utc) + lifespan
    # Stricter SameSite policy in production to mitigate CSRF; keep lax elsewhere for local dev
    samesite = "strict" if secure else "lax"
    return {
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "max_age": max_age,
        "expires": expires,
        "path": "/",
    }


def _set_refresh_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(REFRESH_COOKIE_NAME, token, **_cookie_settings())


def _clear_refresh_cookie(response: JSONResponse) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/")


@router.post("/signup/verify", response_model=schemas.TokenOut)
@limiter.limit(RATE_LIMITS["signup_verify"])
def verify_signup(request: Request, payload: schemas.SignupVerify, svc: AuthServiceDep):
    try:
        bundle = svc.complete_signup(payload)
        metrics.otp_signup_verified()
        log_audit_event("auth.signup.verify", user_id=bundle.user_id)
        return _bundle_to_response(bundle, request=request)
    except ValueError as exc:
        log_failure("auth.signup.verify", user_id=None, error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/login/request", response_model=schemas.MessageOut)
@limiter.limit(RATE_LIMITS["login_request"])
def request_login(request: Request, payload: schemas.OTPPhoneRequest | schemas.OTPEmailRequest, svc: AuthServiceDep):
    """Request login OTP. Delivered to email when available (WhatsApp fallback)."""
    try:
        channel = svc.request_login(payload)
        metrics.otp_login_requested()
        log_audit_event("auth.login.request", user_id=None, method=channel)

        if channel == "email":
            return schemas.MessageOut(detail="OTP sent to email")
        return schemas.MessageOut(detail="OTP sent to WhatsApp")

    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/login/verify", response_model=schemas.TokenOut)
@limiter.limit(RATE_LIMITS["login_verify"])
def verify_login(request: Request, payload: schemas.LoginVerify, svc: AuthServiceDep):
    try:
        bundle = svc.verify_login(payload)
        metrics.otp_login_verified()
        log_audit_event("auth.login.verify", user_id=bundle.user_id)
        return _bundle_to_response(bundle, request=request)
    except ValueError as exc:
        log_failure("auth.login.verify", user_id=None, error=str(exc))
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/otp/resend", response_model=schemas.MessageOut)
@limiter.limit(RATE_LIMITS["otp_resend"])
def resend_otp(request: Request, payload: schemas.OTPResend, svc: AuthServiceDep):
    """Resend OTP for phone OR email."""
    try:
        metrics.otp_resend_attempt()
        channel = svc.resend_otp(payload)
        log_audit_event("auth.otp.resend", user_id=None, method=channel)

        if channel == "email":
            return schemas.MessageOut(detail="OTP resent to email")
        return schemas.MessageOut(detail="OTP resent to WhatsApp")

    except ValueError as exc:
        # Cooldown or other resend restriction
        metrics.otp_resend_blocked()
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@router.get("/otp/status")
@limiter.limit(RATE_LIMITS["otp_status"])
def otp_delivery_status(
    request: Request,
    svc: AuthServiceDep,
    purpose: str,
    phone: str | None = None,
    email: str | None = None,
):
    """Return delivery status for a pending OTP.

    Frontends poll this after requesting an OTP to detect WhatsApp delivery
    failures reported asynchronously via Meta's status webhook (e.g. the
    recipient is not a WhatsApp user, the business account has a payment
    issue, the number is in a restricted region, etc.).

    Response shape::

        {"state": "pending" | "failed" | "none",
         "code": "131026", "title": "...", "detail": "..."}
    """
    if purpose not in {"signup", "login"}:
        raise HTTPException(status_code=400, detail="Invalid purpose")
    if not phone and not email:
        raise HTTPException(status_code=400, detail="phone or email is required")
    if phone:
        try:
            identifier = svc._normalize_phone(phone)  # noqa: SLF001 - intentional reuse
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    else:
        identifier = (email or "").strip().lower()
        if not identifier:
            raise HTTPException(status_code=400, detail="email is required")
    return svc.otp.get_delivery_status(identifier, purpose)


@router.post("/refresh", response_model=schemas.TokenOut)
@limiter.limit(RATE_LIMITS["refresh"])
def refresh_token(request: Request, svc: AuthServiceDep, payload: schemas.RefreshRequest | None = None):
    refresh_value = request.cookies.get(REFRESH_COOKIE_NAME)
    if not refresh_value and payload and payload.refresh_token:
        refresh_value = payload.refresh_token
    if not refresh_value:
        log_failure("auth.refresh", user_id=None, error="missing_refresh_token")
        raise HTTPException(status_code=401, detail="Missing refresh token")

    # Check if the refresh token has been revoked (e.g. via logout)
    try:
        from app.core.token_blocklist import is_token_revoked
        from app.db.redis_client import get_redis_client

        if is_token_revoked(get_redis_client(), refresh_value):
            log_failure("auth.refresh", user_id=None, error="revoked_token")
            raise HTTPException(status_code=401, detail="Token has been revoked")
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        if settings.ENV.lower() in {"prod", "production"}:
            log_failure("auth.refresh", user_id=None, error="blocklist_unavailable")
            raise HTTPException(status_code=503, detail="Service temporarily unavailable")
        # In dev/test, fail-open since Redis may not be running

    try:
        # Revoke old refresh token before issuing new one (rotation)
        try:
            from app.core.security import TokenType, decode_token
            from app.core.token_blocklist import revoke_token
            from app.db.redis_client import get_redis_client

            old_payload = decode_token(refresh_value, expected_type=TokenType.REFRESH)
            from datetime import datetime, timezone

            old_exp = datetime.fromtimestamp(old_payload["exp"], tz=timezone.utc)
            revoke_token(get_redis_client(), refresh_value, expires_at=old_exp)
        except Exception:  # noqa: BLE001
            pass  # Best-effort; old token will expire naturally

        bundle = svc.refresh(refresh_value)
        # Extract user_id from the new access token
        from app.core.security import TokenType, decode_token

        token_payload = decode_token(bundle.access_token, expected_type=TokenType.ACCESS)
        user_id = int(token_payload["sub"])
        log_audit_event("auth.refresh", user_id=user_id)
        return _bundle_to_response(bundle, request=request)
    except ValueError as exc:
        log_failure("auth.refresh", user_id=None, error=str(exc))
        raise HTTPException(status_code=401, detail=str(exc)) from exc


@router.post("/logout", response_model=schemas.MessageOut)
def logout(request: Request):
    response = JSONResponse(status_code=200, content={"detail": "Logged out"})

    # Revoke the refresh token server-side so it can't be reused
    refresh_value = request.cookies.get(REFRESH_COOKIE_NAME)
    if refresh_value:
        try:
            from app.core.security import TokenType, decode_token
            from app.core.token_blocklist import revoke_token
            from app.db.redis_client import get_redis_client

            payload = decode_token(refresh_value, expected_type=TokenType.REFRESH)
            from datetime import datetime, timezone

            expires_at = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)
            user_id = payload.get("sub")
            revoke_token(get_redis_client(), refresh_value, expires_at=expires_at)
            log_audit_event("auth.logout", user_id=int(user_id) if user_id else None)
        except Exception:  # noqa: BLE001
            # Token might already be expired or invalid — still clear the cookie
            log_audit_event("auth.logout", user_id=None)
    else:
        log_audit_event("auth.logout", user_id=None)

    _clear_refresh_cookie(response)
    return response


def get_current_user_id(authorization: str = Header(None)) -> int:
    if not authorization or not authorization.lower().startswith("bearer "):
        log_failure("auth.token.parse", user_id=None, error="missing_token")
        raise HTTPException(status_code=401, detail="Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = decode_token(token)
        user_id = int(payload["sub"])  # type: ignore

        # Set Sentry user context for better error tracking
        sentry_sdk.set_user({"id": user_id})

        return user_id
    except TokenExpiredError as exc:
        log_failure("auth.token.expired", user_id=None, error="expired")
        raise HTTPException(status_code=401, detail="Token expired") from exc
    except TokenValidationError as exc:
        log_failure("auth.token.invalid", user_id=None, error="invalid")
        raise HTTPException(status_code=401, detail="Invalid token") from exc


# Legacy password-based endpoints removed (migrated fully to OTP flows).
