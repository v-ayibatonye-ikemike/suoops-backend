"""Backward compatibility redirect for schemas.

DEPRECATED: This module has been refactored into app/models/schemas/
for better SRP compliance and code organization.

All imports should continue to work via this redirect module.
New code should import from app.models.schemas directly.
"""

from app.models.schemas import (
    AgingReport,
    AnalyticsDashboard,
    BankDetailsOut,
    # Business
    BankDetailsUpdate,
    CustomerMetrics,
    CustomerOut,
    InvoiceCreate,
    # Invoice
    InvoiceLineIn,
    InvoiceLineOut,
    InvoiceMetrics,
    InvoiceOut,
    InvoiceOutDetailed,
    InvoicePublicOut,
    InvoiceQuotaOut,
    InvoiceStatusUpdate,
    InvoiceVerificationOut,
    LoginVerify,
    MessageOut,
    MonthlyTrend,
    OAuthCallbackOut,
    OAuthProviderInfo,
    OAuthProvidersOut,
    OCRItemOut,
    OCRParseOut,
    OTPEmailRequest,
    # Auth
    OTPPhoneRequest,
    OTPResend,
    PhoneVerificationRequest,
    PhoneVerificationResponse,
    PhoneVerificationVerify,
    ReceiptUploadOut,
    RefreshRequest,
    # Analytics
    RevenueMetrics,
    SignupStart,
    SignupVerify,
    TokenOut,
    UserOut,
)

# Also export the format function for backward compat
from app.models.schemas.utils import format_amount as _format_amount

__all__ = [
    "InvoiceLineIn",
    "InvoiceCreate",
    "CustomerOut",
    "InvoiceOut",
    "InvoiceLineOut",
    "InvoiceOutDetailed",
    "InvoiceStatusUpdate",
    "InvoicePublicOut",
    "InvoiceVerificationOut",
    "InvoiceQuotaOut",
    "ReceiptUploadOut",
    "OTPPhoneRequest",
    "OTPEmailRequest",
    "SignupStart",
    "SignupVerify",
    "LoginVerify",
    "OTPResend",
    "UserOut",
    "TokenOut",
    "RefreshRequest",
    "MessageOut",
    "PhoneVerificationRequest",
    "PhoneVerificationVerify",
    "PhoneVerificationResponse",
    "BankDetailsUpdate",
    "BankDetailsOut",
    "OCRItemOut",
    "OCRParseOut",
    "OAuthProviderInfo",
    "OAuthProvidersOut",
    "OAuthCallbackOut",
    "RevenueMetrics",
    "InvoiceMetrics",
    "CustomerMetrics",
    "AgingReport",
    "MonthlyTrend",
    "AnalyticsDashboard",
    "_format_amount",
]
