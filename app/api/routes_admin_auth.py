"""Admin authentication routes for support dashboard."""

import logging
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy.orm import Session

from app.api.rate_limit import limiter
from app.core.admin_security import (
    env_allowlist_entries,
    get_client_ip,
    invalidate_admin_allowlist_cache,
    ip_matches_networks,
    is_admin_ip_allowed,
    load_admin_networks,
    parse_networks,
    record_admin_login_event,
)
from app.core.config import settings
from app.core.security import TokenType, create_access_token, decode_token, hash_password
from app.db.session import get_db
from app.models.admin_models import AdminIpAllowlistEntry, AdminLoginAudit, AdminUser
from app.services.otp_service import OTPService

router = APIRouter(prefix="/admin/auth", tags=["admin-auth"])
logger = logging.getLogger(__name__)

ADMIN_COOKIE_NAME = "suoops.admin"


def _admin_cookie_settings() -> dict[str, object]:
    secure = settings.ENV.lower() in {"prod", "production"}
    samesite = "strict" if secure else "lax"
    return {
        "httponly": True,
        "secure": secure,
        "samesite": samesite,
        "max_age": 60 * 60 * 2,  # 2 hours
        "path": "/",
    }


def _set_admin_cookie(response: Response, token: str) -> None:
    response.set_cookie(ADMIN_COOKIE_NAME, token, **_admin_cookie_settings())


def _clear_admin_cookie(response: Response) -> None:
    response.delete_cookie(ADMIN_COOKIE_NAME, path="/")


# Default admin email — password MUST come from env var, never hardcoded
DEFAULT_ADMIN_EMAIL = "support@suoops.com"


# ============================================================================
# Schemas
# ============================================================================


class AdminOTPRequest(BaseModel):
    """Step 1 of passwordless login: request a one-time code by email."""

    email: EmailStr


class AdminOTPVerify(BaseModel):
    """Step 2 of passwordless login: verify the emailed one-time code."""

    email: EmailStr
    otp: str


class AdminLoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: dict


class AdminInviteRequest(BaseModel):
    email: EmailStr
    name: str
    can_manage_tickets: bool = True
    can_view_users: bool = True
    can_view_analytics: bool = True
    can_invite_admins: bool = False


class AdminInviteResponse(BaseModel):
    success: bool
    message: str
    invite_link: str | None = None


class AcceptInviteRequest(BaseModel):
    token: str


class AdminUserOut(BaseModel):
    id: int
    email: str
    name: str
    is_active: bool
    is_super_admin: bool
    can_manage_tickets: bool
    can_view_users: bool
    can_view_analytics: bool
    can_invite_admins: bool
    last_login: datetime | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SuccessMessageOut(BaseModel):
    success: bool
    message: str


class AdminLoginAuditOut(BaseModel):
    id: int
    admin_id: int | None
    email: str | None
    ip: str | None
    user_agent: str | None
    status: str
    event: str
    reason: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminIpAllowlistEntryOut(BaseModel):
    id: int
    cidr: str
    label: str | None
    created_by_id: int | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AdminIpAllowlistCreate(BaseModel):
    cidr: str
    label: str | None = None


# ============================================================================
# Authentication Dependencies
# ============================================================================

security = HTTPBearer(auto_error=False)


async def get_current_admin(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: Session = Depends(get_db),
) -> AdminUser:
    """Dependency to get current admin from Bearer header or httpOnly cookie.

    Admin tokens have 'admin:' prefix in the subject.
    """
    # Try Bearer header first, then fall back to httpOnly cookie
    raw_token = None
    if credentials and credentials.credentials:
        raw_token = credentials.credentials
    else:
        raw_token = request.cookies.get(ADMIN_COOKIE_NAME)

    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
        )

    try:
        payload = decode_token(raw_token, TokenType.ACCESS)
        subject = payload.get("sub", "")

        # Admin tokens have 'admin:' prefix
        if not subject.startswith("admin:"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not an admin token",
            )

        admin_id = int(subject.replace("admin:", ""))
        admin = db.query(AdminUser).filter(AdminUser.id == admin_id).first()

        if not admin or not admin.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Admin not found or inactive",
            )

        return admin
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Admin auth error: %s", e)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authentication",
        )


# ============================================================================
# Helper Functions


def create_default_admin(db: Session) -> AdminUser | None:
    """Create the default admin if it doesn't exist.

    Admin login is passwordless (email OTP), so no password is needed. The
    account is created with an unusable random password hash purely to satisfy
    the non-null column; it can never be used to authenticate.
    """
    existing = db.query(AdminUser).filter(AdminUser.email == DEFAULT_ADMIN_EMAIL).first()
    if existing:
        return existing

    admin = AdminUser(
        email=DEFAULT_ADMIN_EMAIL,
        name="SuoOps Support",
        hashed_password=hash_password(secrets.token_urlsafe(32)),  # unusable, OTP-only login
        is_active=True,
        is_super_admin=True,
        can_manage_tickets=True,
        can_view_users=True,
        can_view_analytics=True,
        can_invite_admins=True,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    logger.info("Created default admin: %s", DEFAULT_ADMIN_EMAIL)
    return admin


def send_admin_invite_email(to_email: str, name: str, invite_link: str) -> bool:
    """Send admin invitation email."""
    try:
        if not settings.SMTP_HOST:
            logger.warning("SMTP not configured, skipping invite email")
            return False

        msg = MIMEMultipart("alternative")
        msg["Subject"] = "You've been invited to SuoOps Admin"
        msg["From"] = settings.FROM_EMAIL or "noreply@suoops.com"
        msg["To"] = to_email

        html = f"""
        <!DOCTYPE html>
        <html>
        <head>
            <meta charset="utf-8">
            <style>
                body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
                .container {{ max-width: 600px; margin: 0 auto; padding: 40px 20px; }}
                .btn {{
                    display: inline-block;
                    padding: 14px 28px;
                    background: #10b981;
                    color: white;
                    text-decoration: none;
                    border-radius: 8px;
                    font-weight: 600;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <h1 style="color: #0f172a;">Welcome to SuoOps Admin</h1>
                <p style="color: #64748b; font-size: 16px;">Hi {name},</p>
                <p style="color: #64748b; font-size: 16px;">
                    You've been invited to join the SuoOps support admin team. 
                    Click the button below to set up your account:
                </p>
                <p style="margin: 30px 0;">
                    <a href="{invite_link}" class="btn">Accept Invitation</a>
                </p>
                <p style="color: #94a3b8; font-size: 14px;">
                    This invitation expires in 7 days. If you didn't expect this invitation, 
                    you can safely ignore this email.
                </p>
                <hr style="border: none; border-top: 1px solid #e2e8f0; margin: 30px 0;">
                <p style="color: #94a3b8; font-size: 12px;">
                    © 2024 SuoOps. All rights reserved.
                </p>
            </div>
        </body>
        </html>
        """

        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT) as server:
            server.starttls()  # Always use TLS for security
            if settings.SMTP_USER and settings.SMTP_PASSWORD:
                server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)

        return True
    except Exception as e:
        logger.error("Failed to send admin invite email: %s", e)
        return False


# ============================================================================
# Routes
# ============================================================================

ADMIN_OTP_PURPOSE = "admin_login"


def _build_login_response(admin: AdminUser) -> JSONResponse:
    """Issue an admin access token + cookie and return the login response."""
    token = create_access_token(
        subject=f"admin:{admin.id}",
        expires_minutes=60 * 2,  # 2 hours
    )
    response_data = AdminLoginResponse(
        access_token=token,
        user={
            "id": admin.id,
            "email": admin.email,
            "name": admin.name,
            "role": "admin",
            "is_super_admin": admin.is_super_admin,
            "permissions": {
                "can_manage_tickets": admin.can_manage_tickets,
                "can_view_users": admin.can_view_users,
                "can_view_analytics": admin.can_view_analytics,
                "can_invite_admins": admin.can_invite_admins,
            },
        },
    )
    response = JSONResponse(content=jsonable_encoder(response_data))
    _set_admin_cookie(response, token)
    return response


@router.post("/request-otp", response_model=SuccessMessageOut)
@limiter.limit("3/minute;10/hour")
def admin_request_otp(request: Request, payload: AdminOTPRequest, db: Session = Depends(get_db)):
    """Passwordless login step 1: email a one-time code to an admin.

    Always returns a generic success message so the endpoint cannot be used to
    enumerate which @suoops.com addresses are admins.
    """
    email_lower = payload.email.lower().strip()

    generic = {"success": True, "message": "If that address is an admin, a code has been sent."}

    # Only @suoops.com addresses can ever be admins.
    if not email_lower.endswith("@suoops.com"):
        record_admin_login_event(
            db,
            request=request,
            status="failure",
            event="otp_requested",
            email=email_lower,
            reason="bad_domain",
        )
        return generic

    admin = db.query(AdminUser).filter(AdminUser.email == email_lower).first()

    # Bootstrap the default admin on first use if no admin exists yet.
    if not admin and email_lower == DEFAULT_ADMIN_EMAIL:
        if db.query(AdminUser.id).first() is None:
            admin = create_default_admin(db)

    if not admin or not admin.is_active:
        record_admin_login_event(
            db,
            request=request,
            status="failure",
            event="otp_requested",
            email=email_lower,
            reason="unknown_admin",
        )
        return generic

    try:
        OTPService().send_code(email_lower, ADMIN_OTP_PURPOSE)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to send admin login OTP to %s: %s", email_lower, exc)
        record_admin_login_event(
            db,
            request=request,
            status="failure",
            event="otp_requested",
            admin_id=admin.id,
            email=email_lower,
            reason="delivery_failed",
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Could not send the login code. Please try again shortly.",
        )

    record_admin_login_event(
        db,
        request=request,
        status="success",
        event="otp_requested",
        admin_id=admin.id,
        email=email_lower,
    )
    return generic


@router.post("/verify-otp", response_model=AdminLoginResponse)
@limiter.limit("5/minute;20/hour")
def admin_verify_otp(request: Request, payload: AdminOTPVerify, db: Session = Depends(get_db)):
    """Passwordless login step 2: verify the emailed code and issue a session."""
    email_lower = payload.email.lower().strip()
    code = payload.otp.strip()

    if not email_lower.endswith("@suoops.com"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only @suoops.com email addresses can access the admin panel",
        )

    admin = db.query(AdminUser).filter(AdminUser.email == email_lower).first()
    if not admin or not admin.is_active:
        record_admin_login_event(
            db,
            request=request,
            status="failure",
            event="login",
            email=email_lower,
            reason="unknown_admin",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired code",
        )

    if not OTPService().verify_otp(email_lower, code, ADMIN_OTP_PURPOSE):
        record_admin_login_event(
            db,
            request=request,
            status="failure",
            event="login",
            admin_id=admin.id,
            email=email_lower,
            reason="bad_otp",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired code",
        )

    admin.last_login = datetime.now(timezone.utc)
    db.commit()

    record_admin_login_event(
        db,
        request=request,
        status="success",
        event="login",
        admin_id=admin.id,
        email=email_lower,
    )
    return _build_login_response(admin)


@router.post("/invite", response_model=AdminInviteResponse)
@limiter.limit("3/minute")
def invite_admin(
    request: Request,
    payload: AdminInviteRequest,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Invite a new admin user. Requires admin authentication with invite permission."""
    if not current_admin.is_super_admin and not current_admin.can_invite_admins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to invite admins",
        )

    # Validate email domain - only @suoops.com emails can be invited
    email_lower = payload.email.lower()
    if not email_lower.endswith("@suoops.com"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only @suoops.com email addresses can be invited as admins",
        )

    # Check if email already exists
    existing = db.query(AdminUser).filter(AdminUser.email == email_lower).first()
    if existing:
        # If the admin is active, don't allow re-invite
        if existing.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="An active admin with this email already exists",
            )
        # If pending (not active), allow re-invite - update the token and resend
        invite_token = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(days=7)
        existing.name = payload.name
        existing.invite_token = invite_token
        existing.invite_expires_at = expires_at
        existing.can_manage_tickets = payload.can_manage_tickets
        existing.can_view_users = payload.can_view_users
        existing.can_view_analytics = payload.can_view_analytics
        existing.can_invite_admins = payload.can_invite_admins
        db.commit()

        invite_link = f"https://support.suoops.com/admin/accept-invite?token={invite_token}"
        email_sent = send_admin_invite_email(payload.email, payload.name, invite_link)

        return AdminInviteResponse(
            success=True,
            message="Invitation re-sent successfully" if email_sent else "Invitation updated (email not sent)",
            invite_link=invite_link,
        )

    # Generate invite token
    invite_token = secrets.token_urlsafe(32)
    expires_at = datetime.now(timezone.utc) + timedelta(days=7)

    # Create pending admin
    admin = AdminUser(
        email=payload.email.lower(),
        name=payload.name,
        hashed_password="",  # Will be set when accepting invite
        is_active=False,  # Activated when accepting invite
        can_manage_tickets=payload.can_manage_tickets,
        can_view_users=payload.can_view_users,
        can_view_analytics=payload.can_view_analytics,
        can_invite_admins=payload.can_invite_admins,
        invite_token=invite_token,
        invite_expires_at=expires_at,
    )
    db.add(admin)
    db.commit()

    # Generate invite link
    invite_link = f"https://support.suoops.com/admin/accept-invite?token={invite_token}"

    # Send email
    email_sent = send_admin_invite_email(payload.email, payload.name, invite_link)

    return AdminInviteResponse(
        success=True,
        message="Invitation sent successfully" if email_sent else "Invitation created (email not sent)",
        invite_link=invite_link,
    )


@router.post("/accept-invite", response_model=AdminLoginResponse)
@limiter.limit("3/minute")
def accept_invite(request: Request, payload: AcceptInviteRequest, db: Session = Depends(get_db)):
    """Accept an admin invitation and activate the account (passwordless)."""
    admin = db.query(AdminUser).filter(AdminUser.invite_token == payload.token).first()

    if not admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid or expired invitation",
        )

    if admin.invite_expires_at and admin.invite_expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation has expired",
        )

    if admin.is_active:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invitation already accepted",
        )

    # Activate admin. Login is passwordless (email OTP); store an unusable
    # random hash purely to satisfy the non-null column.
    admin.hashed_password = hash_password(secrets.token_urlsafe(32))
    admin.is_active = True
    admin.invite_token = None
    admin.invite_expires_at = None
    admin.last_login = datetime.now(timezone.utc)
    db.commit()

    record_admin_login_event(
        db,
        request=request,
        status="success",
        event="invite_accepted",
        admin_id=admin.id,
        email=admin.email,
    )
    return _build_login_response(admin)


@router.post("/logout")
def admin_logout():
    """Clear admin authentication cookie."""
    response = JSONResponse(content={"success": True, "message": "Logged out"})
    _clear_admin_cookie(response)
    return response


@router.get("/me")
def get_current_admin_user(
    current_admin: AdminUser = Depends(get_current_admin),
):
    """Get current admin user info and issue a fresh access token.

    On page refresh the frontend loses the in-memory JWT but the httpOnly
    cookie still authenticates. This endpoint returns a fresh token so the
    frontend can restore its in-memory state.
    """
    fresh_token = create_access_token(
        subject=f"admin:{current_admin.id}",
        expires_minutes=60 * 2,
    )

    data = jsonable_encoder(AdminUserOut.model_validate(current_admin))
    data["access_token"] = fresh_token

    response = JSONResponse(content=data)
    _set_admin_cookie(response, fresh_token)
    return response


@router.get("/admins", response_model=list[AdminUserOut])
def list_admins(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """List all admin users. Requires admin authentication."""
    if not current_admin.is_super_admin and not current_admin.can_invite_admins:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins or admins with invite permission can list admins",
        )
    admins = db.query(AdminUser).filter(AdminUser.is_active.is_(True)).all()
    return admins


@router.delete("/admins/{admin_id}", response_model=SuccessMessageOut)
def remove_admin(
    admin_id: int,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Remove an admin user. Only super admins can perform this action."""
    # Only super admins can remove admins
    if not current_admin.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can remove admin users",
        )

    # Find the admin to remove
    admin_to_remove = db.query(AdminUser).filter(AdminUser.id == admin_id).first()
    if not admin_to_remove:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Admin not found",
        )

    # Prevent self-deletion
    if admin_to_remove.id == current_admin.id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove yourself",
        )

    # Prevent removing other super admins
    if admin_to_remove.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot remove another super admin",
        )

    # Hard delete the admin
    db.delete(admin_to_remove)
    db.commit()

    logger.info("Admin %s removed admin %s", current_admin.email, admin_to_remove.email)

    return {"success": True, "message": f"Admin {admin_to_remove.email} has been removed"}


@router.get("/login-audit", response_model=list[AdminLoginAuditOut])
def list_login_audit(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
    limit: int = 100,
):
    """Return recent admin authentication events (logins, failures, OTP requests).

    Lets admins spot logins from unexpected IPs. Restricted to super admins.
    """
    if not current_admin.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can view the login audit log",
        )
    limit = max(1, min(limit, 500))
    return db.query(AdminLoginAudit).order_by(AdminLoginAudit.created_at.desc()).limit(limit).all()


# ============================================================================
# IP allowlist (network-based access control)
# ============================================================================


@router.get("/ip-allowed")
def admin_ip_allowed(request: Request, db: Session = Depends(get_db)):
    """Public verdict used by the frontend to gate the /admin pages.

    Returns whether the requesting IP may access the admin panel. This route is
    intentionally exempt from the IP-allowlist middleware so a blocked client
    still receives a clean ``{"allowed": false}`` answer (rather than a 403),
    letting the dashboard show a friendly "blocked" page.

    The admin frontend middleware calls this SERVER-SIDE and forwards the real
    visitor IP in ``X-Client-IP`` (a header intermediary proxies don't rewrite,
    unlike X-Forwarded-For). We honor it when present so the verdict matches what
    the visitor's own browser would get — otherwise the extra Vercel→Cloudflare
    →Render hop makes us read a proxy IP and wrongly block the visitor. This
    endpoint only returns a verdict; the admin routes + API enforce the allowlist
    authoritatively via the real client IP.
    """
    forwarded = (request.headers.get("x-client-ip") or "").strip()
    ip = forwarded or get_client_ip(request)
    return {"allowed": is_admin_ip_allowed(ip, db), "ip": ip}


@router.get("/ip-allowlist", response_model=list[AdminIpAllowlistEntryOut])
def list_ip_allowlist(
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """List IP addresses/ranges allowed to access the admin panel (super admin)."""
    if not current_admin.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can manage the IP allowlist",
        )
    return db.query(AdminIpAllowlistEntry).order_by(AdminIpAllowlistEntry.created_at.desc()).all()


@router.post("/ip-allowlist", response_model=AdminIpAllowlistEntryOut)
def add_ip_allowlist_entry(
    request: Request,
    payload: AdminIpAllowlistCreate,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Add an IP/CIDR to the admin allowlist (super admin).

    Guards against lock-out: the entry is rejected if applying it would block
    the IP the requesting admin is currently connecting from.
    """
    if not current_admin.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can manage the IP allowlist",
        )

    raw = (payload.cidr or "").strip()
    networks = parse_networks([raw])
    if not networks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Enter a valid IP address or range, e.g. 203.0.113.10 or 203.0.113.0/24",
        )
    normalized = str(networks[0])

    if db.query(AdminIpAllowlistEntry).filter(AdminIpAllowlistEntry.cidr == normalized).first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{normalized} is already on the allowlist",
        )

    # Lock-out guard: the resulting allowlist must still include the caller.
    caller_ip = get_client_ip(request)
    resulting = load_admin_networks(db) + networks
    if not ip_matches_networks(caller_ip, resulting):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"This would block your current IP ({caller_ip or 'unknown'}). "
                "Add your own IP/range first so you don't lock yourself out."
            ),
        )

    entry = AdminIpAllowlistEntry(
        cidr=normalized,
        label=(payload.label or None),
        created_by_id=current_admin.id,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    invalidate_admin_allowlist_cache()

    record_admin_login_event(
        db,
        request=request,
        status="success",
        event="ip_allowlist_add",
        admin_id=current_admin.id,
        email=current_admin.email,
        reason=normalized,
    )
    logger.info("Admin %s added IP allowlist entry %s", current_admin.email, normalized)
    return entry


@router.delete("/ip-allowlist/{entry_id}", response_model=SuccessMessageOut)
def delete_ip_allowlist_entry(
    entry_id: int,
    request: Request,
    current_admin: AdminUser = Depends(get_current_admin),
    db: Session = Depends(get_db),
):
    """Remove an IP/CIDR from the admin allowlist (super admin).

    Guards against lock-out: removal is rejected if the remaining allowlist
    would block the requesting admin's current IP.
    """
    if not current_admin.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only super admins can manage the IP allowlist",
        )

    entry = db.query(AdminIpAllowlistEntry).filter(AdminIpAllowlistEntry.id == entry_id).first()
    if not entry:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Allowlist entry not found",
        )

    # Compute what the allowlist would be without this entry.
    remaining_raw = [
        row[0] for row in db.query(AdminIpAllowlistEntry.cidr).filter(AdminIpAllowlistEntry.id != entry_id).all()
    ]
    remaining = parse_networks(env_allowlist_entries() + remaining_raw)
    caller_ip = get_client_ip(request)
    # If entries remain, the caller must still be covered; an empty result
    # disables the allowlist entirely (everyone allowed), which is safe.
    if remaining and not ip_matches_networks(caller_ip, remaining):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"Removing this entry would block your current IP ({caller_ip or 'unknown'}). "
                "Add a range that covers you before removing it."
            ),
        )

    removed = entry.cidr
    db.delete(entry)
    db.commit()
    invalidate_admin_allowlist_cache()

    record_admin_login_event(
        db,
        request=request,
        status="success",
        event="ip_allowlist_remove",
        admin_id=current_admin.id,
        email=current_admin.email,
        reason=removed,
    )
    logger.info("Admin %s removed IP allowlist entry %s", current_admin.email, removed)
    return {"success": True, "message": f"{removed} removed from the allowlist"}


# ============================================================================
# Startup: Create default admin
# ============================================================================


def init_default_admin():
    """Initialize default admin on startup. Called from main.py."""
    from app.db.session import SessionLocal

    db = SessionLocal()
    try:
        admin = create_default_admin(db)
        if admin:
            logger.info("Default admin ready: %s", DEFAULT_ADMIN_EMAIL)
    except Exception as e:
        logger.error("Failed to create default admin: %s", e)
    finally:
        db.close()
