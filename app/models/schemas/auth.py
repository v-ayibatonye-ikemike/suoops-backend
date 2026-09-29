"""Authentication-related schemas."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class OTPPhoneRequest(BaseModel):
    phone: str


class OTPEmailRequest(BaseModel):
    """Request OTP via email (temporary for pre-launch)."""

    email: str


class SignupStart(BaseModel):
    """Start signup with WhatsApp phone number."""

    phone: str
    # Email is REQUIRED: login verification codes are delivered by email (WhatsApp
    # is reserved for the one-time number verification), so every account needs one.
    email: str = Field(
        ..., min_length=5, max_length=255, description="Email address (required — used for login verification codes)"
    )
    name: str
    business_name: str = Field(..., min_length=2, max_length=255, description="Business or brand name (required)")
    accept_terms: bool = Field(
        False,
        description=(
            "Whether the business accepted the Terms & Conditions "
            "(incl. buyer-protection/escrow policy). Must be true to sign up."
        ),
    )
    referral_code: str | None = Field(
        None, min_length=3, max_length=50, description="Referral code or influencer vanity slug from another user"
    )
    signup_source: str | None = Field(
        None,
        max_length=50,
        description=(
            "Attribution source: google_ads, instagram, whatsapp_ad, social_media, " "referral, google_oauth, organic"
        ),
    )
    device_fingerprint: str | None = Field(
        None, max_length=64, description="Client-generated device fingerprint (anti-fraud; hashed on the client)"
    )

    @field_validator("email")
    @classmethod
    def _valid_email(cls, v: str) -> str:
        v = v.strip().lower()
        if "@" not in v or "." not in v.split("@")[-1]:
            raise ValueError("Please enter a valid email address.")
        return v


class SignupVerify(BaseModel):
    """Verify signup OTP and provide bank details to complete registration."""

    phone: str
    otp: str = Field(..., min_length=6, max_length=6)
    bank_name: str = Field(..., min_length=2, max_length=100)
    account_number: str = Field(..., min_length=10, max_length=10)
    account_name: str = Field(..., min_length=2, max_length=255)


class LoginVerify(BaseModel):
    """Verify login OTP with phone OR email."""

    phone: str | None = None
    email: str | None = None
    otp: str = Field(..., min_length=6, max_length=6)


class OTPResend(BaseModel):
    """Resend OTP for phone OR email."""

    phone: str | None = None
    email: str | None = None
    purpose: Literal["signup", "login"]


class UserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    phone: str | None = None
    phone_verified: bool = False
    email: str | None = None
    name: str
    business_name: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    plan: str  # FREE, STARTER, PRO, BUSINESS
    invoice_balance: int = 0  # Legacy count (migrated into wallet)
    wallet_balance_kobo: int = 0  # Prepaid invoice wallet, in kobo
    invoices_this_month: int = 0  # Deprecated, kept for backward compat
    logo_url: str | None = None
    storefront_cover_url: str | None = None
    subscription_expires_at: dt.datetime | None = None
    subscription_started_at: dt.datetime | None = None
    is_influencer: bool = False
    online_payments_enabled: bool = False
    storefront_enabled: bool = False
    has_invoiced: bool = False


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    access_expires_at: dt.datetime
    refresh_token: str | None = None


class RefreshRequest(BaseModel):
    refresh_token: str | None = None


class MessageOut(BaseModel):
    detail: str


class PhoneVerificationRequest(BaseModel):
    """Request to add/verify phone number."""

    phone: str = Field(..., min_length=10, description="Phone number in E.164 format")
    # Step-up code, REQUIRED only when changing an EXISTING phone (the login
    # identity). First-time linking doesn't need it.
    otp: str | None = Field(None, max_length=12)


class PhoneVerificationVerify(BaseModel):
    """Verify phone number with OTP."""

    phone: str = Field(..., min_length=10)
    otp: str = Field(..., min_length=6, max_length=6)


class PhoneVerificationResponse(BaseModel):
    """Response after successful phone verification."""

    detail: str
    phone: str
