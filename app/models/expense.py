"""
Expense tracking models for business expense management.

Supports multi-channel input (WhatsApp, email, dashboard) and automated tax compliance.
"""

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)

from app.db.base_class import Base


class ExpenseCategory(str, Enum):
    """
    Nigerian business expense categories (tax-deductible).

    Based on 2026 Nigerian Tax Law allowable business expenses.
    """

    RENT = "rent"  # Office/shop/store rent
    UTILITIES = "utilities"  # Electricity, water, gas
    DATA_INTERNET = "data_internet"  # Internet, data bundles, airtime
    TRANSPORT = "transport"  # Fuel, transport fares, vehicle costs
    SUPPLIES = "supplies"  # Office supplies, inventory, materials
    EQUIPMENT = "equipment"  # Tools, machinery, computers
    MARKETING = "marketing"  # Advertising, promotions, branding
    PROFESSIONAL_FEES = "professional_fees"  # Accountant, lawyer, consultant fees
    STAFF_WAGES = "staff_wages"  # Employee salaries, contract labor
    MAINTENANCE = "maintenance"  # Repairs, upkeep
    OTHER = "other"  # Miscellaneous expenses


class ExpenseInputMethod(str, Enum):
    """How the expense was recorded"""

    VOICE = "voice"  # WhatsApp/email voice note
    TEXT = "text"  # WhatsApp/email text message
    PHOTO = "photo"  # Receipt photo via OCR
    MANUAL = "manual"  # Dashboard manual entry


class ExpenseChannel(str, Enum):
    """Channel through which expense was submitted"""

    WHATSAPP = "whatsapp"
    EMAIL = "email"
    DASHBOARD = "dashboard"


class Expense(Base):
    """
    Business expense record.

    Tracks all business expenses for accurate profit calculation and tax compliance.
    Supports multi-channel input: WhatsApp (voice/text/photo), email, dashboard.

    Profit = Revenue - Expenses (per 2026 Nigerian Tax Law)
    """

    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)

    # Amount & Date
    amount = Column(Numeric(15, 2), nullable=False)
    date = Column(Date, nullable=False, index=True)

    # Categorization
    category = Column(String(50), nullable=False, index=True)
    description = Column(String(500))
    merchant = Column(String(200))  # Vendor/supplier name

    # Source Tracking
    input_method = Column(String(20))  # voice, text, photo, manual
    channel = Column(String(20))  # whatsapp, email, dashboard

    # Receipt/Evidence
    receipt_url = Column(String(500))  # S3 URL for receipt image/PDF
    receipt_text = Column(Text)  # OCR extracted text from receipt

    # Verification & Notes
    verified = Column(Boolean, default=False)
    notes = Column(Text)  # User notes or system-generated notes

    # Metadata
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime, onupdate=lambda: datetime.now(timezone.utc))

    # Note: This table is DEPRECATED - expenses are now tracked as invoices with invoice_type='expense'
    # Keeping this model for backward compatibility only
    # Relationship removed since User.expenses no longer exists

    def __repr__(self):
        return (
            f"<Expense(id={self.id}, user_id={self.user_id}, amount={self.amount}, "
            f"category={self.category}, date={self.date})>"
        )

    @property
    def amount_float(self) -> float:
        """Convenience property to get amount as float"""
        return float(self.amount) if self.amount else 0.0

    @property
    def category_display(self) -> str:
        """Human-readable category name"""
        return self.category.replace("_", " ").title()
