"""Phone linking endpoints for WhatsApp integration.

Allows users to link their WhatsApp phone number to their account.
When a phone number is saved, it is automatically verified — no OTP needed.
The user already proved their identity during signup.

Endpoints:
1. POST /users/me/phone   - Save phone number (auto-verified)
2. DELETE /users/me/phone  - Remove phone from account
"""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.rate_limit import limiter
from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import models, schemas
from app.utils.phone import normalize_phone as _normalize_phone

logger = logging.getLogger(__name__)
router = APIRouter(tags=["users"])


@router.post("/me/phone/request-otp", response_model=schemas.MessageOut)
@limiter.limit("5/minute")
def request_phone_change_otp(
    request: Request,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Send a step-up code to the CURRENT phone/email to authorise changing the
    login phone number (protects against a hijacked session rerouting it)."""
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    identifier = user.phone or user.email
    if not identifier:
        raise HTTPException(status_code=400, detail="No phone or email on file to send a code to.")
    from app.services.otp_service import OTPService

    try:
        OTPService().send_code(identifier, purpose="phone_change")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send phone-change OTP for user %s", current_user_id)
        raise HTTPException(status_code=502, detail="Could not send your confirmation code. Please try again.") from exc
    channel = "email" if (user.email and not user.phone) else "WhatsApp"
    return schemas.MessageOut(detail=f"Confirmation code sent to your {channel}.")


@router.post("/me/phone", response_model=schemas.PhoneVerificationResponse)
@limiter.limit("5/minute")
def save_phone_number(
    request: Request,
    payload: schemas.PhoneVerificationRequest,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Save/link a phone number for WhatsApp bot access.

    First-time linking is frictionless (verified when the user messages the bot
    from the number). CHANGING an existing phone requires a step-up OTP sent to
    the CURRENT phone/email — the phone is the login identity, so a hijacked
    session must not be able to silently point it at an attacker's number.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    normalized_phone = _normalize_phone(payload.phone)
    if not normalized_phone or not normalized_phone.strip():
        raise HTTPException(status_code=400, detail="Phone number is required")

    # Check if phone is already used by another user
    existing = (
        db.query(models.User)
        .filter(
            models.User.phone == normalized_phone,
            models.User.id != current_user_id,
        )
        .first()
    )
    if existing:
        raise HTTPException(
            status_code=400,
            detail="This phone number is already linked to another account",
        )

    old_phone = user.phone
    is_change = bool(old_phone) and normalized_phone != old_phone

    # Step-up auth: changing the login phone requires an OTP sent to the CURRENT
    # phone/email (which a hijacked session doesn't control).
    if is_change:
        from app.services.otp_service import OTPService

        identifier = old_phone or user.email
        if (
            not payload.otp
            or not identifier
            or not OTPService().verify_otp(identifier, payload.otp, purpose="phone_change")
        ):
            raise HTTPException(
                status_code=401,
                detail="A valid confirmation code is required to change your phone number.",
            )

    user.phone = normalized_phone
    user.phone_verified = False  # Verified when user messages the bot from this number
    user.phone_otp = None
    db.commit()

    # Security alert to the OLD number (best-effort): make a takeover attempt
    # visible to the previous owner.
    if is_change and old_phone:
        try:
            from app.bot.whatsapp_client import WhatsAppClient

            WhatsAppClient().send_text(
                old_phone,
                "⚠️ Your SuoOps login phone number was just changed. If this wasn't "
                "you, contact support@suoops.com immediately.",
            )
        except Exception:  # noqa: BLE001
            logger.exception("Failed to send phone-change alert to old number")

    logger.info(
        "Phone saved (change=%s, pending verification) for user %s: %s",
        is_change,
        current_user_id,
        normalized_phone,
    )
    return schemas.PhoneVerificationResponse(
        detail="Phone number saved! Now message our WhatsApp bot to activate it.",
        phone=normalized_phone,
    )


# ── Legacy endpoints (backward compatibility) ────────────────────────
# Keep these so old frontend versions don't break during rollout.


@router.post("/me/phone/request", response_model=schemas.MessageOut, include_in_schema=False)
def request_phone_otp_legacy(
    request: Request,
    payload: schemas.PhoneVerificationRequest,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Legacy: save phone + auto-verify (no OTP sent)."""
    result = save_phone_number(request, payload, current_user_id, db)
    return schemas.MessageOut(detail=result.detail)


@router.post("/me/phone/send-otp", response_model=schemas.MessageOut, include_in_schema=False)
def send_phone_otp_legacy(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Legacy: no-op, phone is already verified on save."""
    return schemas.MessageOut(detail="Phone is already verified.")


@router.post("/me/phone/verify", response_model=schemas.PhoneVerificationResponse, include_in_schema=False)
def verify_phone_legacy(
    payload: schemas.PhoneVerificationVerify,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Legacy: phone is already verified on save, return current state."""
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user or not user.phone:
        raise HTTPException(status_code=400, detail="No phone number on account")
    return schemas.PhoneVerificationResponse(
        detail="Phone verified successfully! You can now create invoices via WhatsApp.",
        phone=user.phone,
    )


@router.delete("/me/phone", response_model=schemas.MessageOut)
def remove_phone_number(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Phone numbers cannot be removed — only replaced.

    A phone is required on every account so we can deliver invoices via WhatsApp
    and prevent the same number from being recycled to create a second account.
    To change phones, POST /me/phone with the new number; the old one is replaced
    atomically.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not user.phone:
        raise HTTPException(status_code=404, detail="No phone number configured")
    raise HTTPException(
        status_code=400,
        detail=("Phone numbers can't be removed — only replaced. " "Save a new phone number to change it."),
    )
