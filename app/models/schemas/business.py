"""Bank, OAuth, and OCR-related schemas."""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# ----------------- Bank Details -----------------


class BankDetailsUpdate(BaseModel):
    """Schema for updating business bank account details."""

    business_name: str | None = Field(None, min_length=1, max_length=255, description="Business display name")
    bank_name: str | None = Field(
        None, min_length=1, max_length=100, description="Bank name (e.g., GTBank, Access Bank)"
    )
    account_number: str | None = Field(
        None,
        min_length=10,
        max_length=10,
        pattern=r"^\d{10}$",
        description="10-digit account number",
    )
    account_name: str | None = Field(None, min_length=1, max_length=255, description="Account holder name")
    otp: str | None = Field(
        None,
        min_length=4,
        max_length=8,
        description="One-time code (required only when changing an EXISTING bank account)",
    )


class BankDetailsOut(BaseModel):
    """Schema for returning bank account details."""

    model_config = ConfigDict(from_attributes=True)

    business_name: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    is_configured: bool = Field(description="Whether bank details are fully configured")
    online_payments_enabled: bool = Field(
        default=False,
        description="Whether the business has an active Paystack subaccount (online payments on)",
    )


# ----------------- OCR -----------------


class OCRItemOut(BaseModel):
    """Single line item extracted from receipt image."""

    description: str
    quantity: int
    unit_price: str


class OCRParseOut(BaseModel):
    """
    Response from OCR parsing of receipt image.

    User should review this data before creating invoice.
    """

    success: bool
    customer_name: str
    business_name: str
    amount: str
    currency: str
    items: list[OCRItemOut]
    date: str | None = None
    confidence: Literal["high", "medium", "low"]
    raw_text: str

    model_config = ConfigDict(
        json_schema_extra={
            "example": {
                "success": True,
                "customer_name": "Jane Doe",
                "business_name": "Beauty Palace",
                "amount": "50000",
                "currency": "NGN",
                "items": [{"description": "Hair braiding", "quantity": 1, "unit_price": "50000"}],
                "date": "2025-10-30",
                "confidence": "high",
                "raw_text": "BEAUTY PALACE\nCustomer: Jane Doe\nHair braiding: ₦50,000\nTotal: ₦50,000",
            }
        }
    )


# ----------------- OAuth / SSO Schemas -----------------


class OAuthProviderInfo(BaseModel):
    """Information about an OAuth provider."""

    name: str
    display_name: str
    enabled: bool
    supports_refresh: bool
    icon_url: str | None = None


class OAuthProvidersOut(BaseModel):
    """List of available OAuth providers."""

    providers: list[OAuthProviderInfo]


class OAuthCallbackOut(BaseModel):
    """Response from OAuth callback with JWT tokens."""

    access_token: str
    refresh_token: str
    access_expires_at: dt.datetime
    token_type: str = "bearer"
    redirect_uri: str
