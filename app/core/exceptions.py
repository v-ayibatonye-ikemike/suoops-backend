"""Custom exception hierarchy for SuoOps.

Follows SOLID principles:
- Single Responsibility: Each exception class handles one error category
- Open/Closed: Extensible via inheritance, closed for modification
- Liskov Substitution: All exceptions can be caught via SuoOpsException base
- Interface Segregation: Minimal required attributes (code, message, details)
- Dependency Inversion: API layer depends on these abstractions, not implementation

Error codes follow pattern: [CATEGORY][NUMBER]
- INV: Invoice errors (001-099)
- USR: User/Auth errors (100-199)
- PAY: Payment errors (200-299)
- TAX: Tax/Fiscal errors (300-399)
- SYS: System errors (400-499)
"""

from __future__ import annotations

from typing import Any


class SuoOpsException(Exception):
    """Base exception for all SuoOps application errors.
    
    All custom exceptions inherit from this to enable centralized error handling.
    Includes user-friendly messages with Nigerian English idioms where appropriate.
    """

    def __init__(
        self,
        message: str,
        code: str,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ):
        """Initialize exception with user-friendly message and metadata.
        
        Args:
            message: User-friendly error message
            code: Unique error code (e.g., "INV001")
            status_code: HTTP status code (default: 400 Bad Request)
            details: Optional additional context
        """
        super().__init__(message)
        self.message = message
        self.code = code
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Convert exception to API response format."""
        return {
            "error": {
                "message": self.message,
                "code": self.code,
                "details": self.details,
            }
        }


# ============================================================================
# INVOICE ERRORS (INV001-099)
# ============================================================================

class InvoiceError(SuoOpsException):
    """Base class for invoice-related errors."""
    pass


class InvoiceNotFoundError(InvoiceError):
    """Invoice does not exist or user doesn't have access."""
    
    def __init__(self, invoice_id: str | None = None):
        message = "Invoice not found" if not invoice_id else f"Invoice {invoice_id} not found"
        super().__init__(
            message=message,
            code="INV001",
            status_code=404,
            details={"invoice_id": invoice_id} if invoice_id else {},
        )


class InvoiceLimitExceededError(InvoiceError):
    """DEPRECATED: User has exceeded their monthly invoice quota.
    
    This error is kept for backward compatibility.
    New code should use InvoiceBalanceExhaustedError instead.
    """
    
    def __init__(self, plan: str, limit: int, used: int):
        message = (
            f"You don reach your monthly invoice limit! "
            f"Your {plan} plan allows {limit} invoices per month, "
            f"but you don already create {used}. "
            f"Upgrade your plan to create more invoices."
        )
        super().__init__(
            message=message,
            code="INV002",
            status_code=403,
            details={"plan": plan, "limit": limit, "used": used},
        )


class InvoiceBalanceExhaustedError(InvoiceError):
    """User's prepaid invoice wallet can't cover this invoice's fee."""

    def __init__(self, balance: int = 0, pack_price: int = 1250, pack_size: int | None = None):
        message = (
            "Your invoice wallet is too low to create this invoice. "
            f"Top up (from ₦{pack_price:,}) to continue, or share your storefront "
            "link so the customer orders and pays online instead."
        )
        super().__init__(
            message=message,
            code="INV005",
            status_code=403,
            details={
                "wallet_balance_kobo": balance,
                "topup_from": pack_price,
                "purchase_url": "/invoices/purchase-pack",
            },
        )


class InvalidInvoiceStatusError(InvoiceError):
    """Invalid status transition or unsupported status value."""
    
    def __init__(self, current_status: str | None = None, new_status: str | None = None):
        if current_status and new_status:
            message = f"Cannot change invoice status from '{current_status}' to '{new_status}'"
        elif new_status:
            message = f"Invalid invoice status: '{new_status}'"
        else:
            message = "Invalid invoice status"
        
        super().__init__(
            message=message,
            code="INV003",
            status_code=400,
            details={"current_status": current_status, "new_status": new_status},
        )


class MissingBankDetailsError(InvoiceError):
    """User attempted to create invoice without setting up bank details."""
    
    def __init__(self):
        message = (
            "Abeg, add your bank details first! "
            "You need to set your bank account information "
            "before you can create invoices. Go to Settings to add it."
        )
        super().__init__(
            message=message,
            code="INV004",
            status_code=400,
            details={"required_fields": ["bank_name", "account_number", "account_name"]},
        )


# ============================================================================
# USER/AUTH ERRORS (USR100-199)
# ============================================================================

class UserError(SuoOpsException):
    """Base class for user/authentication errors."""
    pass


class UserNotFoundError(UserError):
    """User does not exist."""
    
    def __init__(self, identifier: str | None = None):
        message = "User not found" if not identifier else f"User '{identifier}' not found"
        super().__init__(
            message=message,
            code="USR100",
            status_code=404,
            details={"identifier": identifier} if identifier else {},
        )


class InvalidOTPError(UserError):
    """OTP verification failed."""
    
    def __init__(self):
        message = "The OTP you entered is incorrect or has expired. Please request a new one."
        super().__init__(
            message=message,
            code="USR101",
            status_code=401,
        )


class OTPExpiredError(UserError):
    """OTP has expired."""
    
    def __init__(self):
        message = "Your verification code don expire. Please request a new one."
        super().__init__(
            message=message,
            code="USR102",
            status_code=401,
        )


class TooManyOTPAttemptsError(UserError):
    """User exceeded maximum OTP verification attempts."""
    
    def __init__(self, remaining_time: int | None = None):
        base_message = "Too many failed attempts. "
        if remaining_time:
            message = f"{base_message}Try again after {remaining_time} minutes."
        else:
            message = f"{base_message}Please request a new code."
        
        super().__init__(
            message=message,
            code="USR103",
            status_code=429,
            details={"remaining_time_minutes": remaining_time} if remaining_time else {},
        )


class EmailAlreadyExistsError(UserError):
    """Email address is already registered."""
    
    def __init__(self, email: str):
        message = f"This email address ({email}) is already registered. Please login instead."
        super().__init__(
            message=message,
            code="USR104",
            status_code=409,
            details={"email": email},
        )


class PhoneAlreadyExistsError(UserError):
    """Phone number is already registered."""
    
    def __init__(self, phone: str):
        message = f"This phone number ({phone}) is already registered. Please login instead."
        super().__init__(
            message=message,
            code="USR105",
            status_code=409,
            details={"phone": phone},
        )


class UnauthorizedError(UserError):
    """User is not authorized to perform this action."""
    
    def __init__(self, action: str | None = None):
        message = "You are not authorized to perform this action" if not action else f"Not authorized: {action}"
        super().__init__(
            message=message,
            code="USR106",
            status_code=403,
        )


# ============================================================================
# PAYMENT ERRORS (PAY200-299)
# ============================================================================

class PaymentError(SuoOpsException):
    """Base class for payment-related errors."""
    pass


class PaymentVerificationError(PaymentError):
    """Failed to verify payment status."""
    
    def __init__(self, reference: str | None = None):
        message = "Unable to verify payment at this time. Please try again."
        super().__init__(
            message=message,
            code="PAY200",
            status_code=500,
            details={"reference": reference} if reference else {},
        )


class PaymentAlreadyProcessedError(PaymentError):
    """Payment has already been processed."""
    
    def __init__(self, reference: str):
        message = "This payment has already been processed."
        super().__init__(
            message=message,
            code="PAY201",
            status_code=409,
            details={"reference": reference},
        )


# ============================================================================
# TAX/FISCAL ERRORS (TAX300-399)
# ============================================================================

class TaxError(SuoOpsException):
    """Base class for tax/fiscal errors."""
    pass


class InvalidTINError(TaxError):
    """Tax Identification Number is invalid."""
    
    def __init__(self, tin: str, reason: str | None = None):
        base_message = f"Invalid TIN: {tin}"
        message = f"{base_message}. {reason}" if reason else base_message
        super().__init__(
            message=message,
            code="TAX300",
            status_code=400,
            details={"tin": tin, "reason": reason},
        )


class FiscalizationError(TaxError):
    """Error during invoice fiscalization process."""
    
    def __init__(self, message: str, provider: str | None = None):
        super().__init__(
            message=f"Fiscalization failed: {message}",
            code="TAX301",
            status_code=500,
            details={"provider": provider} if provider else {},
        )


class LookupBalanceExhaustedError(TaxError):
    """User's prepaid wallet can't cover a Mono identity/registry lookup fee."""

    def __init__(self, balance_kobo: int, required_kobo: int):
        message = (
            f"Your wallet balance (₦{balance_kobo / 100:,.2f}) is too low to cover "
            f"this verification (₦{required_kobo / 100:,.2f}). Top up your wallet "
            "and try again."
        )
        super().__init__(
            message=message,
            code="TAX302",
            status_code=403,
            details={"wallet_balance_kobo": balance_kobo, "required_kobo": required_kobo},
        )


class InvalidCACError(TaxError):
    """CAC/RC registration number is invalid or not found."""

    def __init__(self, rc_number: str, reason: str | None = None):
        base_message = f"Invalid CAC/RC number: {rc_number}"
        message = f"{base_message}. {reason}" if reason else base_message
        super().__init__(
            message=message,
            code="TAX303",
            status_code=400,
            details={"rc_number": rc_number, "reason": reason},
        )


# ============================================================================
# SYSTEM ERRORS (SYS400-499)
# ============================================================================

class SystemError(SuoOpsException):
    """Base class for system/infrastructure errors."""
    pass


class ServiceUnavailableError(SystemError):
    """External service or dependency is unavailable."""
    
    def __init__(self, service_name: str, reason: str | None = None):
        message = f"{service_name} is currently unavailable"
        if reason:
            message = f"{message}: {reason}"
        
        super().__init__(
            message=message,
            code="SYS400",
            status_code=503,
            details={"service": service_name, "reason": reason},
        )


class ConfigurationError(SystemError):
    """Application configuration is invalid or missing."""
    
    def __init__(self, parameter: str):
        message = f"Configuration error: {parameter} is not configured properly"
        super().__init__(
            message=message,
            code="SYS401",
            status_code=500,
            details={"parameter": parameter},
        )


class RateLimitExceededError(SystemError):
    """User has exceeded rate limit."""
    
    def __init__(self, limit: str, retry_after: int | None = None):
        message = f"Rate limit exceeded: {limit}. "
        if retry_after:
            message += f"Try again in {retry_after} seconds."
        else:
            message += "Please slow down small!"
        
        super().__init__(
            message=message,
            code="SYS402",
            status_code=429,
            details={"limit": limit, "retry_after_seconds": retry_after},
        )
