"""Invoice-related schemas."""
from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any, Generic, Literal, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_validator

from .utils import format_amount

T = TypeVar("T")


class InvoiceLineIn(BaseModel):
    description: str
    quantity: int = Field(default=1, ge=1, description="Quantity must be at least 1")
    unit_price: Decimal = Field(..., gt=0, description="Unit price must be greater than 0")
    product_id: int | None = None  # Link to inventory product for automatic stock tracking


class InvoiceCreate(BaseModel):
    # Common fields for both revenue and expense invoices
    amount: Decimal = Field(..., gt=0, description="Amount must be greater than 0")
    currency: Literal["NGN", "USD"] = "NGN"
    due_date: dt.datetime | None = None
    lines: list[InvoiceLineIn] | None = None
    discount_amount: Decimal | None = Field(default=None, ge=0)
    
    # Invoice type
    invoice_type: Literal["revenue", "expense"] = "revenue"
    
    # Revenue invoice fields (when invoice_type="revenue")
    customer_name: str | None = None
    customer_phone: str | None = None
    customer_email: str | None = None
    
    # Expense invoice fields (when invoice_type="expense")
    vendor_name: str | None = None
    category: str | None = None  # rent, utilities, supplies, etc.
    merchant: str | None = None
    description: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _validate_amounts(self) -> "InvoiceCreate":
        # A discount can never exceed the amount (that would make the payable
        # total negative). amount>0 and discount>=0 are enforced by Field above.
        if self.discount_amount is not None and self.discount_amount > self.amount:
            raise ValueError("Discount cannot exceed the invoice amount")
        return self


class QuickSaleCreate(BaseModel):
    """A walk-in / in-person sale: no customer required, paid immediately.

    Captures the in-person-equivalent of a POS transaction — amount, what was
    sold, and how it was collected (cash/transfer/card) — without the
    customer-billing fields a normal invoice needs. The sale is recorded and
    marked paid in a single call.
    """

    amount: Decimal = Field(..., gt=0, description="Amount must be greater than 0")
    currency: Literal["NGN", "USD"] = "NGN"
    description: str | None = Field(default=None, max_length=200)
    payment_method: Literal["cash", "transfer", "card", "other"] = "cash"
    # Optional — most walk-in sales have no named customer. When provided, the
    # sale is still recorded and marked paid immediately; this only helps the
    # business recognise the entry later (e.g. a regular customer paying cash).
    customer_name: str | None = None


class CustomerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    phone: str | None = None
    email: str | None = None


class InvoiceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: str
    amount: Decimal
    currency: str = "NGN"
    status: str
    pdf_url: str | None
    receipt_pdf_url: str | None = None
    paid_at: dt.datetime | None = None
    created_at: dt.datetime | None = None
    due_date: dt.datetime | None = None
    
    # Unified invoice/expense fields
    invoice_type: str = "revenue"
    category: str | None = None
    vendor_name: str | None = None
    merchant: str | None = None
    verified: bool | None = None
    notes: str | None = None
    # Origin channel (storefront, whatsapp, dashboard…) so the UI can adapt —
    # e.g. a storefront order hides the due date and payment-link fields.
    channel: str | None = None
    # How the sale was collected (cash/transfer/card/other) — set for
    # quick-sale (walk-in) invoices.
    payment_method: str | None = None
    
    # Creator tracking for team scenarios
    created_by_user_id: int | None = None
    created_by_name: str | None = None
    
    # Customer info for display/search
    customer_name: str | None = None
    
    # Status updater tracking (who marked as paid/cancelled)
    status_updated_by_user_id: int | None = None
    status_updated_by_name: str | None = None
    status_updated_at: dt.datetime | None = None
    
    @model_validator(mode="before")
    @classmethod
    def populate_user_names(cls, data: Any) -> Any:
        """Extract user names/customer info and refresh short-lived S3 URLs."""
        from app.storage.s3_client import s3_client

        if not hasattr(data, "created_by"):
            # Already a mapping (e.g. a Redis-cached dict) — the stored presigned
            # pdf/receipt URLs may be expired, so re-sign them in place.
            if isinstance(data, dict):
                for _f in ("pdf_url", "receipt_pdf_url"):
                    if data.get(_f):
                        data[_f] = s3_client.refresh_presigned_url(data[_f])
            return data
        
        result = {k: getattr(data, k, None) for k in cls.model_fields.keys() 
                  if k not in {"created_by_name", "status_updated_by_name", "customer_name"}}
        
        # Populate created_by_name
        if hasattr(data, "created_by") and data.created_by is not None:
            result["created_by_name"] = data.created_by.name
        
        # Populate status_updated_by_name
        if hasattr(data, "status_updated_by") and data.status_updated_by is not None:
            result["status_updated_by_name"] = data.status_updated_by.name

        # Populate customer_name from relationship
        if hasattr(data, "customer") and data.customer is not None:
            result["customer_name"] = data.customer.name

        # Stored presigned S3 URLs expire (~1h). Re-sign from the object key so
        # the dashboard's invoice PDF / receipt links don't 'AccessDenied /
        # Request has expired' for invoices older than the presign TTL.
        for _f in ("pdf_url", "receipt_pdf_url"):
            if result.get(_f):
                result[_f] = s3_client.refresh_presigned_url(result[_f])
        
        return result


class InvoiceLineOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    quantity: int
    unit_price: Decimal


class InvoiceOutDetailed(InvoiceOut):
    model_config = ConfigDict(from_attributes=True)

    discount_amount: Decimal | None = None
    customer: CustomerOut | None = None
    lines: list[InvoiceLineOut] = Field(default_factory=list)


class InvoiceStatusUpdate(BaseModel):
    # Added "refunded" to support post-payment refund handling
    status: Literal["pending", "awaiting_confirmation", "paid", "cancelled", "refunded"]


class InvoiceLinePublicOut(BaseModel):
    """Minimal line-item data for the public payment page."""
    model_config = ConfigDict(from_attributes=True)
    description: str
    quantity: int
    unit_price: Decimal

    @field_serializer("unit_price")
    def _serialize_unit_price(self, value: Decimal) -> str:
        return format_amount(value) or "0"


class InvoicePublicOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    invoice_id: str
    amount: Decimal
    currency: str = "NGN"
    status: str
    due_date: dt.datetime | None = None
    created_at: dt.datetime | None = None
    customer_name: str | None = None
    business_name: str | None = None
    business_logo_url: str | None = None
    bank_name: str | None = None
    account_number: str | None = None
    account_name: str | None = None
    paid_at: dt.datetime | None = None
    # True when the issuer has an active Paystack subaccount (can be paid online).
    online_payments_enabled: bool = False
    # True for storefront orders that must be paid online (bank transfer hidden).
    online_only: bool = False
    # Present only once the invoice is paid, so customers can download it.
    pdf_url: str | None = None
    receipt_pdf_url: str | None = None
    lines: list[InvoiceLinePublicOut] = []

    # NOTE: account_number is intentionally NOT masked here — this is the payee's
    # receiving account shown to the customer as bank-transfer instructions; they
    # need the FULL number to pay. (Masking it broke transfers: customers copied
    # "810****548".)

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format_amount(value) or "0"


class InvoiceVerificationItem(BaseModel):
    """A single line on a verified invoice (what was bought)."""
    description: str
    quantity: int = 1


class InvoiceVerificationOut(BaseModel):
    """Public invoice verification response (for QR code scanning)."""
    invoice_id: str
    status: str
    amount: Decimal
    customer_name: str  # Masked for privacy
    business_name: str
    verification_code: str = ""  # Unique, non-guessable authenticity stamp
    items: list[InvoiceVerificationItem] = []
    # Fulfilment (storefront orders only): whether the seller has rendered the
    # service / delivered the goods, and where it is in buyer protection.
    fulfilment_status: str | None = None  # e.g. delivered | rendered | confirmed | released
    fulfilment_label: str | None = None  # human-readable
    created_at: dt.datetime
    verified_at: dt.datetime
    authentic: bool = True

    @field_serializer("amount")
    def _serialize_amount(self, value: Decimal) -> str:
        return format_amount(value) or "0"


class InvoiceQuotaOut(BaseModel):
    """Invoice quota information for the authenticated user.
    
    NEW BILLING MODEL: Uses invoice_balance (purchased invoices) instead of monthly limits.
    """
    invoice_balance: int = Field(description="Remaining invoices available to create")
    total_invoices: int = Field(default=0, description="Total revenue invoices the user has created (drives onboarding activation)")
    current_plan: str = Field(description="Current subscription plan code")
    can_create: bool = Field(description="Whether user has invoice balance to create invoices")
    pack_price: int = Field(default=2500, description="Price for an invoice pack in Naira")
    pack_size: int = Field(default=100, description="Number of invoices per pack")
    purchase_url: str | None = Field(default=None, description="URL to purchase more invoice packs")


class ReceiptUploadOut(BaseModel):
    """Response after successfully uploading an expense receipt."""
    receipt_url: str = Field(description="S3 URL of the uploaded receipt")
    filename: str = Field(description="Original filename of the uploaded receipt")


class InvoicePackPurchaseInitOut(BaseModel):
    """Response after initializing invoice pack purchase payment."""
    authorization_url: str = Field(description="Paystack checkout URL for payment")
    reference: str = Field(description="Payment reference for tracking")
    amount: int = Field(description="Total amount in Naira")
    invoices_to_add: int = Field(default=0, description="Legacy: invoices added (0 under wallet model)")
    wallet_credit_naira: int = Field(default=0, description="Amount credited to the prepaid wallet")

class PaginatedResponse(BaseModel, Generic[T]):
    """Generic paginated API response."""
    items: list[T]
    total: int = Field(description="Total number of matching records")
    skip: int = Field(description="Number of records skipped")
    limit: int = Field(description="Maximum records returned per page")
    has_more: bool = Field(description="Whether more records exist beyond this page")
    # Optional per-status counts (used by the invoice list filter chips). None on
    # endpoints that don't compute them.
    status_counts: dict[str, int] | None = Field(
        default=None, description="Counts per status across all matching records"
    )