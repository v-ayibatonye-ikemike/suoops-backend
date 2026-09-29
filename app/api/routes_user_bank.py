"""Bank detail endpoints split from routes_user.py."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.dependencies import AdminUserDep
from app.api.rate_limit import limiter
from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import models, schemas

logger = logging.getLogger(__name__)
router = APIRouter(tags=["users"])


class ResolveAccountIn(BaseModel):
    bank_name: str
    account_number: str


class ResolveAccountOut(BaseModel):
    account_name: str


@router.post("/me/resolve-bank-account", response_model=ResolveAccountOut)
@limiter.limit("20/minute")
async def resolve_bank_account(
    request: Request,
    data: ResolveAccountIn,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """Resolve a bank account holder's name via Paystack for the settings form.

    Lets the frontend auto-fill and verify the account name as the user types,
    so the saved name always matches the bank exactly.
    """
    from app.services.paystack_subaccount_service import (
        PaystackSubaccountService,
        SubaccountError,
    )

    account_number = (data.account_number or "").strip()
    if len(account_number) != 10 or not account_number.isdigit():
        raise HTTPException(status_code=400, detail="Enter a valid 10-digit account number.")
    if not (data.bank_name or "").strip():
        raise HTTPException(status_code=400, detail="Select a bank first.")

    try:
        svc = PaystackSubaccountService(db)
        bank_code = await svc.resolve_bank_code(data.bank_name)
        name = await svc.resolve_account(account_number, bank_code)
    except SubaccountError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return ResolveAccountOut(account_name=name)


@router.get("/me/bank-details", response_model=schemas.BankDetailsOut)
def get_bank_details(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    is_configured = bool(user.bank_name and user.account_number and user.account_name)
    return schemas.BankDetailsOut(
        business_name=user.business_name,
        bank_name=user.bank_name,
        account_number=user.account_number,
        account_name=user.account_name,
        is_configured=is_configured,
        online_payments_enabled=bool(getattr(user, "paystack_subaccount_active", False)),
    )


@router.post("/me/bank-details/request-otp", response_model=schemas.MessageOut)
@limiter.limit("5/minute")
def request_bank_change_otp(
    request: Request,
    current_user_id: AdminUserDep,
    db: Annotated[Session, Depends(get_db)],
):
    """Send a one-time code to confirm a change of bank details (step-up auth).

    Required before SETTING or CHANGING a payout account — protects against an
    attacker who has a hijacked session pointing the business's money at their
    own account.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    identifier = user.phone or user.email
    if not identifier:
        raise HTTPException(status_code=400, detail="No phone or email on file to send a code to.")

    from app.services.otp_service import OTPService

    try:
        OTPService().send_code(identifier, purpose="bank_change")
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to send bank-change OTP for user %s", current_user_id)
        raise HTTPException(status_code=502, detail="Could not send your confirmation code. Please try again.") from exc
    channel = "email" if (user.email and not user.phone) else "WhatsApp"
    return schemas.MessageOut(detail=f"Confirmation code sent to your {channel}.")


@router.patch("/me/bank-details", response_model=schemas.BankDetailsOut)
@limiter.limit("10/minute")
async def update_bank_details(
    request: Request,
    data: schemas.BankDetailsUpdate,
    current_user_id: AdminUserDep,
    db: Annotated[Session, Depends(get_db)],
):
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    update_data = data.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="At least one field must be provided")
    # Snapshot the payout-critical values so we can detect a SET or CHANGE of the
    # bank account. Setting a payout account (first-time included) or changing it
    # is a step-up event — a hijacked session on an account with no bank on file
    # must not be able to silently point payouts at an attacker.
    old_bank = (user.bank_name, user.account_number)
    intended_bank = (
        data.bank_name if data.bank_name is not None else user.bank_name,
        data.account_number if data.account_number is not None else user.account_number,
    )
    bank_is_written = bool(intended_bank[0] and intended_bank[1]) and intended_bank != old_bank

    # Step-up auth: setting or changing the payout account requires a fresh OTP.
    if bank_is_written:
        identifier = user.phone or user.email
        from app.services.otp_service import OTPService

        if not data.otp or not identifier or not OTPService().verify_otp(identifier, data.otp, purpose="bank_change"):
            raise HTTPException(
                status_code=401,
                detail="A valid confirmation code is required to set or change your bank details.",
            )

    # Never trust a client-supplied account NAME: re-resolve it server-side from
    # the bank + account number so the stored payout name always matches what the
    # bank actually returns. Best-effort — if the resolver is unreachable we fall
    # back to the submitted name rather than blocking a legitimate update.
    verified_name: str | None = None
    bank_or_acct_changed = data.bank_name is not None or data.account_number is not None
    intended_bank_name = data.bank_name if data.bank_name is not None else user.bank_name
    intended_account_number = data.account_number if data.account_number is not None else user.account_number
    if bank_or_acct_changed and intended_bank_name and intended_account_number:
        from app.services.paystack_subaccount_service import PaystackSubaccountService

        try:
            svc = PaystackSubaccountService(db)
            bank_code = await svc.resolve_bank_code(intended_bank_name)
            verified_name = await svc.resolve_account(intended_account_number, bank_code)
        except Exception:  # noqa: BLE001 — resolver down → fall back to submitted name
            logger.warning("Bank account name could not be verified for user %s", current_user_id)

    if data.business_name is not None:
        user.business_name = data.business_name
    if data.bank_name is not None:
        user.bank_name = data.bank_name
    if data.account_number is not None:
        user.account_number = data.account_number
    # Prefer the SERVER-VERIFIED name; fall back to the submitted one only if the
    # resolver was unavailable.
    if verified_name:
        user.account_name = verified_name
    elif data.account_name is not None:
        user.account_name = data.account_name

    # Single source of truth: the bank a seller sees/edits here IS where they get
    # paid. Mirror it into the payout fields so escrow/commission payouts always
    # follow the current account and never a stale separate payout account.
    if user.bank_name and user.account_number:
        user.payout_bank_name = user.bank_name
        user.payout_account_number = user.account_number
        user.payout_account_name = user.account_name or user.business_name or user.name
    db.commit()
    db.refresh(user)

    # Bank details drive escrow payouts — setting or changing the account is a
    # takeover risk, so invalidate the recipient, freeze payouts + alert the owner.
    if bank_is_written:
        from app.services.escrow_service import on_payout_details_changed

        on_payout_details_changed(db, user)
    is_configured = bool(user.bank_name and user.account_number and user.account_name)
    return schemas.BankDetailsOut(
        business_name=user.business_name,
        bank_name=user.bank_name,
        account_number=user.account_number,
        account_name=user.account_name,
        is_configured=is_configured,
        online_payments_enabled=bool(getattr(user, "paystack_subaccount_active", False)),
    )


@router.post("/me/bank-details", response_model=schemas.BankDetailsOut)
@limiter.limit("10/minute")
async def create_bank_details(
    request: Request,
    data: schemas.BankDetailsUpdate,
    current_user_id: AdminUserDep,
    db: Annotated[Session, Depends(get_db)],
):
    return await update_bank_details(request, data, current_user_id, db)


@router.delete("/me/bank-details", response_model=schemas.MessageOut)
@limiter.limit("10/minute")
def delete_bank_details(
    request: Request,
    current_user_id: AdminUserDep,
    db: Annotated[Session, Depends(get_db)],
):
    user = db.query(models.User).filter(models.User.id == current_user_id).one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.business_name = None
    user.bank_name = None
    user.account_number = None
    user.account_name = None
    # Single-account model: clearing the visible bank clears the payout account
    # too, so payouts can't silently go to a stale destination.
    user.payout_bank_name = None
    user.payout_account_number = None
    user.payout_account_name = None
    db.commit()
    return schemas.MessageOut(detail="Bank details cleared successfully")
