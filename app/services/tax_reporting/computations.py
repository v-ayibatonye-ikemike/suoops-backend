"""Tax computation functions and constants.

Pure computation logic for Nigerian Personal Income Tax (PIT)
and profit calculations. No database access in helper functions.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# Nigerian Personal Income Tax (PIT) Bands for 2026
# Progressive taxation on profit (revenue - expenses)
# Tax exemption for low earners (≤₦800,000)
PIT_BANDS = [
    (800_000, 0.00),  # First ₦800,000: 0% (EXEMPT)
    (3_000_000, 0.15),  # ₦800,001-₦3,000,000: 15%
    (12_000_000, 0.18),  # ₦3,000,001-₦12,000,000: 18%
    (25_000_000, 0.21),  # ₦12,000,001-₦25,000,000: 21%
    (50_000_000, 0.23),  # ₦25,000,001-₦50,000,000: 23%
    (float("inf"), 0.25),  # Above ₦50,000,000: 25%
]


# Nigerian Company Income Tax (CIT) Rates
# Based on Nigeria Tax Act 2025 (NTA 2025) - effective January 1, 2026
# Thresholds based on annual gross turnover
CIT_THRESHOLDS = {
    "small": {
        "max_turnover": 100_000_000,  # ≤₦100M (increased from ₦25M under NTA 2025)
        "rate": 0.00,  # 0% (Exempt from CIT)
    },
    "medium": {
        "max_turnover": 250_000_000,  # >₦100M and ≤₦250M
        "rate": 0.20,  # 20%
    },
    "large": {
        "max_turnover": float("inf"),  # >₦250M
        "rate": 0.30,  # 30%
    },
}

# Development Levy rate (4% of assessable profits for non-small companies)
DEVELOPMENT_LEVY_RATE = 0.04

# Minimum tax rate (0.5% of gross turnover less franked investment income)
MINIMUM_TAX_RATE = 0.005

# Maximum capital allowance claimable (66.67% of assessable profit)
MAX_CAPITAL_ALLOWANCE_RATIO = 0.6667

# Rent Relief: 20% of annual rent paid, capped at ₦500,000
RENT_RELIEF_RATE = 0.20
RENT_RELIEF_CAP = 500_000


def compute_company_income_tax(
    profit: Decimal,
    annual_turnover: Optional[Decimal] = None,
    capital_allowances: Optional[Decimal] = None,
) -> dict:
    """
    Calculate Nigerian Company Income Tax (CIT) based on profit and turnover.

    CIT Calculation Steps:
    1. Start with accounting profit (profit parameter)
    2. Compute assessable profit (after adjustments - simplified here)
    3. Deduct capital allowances (capped at 66.67% of assessable profit)
    4. Apply CIT rate based on company size (turnover threshold)

    CIT Rates (Nigeria Tax Act 2025 - NTA 2025):
    - Small Company (turnover ≤₦100M): 0% (EXEMPT)
    - Medium Company (turnover >₦100M and ≤₦250M): 20%
    - Large Company (turnover >₦250M): 30%

    Additional Levies:
    - Development Levy: 4% of assessable profits (non-small companies)
    - Minimum Tax: 0.5% of gross turnover (if CIT < minimum tax)

    Args:
        profit: Taxable profit (Revenue - Expenses)
        annual_turnover: Annual gross turnover for size classification
        capital_allowances: Capital allowances (tax depreciation) to deduct

    Returns:
        dict with cit_amount, development_levy, minimum_tax, effective_rate,
        company_size, notes
    """
    if profit <= 0:
        return {
            "cit_amount": Decimal("0"),
            "development_levy": Decimal("0"),
            "minimum_tax": Decimal("0"),
            "effective_rate": Decimal("0"),
            "company_size": "small",
            "taxable_profit": Decimal("0"),
            "notes": "No profit - no CIT liability",
        }

    turnover = float(annual_turnover or 0)

    # Determine company size and CIT rate based on turnover thresholds
    if turnover <= CIT_THRESHOLDS["small"]["max_turnover"]:
        company_size = "small"
        cit_rate = CIT_THRESHOLDS["small"]["rate"]
    elif turnover < CIT_THRESHOLDS["medium"]["max_turnover"]:
        company_size = "medium"
        cit_rate = CIT_THRESHOLDS["medium"]["rate"]
    else:
        company_size = "large"
        cit_rate = CIT_THRESHOLDS["large"]["rate"]

    # Calculate taxable profit after capital allowances
    assessable_profit = profit

    if capital_allowances and capital_allowances > 0:
        # Cap capital allowances at 66.67% of assessable profit
        max_allowance = assessable_profit * Decimal(str(MAX_CAPITAL_ALLOWANCE_RATIO))
        actual_allowance = min(capital_allowances, max_allowance)
        taxable_profit = assessable_profit - actual_allowance
    else:
        taxable_profit = assessable_profit

    if taxable_profit < Decimal("0"):
        taxable_profit = Decimal("0")

    # Calculate CIT
    cit_amount = taxable_profit * Decimal(str(cit_rate))

    # Calculate Development Levy (4% for non-small companies)
    if company_size == "small":
        development_levy = Decimal("0")
    else:
        development_levy = assessable_profit * Decimal(str(DEVELOPMENT_LEVY_RATE))

    # Calculate Minimum Tax (0.5% of turnover)
    # Applies if CIT is less than minimum tax
    minimum_tax = Decimal("0")
    if company_size != "small" and turnover > 0:
        min_tax_amount = Decimal(str(turnover)) * Decimal(str(MINIMUM_TAX_RATE))
        if cit_amount < min_tax_amount:
            minimum_tax = min_tax_amount - cit_amount
            # Note: In practice, company pays the higher of CIT or minimum tax

    # Calculate effective rate
    if profit > 0:
        total_tax = cit_amount + development_levy
        effective_rate = total_tax / profit * 100
    else:
        effective_rate = Decimal("0")

    # Generate notes based on company size
    notes = []
    if company_size == "small":
        notes.append("Small company (turnover ≤₦100M): CIT exempt (NTA 2025)")
    elif company_size == "medium":
        notes.append(f"Medium company (₦100M-₦250M): CIT rate {int(cit_rate * 100)}%")
        if development_levy > 0:
            notes.append("Development levy: 4% of assessable profit")
        if minimum_tax > 0:
            notes.append("Minimum tax applies (CIT < 0.5% of turnover)")
    else:
        notes.append(f"Large company (>₦250M): CIT rate {int(cit_rate * 100)}%")
        if development_levy > 0:
            notes.append("Development levy: 4% of assessable profit")
        if minimum_tax > 0:
            notes.append("Minimum tax applies (CIT < 0.5% of turnover)")

    return {
        "cit_amount": cit_amount.quantize(Decimal("0.01")),
        "development_levy": development_levy.quantize(Decimal("0.01")),
        "minimum_tax": minimum_tax.quantize(Decimal("0.01")),
        "effective_rate": effective_rate.quantize(Decimal("0.01")),
        "company_size": company_size,
        "taxable_profit": taxable_profit.quantize(Decimal("0.01")),
        "notes": "; ".join(notes),
    }


def compute_personal_income_tax(profit: Decimal) -> dict:
    """
    Calculate Nigerian Personal Income Tax (PIT) using progressive bands.

    Per 2026 Nigerian Tax Law:
    - Small businesses (turnover <₦50M) are exempt from CIT
    - Owners pay PIT on profit using progressive rates
    - Tax is charged on profit (Revenue - Expenses), not revenue

    Args:
        profit: Taxable profit (Revenue - Expenses)

    Returns:
        dict with pit_amount, effective_rate, band_label
    """
    if profit <= 0:
        return {
            "pit_amount": Decimal("0"),
            "effective_rate": Decimal("0"),
            "band_label": "0% (No profit)",
        }

    profit_float = float(profit)
    tax_amount = Decimal("0")
    previous_threshold = 0

    # Calculate tax using progressive bands
    for threshold, rate in PIT_BANDS:
        if profit_float <= previous_threshold:
            break

        taxable_in_band = min(profit_float, threshold) - previous_threshold
        tax_in_band = Decimal(str(taxable_in_band * rate))
        tax_amount += tax_in_band

        previous_threshold = threshold

    # Determine which band the profit falls into
    for threshold, rate in PIT_BANDS:
        if profit_float <= threshold:
            band_label = f"{int(rate * 100)}%"
            break
    else:
        band_label = "25%"

    # Calculate effective rate
    effective_rate = (tax_amount / profit * 100) if profit > 0 else Decimal("0")

    return {
        "pit_amount": tax_amount.quantize(Decimal("0.01")),
        "effective_rate": effective_rate.quantize(Decimal("0.01")),
        "band_label": band_label,
    }


def compute_revenue_by_date_range(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
    basis: str = "paid",
) -> Decimal:
    """
    Compute total revenue from REVENUE invoices for a date range.

    Uses unified Invoice model filtered by invoice_type='revenue'.

    Args:
        db: Database session
        user_id: User ID
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        basis: 'paid' (only paid invoices) or 'all' (all non-refunded)

    Returns:
        Total revenue for the period
    """
    from app.models.models import Invoice

    # Convert dates to datetime with timezone
    start_dt = datetime.combine(start_date, datetime.min.time()).replace(tzinfo=timezone.utc)
    end_dt = datetime.combine(end_date, datetime.max.time()).replace(tzinfo=timezone.utc)

    q = db.query(Invoice).filter(
        Invoice.issuer_id == user_id,
        Invoice.invoice_type == "revenue",  # Only revenue invoices
        Invoice.created_at >= start_dt,
        Invoice.created_at <= end_dt,
    )

    if basis == "paid":
        q = q.filter(Invoice.status == "paid")
    else:
        # Exclude both refunded AND cancelled invoices for "all" basis
        # Only count invoices that represent actual/potential revenue
        q = q.filter(Invoice.status.notin_(["refunded", "cancelled"]))

    invoices = q.all()
    total = Decimal("0")
    for inv in invoices:
        amount = Decimal(str(inv.amount))
        if inv.discount_amount:
            amount -= Decimal(str(inv.discount_amount))
        total += amount
    return total


def compute_expenses_by_date_range(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
) -> Decimal:
    """
    Compute total expenses from EXPENSE invoices for a date range.

    Uses unified Invoice model filtered by invoice_type='expense'.
    No longer uses separate Expense table.

    Args:
        db: Database session
        user_id: User ID
        start_date: Start date (inclusive)
        end_date: End date (inclusive)

    Returns:
        Total expenses for the period
    """
    from app.models.models import Invoice

    # Attribute each expense to WHEN IT WAS INCURRED (due_date), falling back to
    # created_at when no due_date is set. This mirrors the Expenses tab
    # (expense_summary) and the invoice list, so the tax report's expense total
    # reconciles with what the user sees on those screens instead of drifting
    # apart because one uses created_at and the other uses due_date.
    expense_date = func.coalesce(Invoice.due_date, Invoice.created_at)

    result = (
        db.query(func.sum(Invoice.amount))
        .filter(
            Invoice.issuer_id == user_id,
            Invoice.invoice_type == "expense",  # Only expense invoices
            Invoice.status == "paid",  # Only count paid expenses
            func.date(expense_date) >= start_date,
            func.date(expense_date) <= end_date,
        )
        .scalar()
    )

    return result or Decimal("0")


def compute_expense_evidence_breakdown(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
) -> dict[str, Decimal]:
    """Split paid expenses into documented, self-reported, and flagged totals."""
    from app.models.models import Invoice

    expense_date = func.coalesce(Invoice.due_date, Invoice.created_at)
    base_filters = (
        Invoice.issuer_id == user_id,
        Invoice.invoice_type == "expense",
        Invoice.status == "paid",
        func.date(expense_date) >= start_date,
        func.date(expense_date) <= end_date,
    )

    def total(*extra_filters) -> Decimal:
        value = db.query(func.sum(Invoice.amount)).filter(*base_filters, *extra_filters).scalar()
        return value or Decimal("0")

    unflagged = Invoice.expense_flag_reason.is_(None)
    return {
        "documented": total(unflagged, Invoice.receipt_url.is_not(None)),
        "self_reported": total(unflagged, Invoice.receipt_url.is_(None)),
        "flagged": total(Invoice.expense_flag_reason.is_not(None)),
    }


def compute_actual_profit_by_date_range(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
    basis: str = "paid",
) -> Decimal:
    """
    Compute ACTUAL profit: Revenue - Expenses.

    This is the correct taxable profit calculation per 2026 Nigerian Tax Law.

    Args:
        db: Database session
        user_id: User ID
        start_date: Start date (inclusive)
        end_date: End date (inclusive)
        basis: 'paid' (only paid invoices) or 'all' (all non-refunded)

    Returns:
        Actual profit (Revenue - Expenses) for the period
    """
    revenue = compute_revenue_by_date_range(db, user_id, start_date, end_date, basis)
    expenses = compute_expenses_by_date_range(db, user_id, start_date, end_date)

    profit = revenue - expenses

    logger.info(
        f"Profit calculation for user {user_id} ({start_date} to {end_date}): "
        f"Revenue={revenue}, Expenses={expenses}, Profit={profit}"
    )

    return profit
