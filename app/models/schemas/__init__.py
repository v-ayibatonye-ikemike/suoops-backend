"""Pydantic schemas for API requests and responses.

Refactored from monolithic schemas.py for SRP compliance.

Sub-modules:
- invoice: Invoice-related schemas
- auth: Authentication schemas
- business: Bank, OAuth, OCR schemas
- analytics: Analytics schemas
- utils: Common utility functions
"""
# Invoice schemas
# Analytics schemas
from .analytics import (
    AgingReport,
    AnalyticsDashboard,
    CustomerMetrics,
    InvoiceMetrics,
    MonthlyTrend,
    RevenueMetrics,
)

# Auth schemas
from .auth import (
    LoginVerify,
    MessageOut,
    OTPEmailRequest,
    OTPPhoneRequest,
    OTPResend,
    PhoneVerificationRequest,
    PhoneVerificationResponse,
    PhoneVerificationVerify,
    RefreshRequest,
    SignupStart,
    SignupVerify,
    TokenOut,
    UserOut,
)

# Business schemas
from .business import (
    BankDetailsOut,
    BankDetailsUpdate,
    OAuthCallbackOut,
    OAuthProviderInfo,
    OAuthProvidersOut,
    OCRItemOut,
    OCRParseOut,
)
from .invoice import (
    CustomerOut,
    InvoiceCreate,
    InvoiceLineIn,
    InvoiceLineOut,
    InvoiceOut,
    InvoiceOutDetailed,
    InvoicePackPurchaseInitOut,
    InvoicePublicOut,
    InvoiceQuotaOut,
    InvoiceStatusUpdate,
    InvoiceVerificationItem,
    InvoiceVerificationOut,
    PaginatedResponse,
    QuickSaleCreate,
    ReceiptUploadOut,
)

__all__ = [
    # Invoice
    "InvoiceLineIn",
    "InvoiceCreate",
    "QuickSaleCreate",
    "CustomerOut",
    "InvoiceOut",
    "InvoiceLineOut",
    "InvoiceOutDetailed",
    "InvoiceStatusUpdate",
    "InvoicePublicOut",
    "InvoiceVerificationItem",
    "InvoiceVerificationOut",
    "InvoiceQuotaOut",
    "InvoicePackPurchaseInitOut",
    "ReceiptUploadOut",
    "PaginatedResponse",
    # Auth
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
    # Business
    "BankDetailsUpdate",
    "BankDetailsOut",
    "OCRItemOut",
    "OCRParseOut",
    "OAuthProviderInfo",
    "OAuthProvidersOut",
    "OAuthCallbackOut",
    # Analytics
    "RevenueMetrics",
    "InvoiceMetrics",
    "CustomerMetrics",
    "AgingReport",
    "MonthlyTrend",
    "AnalyticsDashboard",
]
