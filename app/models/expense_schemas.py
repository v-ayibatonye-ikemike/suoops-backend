"""
Pydantic schemas for expense tracking API.

Used for request/response validation in expense endpoints.
"""

from datetime import date, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Expense Categories
ExpenseCategoryType = Literal[
    "rent",
    "utilities",
    "data_internet",
    "transport",
    "supplies",
    "equipment",
    "marketing",
    "professional_fees",
    "staff_wages",
    "maintenance",
    "other",
]

# Input Methods
InputMethodType = Literal["voice", "text", "photo", "manual"]

# Channels
ChannelType = Literal["whatsapp", "email", "dashboard"]


class ExpenseBase(BaseModel):
    """Base expense fields"""

    amount: Decimal = Field(..., gt=0, description="Expense amount in Naira")
    expense_date: date = Field(..., description="Date of expense")
    category: ExpenseCategoryType = Field(..., description="Expense category")
    description: str | None = Field(None, max_length=500, description="Expense description")
    merchant: str | None = Field(None, max_length=200, description="Merchant/vendor name")


class ExpenseCreate(ExpenseBase):
    """Schema for creating an expense manually"""

    notes: str | None = Field(None, description="User notes")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        if v <= 0:
            raise ValueError("Amount must be greater than 0")
        # Round to 2 decimal places
        return round(v, 2)


class ExpenseUpdate(BaseModel):
    """Schema for updating an expense"""

    amount: Decimal | None = Field(None, gt=0)
    expense_date: date | None = None
    category: ExpenseCategoryType | None = None
    description: str | None = Field(None, max_length=500)
    merchant: str | None = Field(None, max_length=200)
    notes: str | None = None


class ExpenseOut(ExpenseBase):
    """Schema for expense response"""

    id: int
    user_id: int
    input_method: str | None
    channel: str | None
    receipt_url: str | None
    verified: bool
    record_status: Literal["self_reported", "documented", "flagged"]
    possible_duplicate: bool
    possible_duplicate_of_id: int | None = None
    notes: str | None
    created_at: datetime
    updated_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class ExpenseSummary(BaseModel):
    """Schema for expense summary by category"""

    total_expenses: float = Field(..., description="Total expenses for period")
    by_category: dict[str, float] = Field(..., description="Expenses grouped by category")
    period_type: str = Field(..., description="Period type: day, week, month, year")
    start_date: str = Field(..., description="Period start date (ISO 8601)")
    end_date: str = Field(..., description="Period end date (ISO 8601)")
    count: int = Field(..., description="Total number of expense records")


class ExpenseStats(BaseModel):
    """Schema for expense statistics"""

    total_expenses: float
    total_revenue: float
    actual_profit: float
    expense_to_revenue_ratio: float  # Percentage
    top_categories: list[dict[str, float]]  # Top 5 expense categories
    period_type: str
    start_date: str
    end_date: str
