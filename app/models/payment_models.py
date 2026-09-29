"""Payment transaction models for subscription billing."""

from __future__ import annotations

import datetime as dt
import enum
from typing import TYPE_CHECKING

from sqlalchemy import JSON, DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.sql import func

from app.db.base_class import Base

if TYPE_CHECKING:
    from app.models.models import User


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


class PaymentStatus(str, enum.Enum):
    """Payment transaction status."""

    PENDING = "pending"  # Payment initiated, awaiting confirmation
    SUCCESS = "success"  # Payment confirmed by Paystack
    FAILED = "failed"  # Payment failed or declined
    CANCELLED = "cancelled"  # User cancelled payment
    REFUNDED = "refunded"  # Payment refunded to user


class PaymentProvider(str, enum.Enum):
    """Payment gateway provider."""

    PAYSTACK = "paystack"
    STRIPE = "stripe"  # Future support
    MANUAL = "manual"  # Manual/bank transfer


class PaymentTransaction(Base):
    """
    Records all subscription payment transactions.

    Purpose: Track customer payments for SuoOps subscription upgrades/renewals.
    Each successful payment upgrades the user's plan and extends their billing cycle.

    Flow:
    1. User initiates payment → status=PENDING
    2. Paystack webhook confirms → status=SUCCESS, user.plan upgraded
    3. Payment fails → status=FAILED
    """

    __tablename__ = "payment_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)

    # User information
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"), nullable=False, index=True)
    user: Mapped[User] = relationship(back_populates="payment_transactions")

    # Payment details
    reference: Mapped[str] = mapped_column(String(100), unique=True, nullable=False, index=True)
    """Unique payment reference (e.g., SUB-123-PRO-1700000000000)"""

    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    """Amount in kobo (Naira x 100). Example: ₦8,000 = 800000 kobo"""

    currency: Mapped[str] = mapped_column(String(3), default="NGN", nullable=False)
    """Currency code (NGN for Naira)"""

    # Plan information
    plan_before: Mapped[str] = mapped_column(String(20), nullable=False)
    """User's plan before payment (e.g., 'free', 'starter')"""

    plan_after: Mapped[str] = mapped_column(String(20), nullable=False)
    """User's plan after successful payment (e.g., 'pro', 'business')"""

    # Payment status
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING, nullable=False, index=True
    )

    provider: Mapped[PaymentProvider] = mapped_column(
        Enum(PaymentProvider), default=PaymentProvider.PAYSTACK, nullable=False
    )

    # Paystack-specific fields
    paystack_transaction_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Paystack's internal transaction ID"""

    paystack_authorization_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Paystack checkout URL"""

    paystack_access_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Paystack access code for payment page"""

    # Payment metadata
    payment_method: Mapped[str | None] = mapped_column(String(50), nullable=True)
    """Payment method used (card, bank_transfer, ussd, etc.)"""

    card_last4: Mapped[str | None] = mapped_column(String(4), nullable=True)
    """Last 4 digits of card (if card payment)"""

    card_brand: Mapped[str | None] = mapped_column(String(20), nullable=True)
    """Card brand (visa, mastercard, verve, etc.)"""

    bank_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    """Bank name (if bank transfer)"""

    # Customer details at time of payment
    customer_email: Mapped[str] = mapped_column(String(255), nullable=False)
    """Email used for payment (may differ from current user email)"""

    customer_phone: Mapped[str | None] = mapped_column(String(32), nullable=True)
    """Phone number at time of payment (supports OAuth synthetic phones)"""

    # Billing period
    billing_start_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """Start of billing period (for subscription)"""

    billing_end_date: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    """End of billing period (usually 30 days from start)"""

    # Timestamps
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    """When payment was initiated"""

    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    """Last status update"""

    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    """When payment was confirmed (status=SUCCESS)"""

    # Failure information
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Reason for payment failure (if status=FAILED)"""

    # Additional metadata (JSON) - renamed from 'metadata' to avoid SQLAlchemy reserved word
    payment_metadata: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    """Additional payment metadata from Paystack webhook"""

    # Audit fields
    ip_address: Mapped[str | None] = mapped_column(String(45), nullable=True)
    """IP address where payment was initiated"""

    user_agent: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Browser user agent"""

    def __repr__(self) -> str:
        return (
            f"<PaymentTransaction(id={self.id}, reference={self.reference}, "
            f"status={self.status}, amount=₦{self.amount/100:.2f})>"
        )

    @property
    def amount_naira(self) -> float:
        """Convert kobo to Naira."""
        return self.amount / 100.0

    @property
    def is_successful(self) -> bool:
        """Check if payment was successful."""
        return self.status == PaymentStatus.SUCCESS

    @property
    def is_pending(self) -> bool:
        """Check if payment is still pending."""
        return self.status == PaymentStatus.PENDING

    @property
    def is_failed(self) -> bool:
        """Check if payment failed."""
        return self.status in (PaymentStatus.FAILED, PaymentStatus.CANCELLED)
