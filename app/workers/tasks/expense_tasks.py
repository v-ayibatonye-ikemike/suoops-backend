"""
Expense Tracking Tasks.

Celery tasks for expense summaries and reminders.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from celery import Task
from sqlalchemy import func

from app.db.session import session_scope
from app.models.models import User
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="expense.send_summary",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=3,
)
def send_expense_summary(
    self: Task,
    user_id: int,
    period: str = "weekly",
) -> dict[str, Any]:
    """
    Send expense summary to user via WhatsApp/email.

    Args:
        user_id: User ID
        period: 'daily', 'weekly', or 'monthly'

    Returns:
        Summary statistics
    """
    from app.models.models import Invoice
    from app.services.tax_reporting_service import (
        compute_actual_profit_by_date_range,
        compute_revenue_by_date_range,
    )

    with session_scope() as db:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.error("User %s not found for expense summary", user_id)
            return {"success": False, "error": "User not found"}

        start_date, end_date = _calculate_period_range(period)

        # Expenses are unified invoices (invoice_type='expense'); filter on the
        # expense date (due_date) falling back to created_at, matching the app.
        expense_date_col = func.coalesce(Invoice.due_date, Invoice.created_at)
        expenses = (
            db.query(Invoice)
            .filter(
                Invoice.issuer_id == user_id,
                Invoice.invoice_type == "expense",
                Invoice.status == "paid",
                func.date(expense_date_col) >= start_date,
                func.date(expense_date_col) <= end_date,
            )
            .all()
        )

        by_category, total_expenses = _aggregate_expenses(expenses)
        revenue = compute_revenue_by_date_range(db, user_id, start_date, end_date, "paid")
        profit = compute_actual_profit_by_date_range(db, user_id, start_date, end_date, "paid")
        pit_band = _get_pit_band(profit, period)

        message = _format_summary_message(period, revenue, total_expenses, profit, by_category, pit_band)

        if user.phone:
            _send_whatsapp_message(user.phone, message, user_id, period)

        return {
            "success": True,
            "user_id": user_id,
            "period": period,
            "total_expenses": float(total_expenses),
            "revenue": float(revenue),
            "profit": float(profit),
            "categories": len(by_category),
        }


@celery_app.task(
    bind=True,
    name="expense.send_reminders",
    autoretry_for=(Exception,),
    retry_backoff=True,
    max_retries=2,
)
def send_expense_reminders(self: Task) -> dict[str, Any]:
    """Nudge active businesses that recorded no expense in the past week.

    Email is the free primary channel. WhatsApp is used only when the user has
    no working email and their free-form 24-hour conversation window is open.
    """
    from sqlalchemy import or_

    from app.models.models import Invoice, UserEmailLog
    from app.utils.smtp import send_smtp_batch

    with session_scope() as db:
        today = date.today()
        seven_days_ago = today - timedelta(days=7)
        active_since = today - timedelta(days=30)
        iso_year, iso_week, _ = today.isocalendar()
        email_type = f"expense_habit_{iso_year}_{iso_week:02d}"

        expense_date_col = func.coalesce(Invoice.due_date, Invoice.created_at)
        recent_expense_users = db.query(Invoice.issuer_id).filter(
            Invoice.invoice_type == "expense",
            Invoice.status == "paid",
            func.date(expense_date_col) >= seven_days_ago,
        )
        active_revenue_users = db.query(Invoice.issuer_id).filter(
            Invoice.invoice_type == "revenue",
            func.date(Invoice.created_at) >= active_since,
        )
        already_reminded = db.query(UserEmailLog.user_id).filter(
            UserEmailLog.email_type == email_type,
        )

        users = (
            db.query(User)
            .filter(
                User.id.in_(active_revenue_users),
                ~User.id.in_(recent_expense_users),
                ~User.id.in_(already_reminded),
                or_(User.email.isnot(None), User.phone.isnot(None)),
            )
            .all()
        )
        if not users:
            return {
                "success": True,
                "email_sent": 0,
                "whatsapp_sent": 0,
                "users_targeted": 0,
                "failed": 0,
            }

        pending_email: list[tuple[User, str]] = []
        whatsapp_candidates: list[User] = []
        for user in users:
            name = (user.name or "there").split()[0]
            plain = (
                f"Hi {name},\n\nSales are not the same as profit. Record this week's "
                "transport, data, stock, supplies and other business costs so SuoOps "
                "can show what you truly earned and keep your tax records accurate.\n\n"
                "Quick add on your dashboard: https://suoops.com/dashboard/expenses\n\n"
                "Or send this on WhatsApp: Expense ₦5000 for transport\n\n— SuoOps"
            )
            if user.email:
                pending_email.append((user, plain))
            else:
                whatsapp_candidates.append(user)

        email_sent = 0
        whatsapp_sent = 0
        failed = 0
        if pending_email:
            results = send_smtp_batch(
                [(user.email, "Know what you actually earned this week", None, plain) for user, plain in pending_email]
            )
            for (user, _), sent in zip(pending_email, results):
                if sent:
                    db.add(UserEmailLog(user_id=user.id, email_type=email_type))
                    email_sent += 1
                else:
                    whatsapp_candidates.append(user)

        from app.bot.conversation_window import is_window_open
        from app.core.whatsapp import get_whatsapp_client
        from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

        client = get_whatsapp_client()
        for user in whatsapp_candidates:
            if not user.phone or not is_window_open(user.phone):
                failed += 1
                continue
            if not can_send_whatsapp(priority=False):
                failed += 1
                continue
            name = (user.name or "there").split()[0]
            message = (
                f"Hi {name} 👋 Sales are not the same as profit. Add this week's costs "
                "so your profit and tax records stay accurate.\n\n"
                "Reply like: *Expense ₦5000 for transport*\n"
                "Or use: suoops.com/dashboard/expenses"
            )
            try:
                if client.send_text(user.phone, message):
                    record_whatsapp_send(priority=False)
                    db.add(UserEmailLog(user_id=user.id, email_type=email_type))
                    whatsapp_sent += 1
                else:
                    failed += 1
            except Exception as exc:
                logger.warning("Expense habit nudge failed for user %s: %s", user.id, exc)
                failed += 1

        db.commit()
        logger.info(
            "Expense habit reminders: targeted=%d email=%d whatsapp=%d failed=%d",
            len(users),
            email_sent,
            whatsapp_sent,
            failed,
        )

        return {
            "success": True,
            "email_sent": email_sent,
            "whatsapp_sent": whatsapp_sent,
            "users_targeted": len(users),
            "failed": failed,
        }


# ============================================================================
# Private Helper Functions
# ============================================================================


def _calculate_period_range(period: str) -> tuple[date, date]:
    """Calculate date range for period."""
    today = date.today()
    if period == "daily":
        return today, today
    elif period == "weekly":
        return today - timedelta(days=7), today
    elif period == "monthly":
        return today.replace(day=1), today
    return today - timedelta(days=30), today


def _aggregate_expenses(expenses) -> tuple[dict[str, Decimal], Decimal]:
    """Aggregate expenses by category."""
    by_category: dict[str, Decimal] = {}
    total_expenses = Decimal("0")

    for expense in expenses:
        cat = expense.category
        by_category[cat] = by_category.get(cat, Decimal("0")) + expense.amount
        total_expenses += expense.amount

    return by_category, total_expenses


def _get_pit_band(profit: Decimal, period: str) -> str:
    """Determine PIT band based on annualized profit."""
    multiplier = 12 if period == "monthly" else 52 if period == "weekly" else 365
    annual_profit = profit * multiplier

    if annual_profit <= 800_000:
        return "0%"
    elif annual_profit <= 3_000_000:
        return "15%"
    elif annual_profit <= 12_000_000:
        return "18%"
    elif annual_profit <= 25_000_000:
        return "21%"
    elif annual_profit <= 50_000_000:
        return "23%"
    return "25%"


def _format_summary_message(
    period: str,
    revenue: Decimal,
    total_expenses: Decimal,
    profit: Decimal,
    by_category: dict[str, Decimal],
    pit_band: str,
) -> str:
    """Format expense summary message."""
    period_display = period.title()
    message = f"📊 {period_display} Financial Summary\n\n"
    message += f"💰 Total Income: ₦{revenue:,.0f}\n"
    message += f"💸 Total Expenses: ₦{total_expenses:,.0f}\n"
    message += f"✅ Profit: ₦{profit:,.0f}\n\n"

    if by_category:
        message += "📂 Expenses by Category:\n"
        for category, amount in sorted(by_category.items(), key=lambda x: x[1], reverse=True):
            cat_display = category.replace("_", " ").title()
            message += f"  • {cat_display}: ₦{amount:,.0f}\n"
        message += "\n"

    message += f"💡 Expected PIT band: {pit_band}"
    return message


def _send_whatsapp_message(phone: str, message: str, user_id: int, period: str) -> None:
    """Send WhatsApp message."""
    from app.core.whatsapp import get_whatsapp_client

    try:
        client = get_whatsapp_client()
        client.send_text(phone, message)
        logger.info("Sent %s expense summary to user %s", period, user_id)
    except Exception as e:
        logger.error("Failed to send WhatsApp summary to %s: %s", user_id, e)
