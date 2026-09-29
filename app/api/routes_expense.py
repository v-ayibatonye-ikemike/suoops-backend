"""
Expense tracking API endpoints.

Handles CRUD operations for business expenses and provides summary/stats endpoints.
"""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.api.dependencies import get_data_owner_id
from app.api.rate_limit import limiter
from app.api.routes_auth import get_current_user_id
from app.core.audit import log_audit_event
from app.db.session import get_db
from app.models.expense_schemas import (
    ExpenseCreate,
    ExpenseOut,
    ExpenseStats,
    ExpenseSummary,
    ExpenseUpdate,
)
from app.models.models import Invoice
from app.services.expense_service import expense_invoice_to_out, record_expense_invoice

router = APIRouter(prefix="/expenses", tags=["expenses"])

# Type aliases for dependency injection
CurrentUserDep = Annotated[int, Depends(get_current_user_id)]
DataOwnerDep = Annotated[int, Depends(get_data_owner_id)]
DbDep = Annotated[Session, Depends(get_db)]


def _calculate_period_range(
    period_type: str,
    year: int | None = None,
    month: int | None = None,
    day: int | None = None,
    week: int | None = None,
) -> tuple[date, date]:
    """
    Calculate start and end date for a given period.

    Same logic as tax reporting for consistency.
    """
    today = date.today()

    if period_type == "day":
        if year and month and day:
            target_date = date(year, month, day)
        else:
            target_date = today
        return target_date, target_date

    elif period_type == "week":
        if year and week:
            # ISO week calculation
            jan4 = date(year, 1, 4)
            week_one_start = jan4 - timedelta(days=jan4.isoweekday() - 1)
            target_start = week_one_start + timedelta(weeks=week - 1)
            target_end = target_start + timedelta(days=6)
        else:
            # Current week
            target_start = today - timedelta(days=today.isoweekday() - 1)
            target_end = target_start + timedelta(days=6)
        return target_start, target_end

    elif period_type == "month":
        if year and month:
            target_year, target_month = year, month
        else:
            target_year, target_month = today.year, today.month

        start_date = date(target_year, target_month, 1)
        # Last day of month
        if target_month == 12:
            end_date = date(target_year, 12, 31)
        else:
            end_date = date(target_year, target_month + 1, 1) - timedelta(days=1)
        return start_date, end_date

    elif period_type == "year":
        target_year = year if year else today.year
        return date(target_year, 1, 1), date(target_year, 12, 31)

    else:
        raise ValueError(f"Invalid period_type: {period_type}")


@router.post("/", response_model=ExpenseOut, status_code=201)
@limiter.limit("30/minute")
def create_expense(
    request: Request,
    data: ExpenseCreate,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """
    Create a new expense manually from dashboard.

    For WhatsApp/email expenses, use the bot message handler.
    Expense is created under the data owner (team admin for members).
    """
    invoice = record_expense_invoice(
        db,
        user_id=data_owner_id,
        amount=data.amount,
        category=data.category,
        description=data.description,
        merchant=data.merchant,
        expense_date=data.expense_date,
        input_method="manual",
        channel="dashboard",
        notes=data.notes,
        created_by_user_id=current_user_id,
    )
    return expense_invoice_to_out(invoice)


@router.get("/", response_model=list[ExpenseOut])
def list_expenses(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    start_date: date | None = Query(None, description="Filter by start date (inclusive)"),
    end_date: date | None = Query(None, description="Filter by end date (inclusive)"),
    category: str | None = Query(None, description="Filter by category"),
    limit: int = Query(100, ge=1, le=500, description="Max results to return"),
    offset: int = Query(0, ge=0, description="Number of results to skip"),
):
    """
    List expenses with optional filters.

    Returns expenses sorted by date (most recent first).
    For team members, returns the team admin's expenses.
    """
    date_col = func.coalesce(Invoice.due_date, Invoice.created_at)
    q = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(
            Invoice.issuer_id == data_owner_id,
            Invoice.invoice_type == "expense",
        )
    )

    if start_date:
        q = q.filter(func.date(date_col) >= start_date)
    if end_date:
        q = q.filter(func.date(date_col) <= end_date)
    if category:
        q = q.filter(Invoice.category == category)

    q = q.order_by(date_col.desc(), Invoice.created_at.desc())
    q = q.limit(limit).offset(offset)

    return [expense_invoice_to_out(inv) for inv in q.all()]


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(
    expense_id: int,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Get a specific expense by ID"""
    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(
            Invoice.id == expense_id,
            Invoice.issuer_id == data_owner_id,
            Invoice.invoice_type == "expense",
        )
        .first()
    )

    if not invoice:
        raise HTTPException(status_code=404, detail="Expense not found")

    return expense_invoice_to_out(invoice)


@router.put("/{expense_id}", response_model=ExpenseOut)
def update_expense(
    expense_id: int,
    data: ExpenseUpdate,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Update an existing expense"""
    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.lines))
        .filter(
            Invoice.id == expense_id,
            Invoice.issuer_id == data_owner_id,
            Invoice.invoice_type == "expense",
        )
        .first()
    )

    if not invoice:
        raise HTTPException(status_code=404, detail="Expense not found")

    update_data = data.model_dump(exclude_unset=True)
    old_values = {
        "amount": str(invoice.amount),
        "expense_date": (invoice.due_date or invoice.created_at).date().isoformat(),
        "category": invoice.category,
        "description": invoice.lines[0].description if invoice.lines else None,
        "merchant": invoice.merchant or invoice.vendor_name,
        "notes": invoice.notes,
    }
    first_line = invoice.lines[0] if invoice.lines else None

    if invoice.receipt_url and any(field in update_data for field in ("amount", "expense_date", "merchant")):
        invoice.expense_flag_reason = invoice.expense_flag_reason or "receipt_details_edited"

    if update_data.get("amount") is not None:
        invoice.amount = update_data["amount"]
        if first_line is not None:
            first_line.unit_price = update_data["amount"]
            first_line.quantity = 1
    if update_data.get("expense_date") is not None:
        ed = update_data["expense_date"]
        invoice.due_date = (
            ed if isinstance(ed, datetime) else datetime.combine(ed, datetime.min.time(), tzinfo=timezone.utc)
        )
    if "category" in update_data:
        invoice.category = update_data["category"]
    if "description" in update_data and first_line is not None:
        first_line.description = update_data["description"]
    if "merchant" in update_data:
        invoice.merchant = update_data["merchant"]
        invoice.vendor_name = update_data["merchant"]
    if "notes" in update_data:
        invoice.notes = update_data["notes"]

    invoice.status_updated_at = datetime.now(timezone.utc)

    db.commit()
    db.refresh(invoice)

    log_audit_event(
        "expense.updated",
        user_id=current_user_id,
        expense_id=invoice.id,
        data_owner_id=data_owner_id,
        old_values=old_values,
        new_values={
            key: (
                str(value)
                if isinstance(value, Decimal)
                else value.isoformat()
                if isinstance(value, (date, datetime))
                else value
            )
            for key, value in update_data.items()
        },
        documented=bool(invoice.receipt_url),
        flag_reason=invoice.expense_flag_reason,
    )

    return expense_invoice_to_out(invoice)


@router.delete("/{expense_id}", status_code=204)
def delete_expense(
    expense_id: int,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Delete an expense"""
    invoice = (
        db.query(Invoice)
        .filter(
            Invoice.id == expense_id,
            Invoice.issuer_id == data_owner_id,
            Invoice.invoice_type == "expense",
        )
        .first()
    )

    if not invoice:
        raise HTTPException(status_code=404, detail="Expense not found")

    deleted_values = {
        "amount": str(invoice.amount),
        "expense_date": (invoice.due_date or invoice.created_at).date().isoformat(),
        "category": invoice.category,
        "merchant": invoice.merchant or invoice.vendor_name,
        "documented": bool(invoice.receipt_url),
        "flag_reason": invoice.expense_flag_reason,
    }
    db.delete(invoice)
    db.commit()
    log_audit_event(
        "expense.deleted",
        user_id=current_user_id,
        expense_id=expense_id,
        data_owner_id=data_owner_id,
        deleted_values=deleted_values,
    )

    return None


@router.get("/summary/by-period", response_model=ExpenseSummary)
def expense_summary(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period_type: str = Query("month", pattern="^(day|week|month|year)$"),
    year: int | None = Query(None, description="Year (required for week/month/year)"),
    month: int | None = Query(None, ge=1, le=12, description="Month (1-12, for month period)"),
    day: int | None = Query(None, ge=1, le=31, description="Day (for day period)"),
    week: int | None = Query(None, ge=1, le=53, description="ISO week number (for week period)"),
):
    """
    Get expense summary by category for a given period.

    Examples:
    - Daily: /summary/by-period?period_type=day&year=2025&month=11&day=10
    - Weekly: /summary/by-period?period_type=week&year=2025&week=45
    - Monthly: /summary/by-period?period_type=month&year=2025&month=11
    - Yearly: /summary/by-period?period_type=year&year=2025
    - Current month: /summary/by-period?period_type=month
    """
    # Calculate date range
    start_date, end_date = _calculate_period_range(period_type, year, month, day, week)

    # SQL aggregation over unified expense-invoices (invoice_type='expense').
    date_col = func.coalesce(Invoice.due_date, Invoice.created_at)
    rows = (
        db.query(
            Invoice.category,
            func.sum(Invoice.amount).label("total"),
            func.count(Invoice.id).label("cnt"),
        )
        .filter(
            Invoice.issuer_id == data_owner_id,
            Invoice.invoice_type == "expense",
            Invoice.status == "paid",
            func.date(date_col) >= start_date,
            func.date(date_col) <= end_date,
        )
        .group_by(Invoice.category)
        .all()
    )

    by_category = {(row.category or "other"): float(row.total) for row in rows}
    total = sum(row.total for row in rows)
    count = sum(row.cnt for row in rows)

    return ExpenseSummary(
        total_expenses=float(total),
        by_category=by_category,
        period_type=period_type,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
        count=count,
    )


@router.get("/stats/overview", response_model=ExpenseStats)
def expense_stats(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period_type: str = Query("month", pattern="^(day|week|month|year)$"),
    year: int | None = Query(None),
    month: int | None = Query(None, ge=1, le=12),
    day: int | None = Query(None, ge=1, le=31),
    week: int | None = Query(None, ge=1, le=53),
):
    """
    Get comprehensive expense statistics including revenue and profit.

    Shows:
    - Total expenses
    - Total revenue (from invoices)
    - Actual profit (revenue - expenses)
    - Expense-to-revenue ratio
    - Top expense categories
    """
    from app.models.models import Invoice
    from app.services.tax_reporting_service import compute_revenue_by_date_range

    # Calculate date range
    start_date, end_date = _calculate_period_range(period_type, year, month, day, week)

    # Expenses are stored as unified invoices (invoice_type='expense'), NOT the
    # legacy Expense table. Aggregate them the SAME way the Expense list page
    # does — by coalesce(due_date, created_at) so backdated expenses land in the
    # right period — and only count paid ones (expenses are created as paid).
    expense_date_col = func.coalesce(Invoice.due_date, Invoice.created_at)
    expense_filters = (
        Invoice.issuer_id == data_owner_id,
        Invoice.invoice_type == "expense",
        Invoice.status == "paid",
        func.date(expense_date_col) >= start_date,
        func.date(expense_date_col) <= end_date,
    )

    stats_row = (
        db.query(
            func.coalesce(func.sum(Invoice.amount), 0).label("total"),
        )
        .filter(*expense_filters)
        .first()
    )
    total_expenses = Decimal(str(stats_row.total))

    # Top categories via SQL
    category_rows = (
        db.query(
            Invoice.category,
            func.sum(Invoice.amount).label("total"),
        )
        .filter(*expense_filters)
        .group_by(Invoice.category)
        .order_by(func.sum(Invoice.amount).desc())
        .limit(5)
        .all()
    )
    top_categories = [{(row.category or "other"): float(row.total)} for row in category_rows]

    # Get revenue from invoices (use data_owner_id for team context)
    total_revenue = compute_revenue_by_date_range(
        db=db,
        user_id=data_owner_id,
        start_date=start_date,
        end_date=end_date,
        basis="paid",  # Use paid basis for actual cash flow
    )

    # Calculate profit
    actual_profit = total_revenue - total_expenses

    # Calculate expense-to-revenue ratio
    if total_revenue > 0:
        expense_ratio = float(total_expenses / total_revenue * 100)
    else:
        expense_ratio = 0.0

    return ExpenseStats(
        total_expenses=float(total_expenses),
        total_revenue=float(total_revenue),
        actual_profit=float(actual_profit),
        expense_to_revenue_ratio=round(expense_ratio, 2),
        top_categories=top_categories,
        period_type=period_type,
        start_date=start_date.isoformat(),
        end_date=end_date.isoformat(),
    )
