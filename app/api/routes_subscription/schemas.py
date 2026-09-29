"""Response schemas for subscription and payment endpoints.

Prevents accidental leakage of internal fields like Paystack
transaction IDs, subscription codes, and raw webhook metadata.
"""

from __future__ import annotations

from pydantic import BaseModel

# ── Subscription status ────────────────────────────────────────────────


class SubscriptionStatusOut(BaseModel):
    plan: str
    is_recurring: bool
    subscription_started_at: str | None = None
    expires_at: str | None = None
    invoice_balance: int = 0


# ── Payment initialization ────────────────────────────────────────────


class PaymentInitOut(BaseModel):
    authorization_url: str
    access_code: str
    reference: str
    amount: int
    plan: str
    billing_type: str
    message: str


# ── Payment verification ──────────────────────────────────────────────


class PaymentVerifyOut(BaseModel):
    status: str
    message: str
    old_plan: str | None = None
    new_plan: str | None = None
    amount_paid: float | None = None


# ── Payment history list item ─────────────────────────────────────────


class PaymentHistoryItem(BaseModel):
    id: int
    reference: str
    amount: float
    currency: str
    status: str
    plan_before: str
    plan_after: str
    payment_method: str | None = None
    card_last4: str | None = None
    card_brand: str | None = None
    bank_name: str | None = None
    created_at: str
    paid_at: str | None = None
    billing_start_date: str | None = None
    billing_end_date: str | None = None
    failure_reason: str | None = None


class PaymentSummary(BaseModel):
    total_paid: float
    successful_count: int
    pending_count: int
    failed_count: int


class PaymentHistoryOut(BaseModel):
    payments: list[PaymentHistoryItem]
    total: int
    limit: int
    offset: int
    summary: PaymentSummary


# ── Single payment detail (excludes Paystack internals) ───────────────


class PaymentDetailOut(BaseModel):
    """Excludes paystack_transaction_id, payment_metadata, ip_address."""

    id: int
    reference: str
    amount: float
    currency: str
    status: str
    provider: str
    plan_before: str
    plan_after: str
    payment_method: str | None = None
    card_last4: str | None = None
    card_brand: str | None = None
    bank_name: str | None = None
    customer_email: str | None = None
    created_at: str
    updated_at: str
    paid_at: str | None = None
    billing_start_date: str | None = None
    billing_end_date: str | None = None
    failure_reason: str | None = None


# ── Cancel plan ───────────────────────────────────────────────────────


class CancelSubscriptionOut(BaseModel):
    status: str
    message: str
    plan: str | None = None
    expires_at: str | None = None
