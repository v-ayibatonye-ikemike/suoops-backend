"""Analytics calculation service for business metrics."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import and_, case, extract, func, or_
from sqlalchemy.orm import Session

from app.models import models
from app.models.schemas import (
    AgingReport,
    CustomerMetrics,
    InvoiceMetrics,
    MonthlyTrend,
    RevenueMetrics,
)


def exclude_abandoned_storefront():
    """SQLAlchemy filter clause that drops unpaid/abandoned storefront carts.

    A storefront checkout that was started but never paid stays as a
    ``channel=='storefront'`` invoice in ``'pending'``. Those are drop-offs, not
    real receivables, so every Insights metric excludes them — the same rule the
    conversion funnel uses — keeping revenue, invoice counts and the funnel
    consistent with one another.
    """
    return or_(
        models.Invoice.channel.is_(None),
        models.Invoice.channel != "storefront",
        models.Invoice.status != "pending",
    )


def calculate_revenue_metrics(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
    conversion_rate: Decimal,
) -> RevenueMetrics:
    """Calculate total revenue, paid, pending, and overdue amounts.

    Uses a single SQL aggregation query instead of loading all invoices
    into Python memory.
    """
    end_dt = datetime.combine(end_date, datetime.max.time())
    start_dt = datetime.combine(start_date, datetime.min.time())

    # Single query with conditional aggregation
    row = (
        db.query(
            func.coalesce(func.sum(models.Invoice.amount), 0).label("total"),
            func.coalesce(
                func.sum(case((models.Invoice.status == "paid", models.Invoice.amount), else_=0)),
                0,
            ).label("paid"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                models.Invoice.status == "pending",
                                models.Invoice.due_date != None,  # noqa: E711
                                models.Invoice.due_date < end_dt,
                            ),
                            models.Invoice.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("overdue"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                models.Invoice.status == "pending",
                                or_(
                                    models.Invoice.due_date == None,  # noqa: E711
                                    models.Invoice.due_date >= end_dt,
                                ),
                            ),
                            models.Invoice.amount,
                        ),
                        (models.Invoice.status == "awaiting_confirmation", models.Invoice.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("pending"),
            func.count(models.Invoice.id).label("cnt"),
        )
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= start_dt,
            models.Invoice.created_at <= end_dt,
            exclude_abandoned_storefront(),
        )
        .first()
    )

    total_revenue = Decimal(str(row.total)) / conversion_rate
    paid_revenue = Decimal(str(row.paid)) / conversion_rate
    overdue_revenue = Decimal(str(row.overdue)) / conversion_rate
    pending_revenue = Decimal(str(row.pending)) / conversion_rate
    count = row.cnt or 0

    # Calculate previous period for growth
    period_days = (end_date - start_date).days
    prev_start = start_date - timedelta(days=period_days)

    prev_revenue = (
        db.query(func.sum(models.Invoice.amount))
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "paid",
            models.Invoice.created_at >= datetime.combine(prev_start, datetime.min.time()),
            models.Invoice.created_at < start_dt,
        )
        .scalar()
    ) or Decimal("0")

    prev_revenue = prev_revenue / conversion_rate

    # Calculate growth percentage
    if prev_revenue > 0:
        growth_rate = float(((paid_revenue - prev_revenue) / prev_revenue) * 100)
    else:
        growth_rate = 100.0 if paid_revenue > 0 else 0.0

    return RevenueMetrics(
        total_revenue=float(total_revenue),
        paid_revenue=float(paid_revenue),
        pending_revenue=float(pending_revenue),
        overdue_revenue=float(overdue_revenue),
        growth_rate=growth_rate,
        average_invoice_value=float(total_revenue / count) if count else 0.0,
    )


def calculate_invoice_metrics(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
) -> InvoiceMetrics:
    """Calculate invoice counts by status."""
    
    invoices = (
        db.query(
            func.count(models.Invoice.id).label("total"),
            func.sum(case((models.Invoice.status == "paid", 1), else_=0)).label("paid"),
            func.sum(case((models.Invoice.status == "pending", 1), else_=0)).label("pending"),
            func.sum(case((models.Invoice.status == "failed", 1), else_=0)).label("failed"),
            func.sum(case((models.Invoice.status == "awaiting_confirmation", 1), else_=0)).label("awaiting"),
            func.sum(case((models.Invoice.status == "cancelled", 1), else_=0)).label("cancelled"),
        )
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= datetime.combine(start_date, datetime.min.time()),
            models.Invoice.created_at <= datetime.combine(end_date, datetime.max.time()),
            exclude_abandoned_storefront(),
        )
        .first()
    )
    
    total = invoices.total or 0
    paid = invoices.paid or 0
    pending = invoices.pending or 0
    failed = invoices.failed or 0
    awaiting = invoices.awaiting or 0
    cancelled = invoices.cancelled or 0
    
    # Calculate conversion rate (paid / total)
    conversion_rate = (paid / total * 100) if total > 0 else 0.0
    
    return InvoiceMetrics(
        total_invoices=total,
        paid_invoices=paid,
        pending_invoices=pending,
        failed_invoices=failed,
        awaiting_confirmation=awaiting,
        cancelled_invoices=cancelled,
        conversion_rate=conversion_rate,
    )


def calculate_customer_metrics(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
) -> CustomerMetrics:
    """Calculate customer counts and repeat customer rate."""
    
    # Get unique customers in period
    customers_in_period = (
        db.query(func.count(func.distinct(models.Invoice.customer_id)))
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= datetime.combine(start_date, datetime.min.time()),
            models.Invoice.created_at <= datetime.combine(end_date, datetime.max.time()),
            exclude_abandoned_storefront(),
        )
        .scalar()
    ) or 0
    
    # Get total unique customers ever
    total_customers = (
        db.query(func.count(func.distinct(models.Invoice.customer_id)))
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            exclude_abandoned_storefront(),
        )
        .scalar()
    ) or 0
    
    # Get customers with multiple invoices (repeat customers)
    repeat_customers = (
        db.query(models.Invoice.customer_id)
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= datetime.combine(start_date, datetime.min.time()),
            models.Invoice.created_at <= datetime.combine(end_date, datetime.max.time()),
            exclude_abandoned_storefront(),
        )
        .group_by(models.Invoice.customer_id)
        .having(func.count(models.Invoice.id) > 1)
    ).count()
    
    # Calculate repeat rate
    repeat_rate = (repeat_customers / customers_in_period * 100) if customers_in_period > 0 else 0.0
    
    return CustomerMetrics(
        total_customers=total_customers,
        active_customers=customers_in_period,
        new_customers=customers_in_period,  # Simplified - in production, filter by first invoice date
        repeat_customer_rate=repeat_rate,
    )


def calculate_aging_report(
    db: Session,
    user_id: int,
    reference_date: date,
    conversion_rate: Decimal,
) -> AgingReport:
    """Calculate accounts receivable aging buckets.

    Uses SQL CASE bucketing instead of loading all unpaid invoices into memory.
    """
    cutoff_30 = datetime.combine(reference_date - timedelta(days=30), datetime.min.time())
    cutoff_60 = datetime.combine(reference_date - timedelta(days=60), datetime.min.time())
    cutoff_90 = datetime.combine(reference_date - timedelta(days=90), datetime.min.time())

    base_filter = [
        models.Invoice.issuer_id == user_id,
        models.Invoice.invoice_type == "revenue",
        models.Invoice.status.in_(["pending", "awaiting_confirmation"]),
        exclude_abandoned_storefront(),
    ]

    row = (
        db.query(
            # Current bucket: no due_date OR due_date within last 30 days or in future
            func.coalesce(
                func.sum(
                    case(
                        (
                            or_(
                                models.Invoice.due_date == None,  # noqa: E711
                                models.Invoice.due_date >= cutoff_30,
                            ),
                            models.Invoice.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("current_bucket"),
            # 31-60 days
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                models.Invoice.due_date < cutoff_30,
                                models.Invoice.due_date >= cutoff_60,
                            ),
                            models.Invoice.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("days_31_60"),
            # 61-90 days
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                models.Invoice.due_date < cutoff_60,
                                models.Invoice.due_date >= cutoff_90,
                            ),
                            models.Invoice.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("days_61_90"),
            # Over 90 days
            func.coalesce(
                func.sum(
                    case(
                        (models.Invoice.due_date < cutoff_90, models.Invoice.amount),
                        else_=0,
                    )
                ),
                0,
            ).label("over_90"),
        )
        .filter(*base_filter)
        .first()
    )

    current = Decimal(str(row.current_bucket)) / conversion_rate
    d31_60 = Decimal(str(row.days_31_60)) / conversion_rate
    d61_90 = Decimal(str(row.days_61_90)) / conversion_rate
    over_90 = Decimal(str(row.over_90)) / conversion_rate
    total_outstanding = current + d31_60 + d61_90 + over_90

    return AgingReport(
        current=float(current),
        days_31_60=float(d31_60),
        days_61_90=float(d61_90),
        over_90_days=float(over_90),
        total_outstanding=float(total_outstanding),
    )


def calculate_monthly_trends(
    db: Session,
    user_id: int,
    end_date: date,
    conversion_rate: Decimal,
) -> list[MonthlyTrend]:
    """Calculate revenue and invoice trends for last 12 months.

    Uses a single GROUP BY query instead of 36 individual queries (3 per month).
    """
    # Determine the 12-month window (current month + 11 prior months)
    end_month = end_date.replace(day=1)
    # Go back 11 months from end_month to get exactly 12 months total
    start_month_raw = end_month.month - 11
    start_year = end_month.year
    if start_month_raw <= 0:
        start_month_raw += 12
        start_year -= 1
    month_start = date(start_year, start_month_raw, 1)
    end_dt = datetime.combine(end_date, datetime.max.time())
    start_dt = datetime.combine(month_start, datetime.min.time())

    yr_col = extract("year", models.Invoice.created_at).label("yr")
    mo_col = extract("month", models.Invoice.created_at).label("mo")

    rows = (
        db.query(
            yr_col,
            mo_col,
            models.Invoice.invoice_type,
            func.coalesce(
                func.sum(
                    case((models.Invoice.status == "paid", models.Invoice.amount), else_=0)
                ),
                0,
            ).label("paid_amount"),
            func.coalesce(func.sum(models.Invoice.amount), 0).label("total_amount"),
            func.count(models.Invoice.id).label("cnt"),
        )
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type.in_(["revenue", "expense"]),
            models.Invoice.created_at >= start_dt,
            models.Invoice.created_at <= end_dt,
            exclude_abandoned_storefront(),
        )
        .group_by(yr_col, mo_col, models.Invoice.invoice_type)
        .all()
    )

    # Build lookup: (year, month) → {revenue, expenses, count}
    data: dict[tuple[int, int], dict] = {}
    for row in rows:
        key = (int(row.yr), int(row.mo))
        entry = data.setdefault(key, {"revenue": Decimal("0"), "expenses": Decimal("0"), "count": 0})
        if row.invoice_type == "revenue":
            entry["revenue"] = Decimal(str(row.paid_amount))
            entry["count"] = row.cnt
        elif row.invoice_type == "expense":
            entry["expenses"] = Decimal(str(row.total_amount))

    # Build ordered trend list for the 12-month window
    trends: list[MonthlyTrend] = []
    cursor = month_start
    while cursor <= end_date:
        key = (cursor.year, cursor.month)
        entry = data.get(key, {"revenue": Decimal("0"), "expenses": Decimal("0"), "count": 0})

        revenue_converted = entry["revenue"] / conversion_rate
        expenses_converted = entry["expenses"] / conversion_rate
        profit = revenue_converted - expenses_converted

        trends.append(
            MonthlyTrend(
                month=cursor.strftime("%b %Y"),
                revenue=float(revenue_converted),
                expenses=float(expenses_converted),
                profit=float(profit),
                invoice_count=entry["count"],
            )
        )

        # Advance to next month
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)

    return trends


def get_date_range(period: str) -> tuple[date, date]:
    """Calculate start and end dates based on period."""
    today = date.today()
    
    if period == "7d":
        start_date = today - timedelta(days=7)
    elif period == "30d":
        start_date = today - timedelta(days=30)
    elif period == "90d":
        start_date = today - timedelta(days=90)
    elif period == "1y":
        start_date = today - timedelta(days=365)
    else:  # all
        start_date = date(2020, 1, 1)
    
    return start_date, today


def get_conversion_rate(currency: str) -> Decimal:
    """Get currency conversion rate (NGN to target currency).

    Uses real-time exchange rate with 1-hour caching.
    Falls back to NGN_USD_RATE env var, then to a hardcoded default.
    """
    from app.services.exchange_rate import get_conversion_rate as _get_rate

    return _get_rate(currency)


def calculate_storefront_insights(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
    conversion_rate: Decimal,
    top_limit: int = 5,
) -> dict:
    """Storefront performance: views, orders, GMV, ratings, top products, demand.

    Store-lifetime counters (views, reviews, rating, conversion) sit alongside
    period-scoped order metrics so a seller sees both the big picture and the
    selected window. Money is in the requested currency and reflects goods value
    only (the buyer pays the service fee on top, so the seller keeps the full
    listed price). Returns ``enabled=False`` when the business has no storefront.
    """
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None or not getattr(user, "storefront_enabled", False):
        return {
            "enabled": False,
            "slug": None,
            "store_url": None,
            "views": 0,
            "reviews": 0,
            "avg_rating": None,
            "conversion_rate": 0.0,
            "orders": 0,
            "paid_orders": 0,
            "abandoned_orders": 0,
            "gmv": 0.0,
            "avg_order_value": 0.0,
            "awaiting_release": 0.0,
            "refunds": 0,
            "disputes": 0,
            "restock_requests": 0,
            "top_products": [],
        }

    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    Escrow = models.StorefrontOrderEscrow
    PAID = ("held", "released")  # payment collected & not refunded

    # ── Period order metrics ──
    row = (
        db.query(
            func.count(Escrow.id).label("orders"),
            func.coalesce(func.sum(case((Escrow.status.in_(PAID), 1), else_=0)), 0).label("paid"),
            func.coalesce(func.sum(case((Escrow.status == "pending", 1), else_=0)), 0).label("abandoned"),
            func.coalesce(func.sum(case((Escrow.status == "refunded", 1), else_=0)), 0).label("refunds"),
            func.coalesce(func.sum(case((Escrow.status == "disputed", 1), else_=0)), 0).label("disputes"),
            func.coalesce(func.sum(case((Escrow.status.in_(PAID), Escrow.gross_kobo), else_=0)), 0).label("gmv_kobo"),
            func.coalesce(func.sum(case((Escrow.status == "held", Escrow.payout_kobo), else_=0)), 0).label("held_kobo"),
        )
        .filter(
            Escrow.seller_id == user_id,
            Escrow.created_at >= start_dt,
            Escrow.created_at <= end_dt,
        )
        .first()
    )

    paid_orders = int(row.paid or 0)
    gmv = (Decimal(str(row.gmv_kobo or 0)) / 100) / conversion_rate
    awaiting_release = (Decimal(str(row.held_kobo or 0)) / 100) / conversion_rate
    avg_order_value = (gmv / paid_orders) if paid_orders else Decimal("0")

    # ── Lifetime store stats ──
    views = int(getattr(user, "storefront_views", 0) or 0)
    lifetime_paid = (
        db.query(func.count(Escrow.id))
        .filter(Escrow.seller_id == user_id, Escrow.status.in_(PAID))
        .scalar()
    ) or 0
    conversion = (lifetime_paid / views * 100) if views > 0 else 0.0

    rating_row = (
        db.query(
            func.count(models.StorefrontReview.id).label("cnt"),
            func.avg(models.StorefrontReview.rating).label("avg"),
        )
        .filter(
            models.StorefrontReview.user_id == user_id,
            models.StorefrontReview.approved.is_(True),
        )
        .first()
    )
    reviews = int(rating_row.cnt or 0)
    avg_rating = round(float(rating_row.avg), 2) if rating_row.avg is not None else None

    restock_requests = (
        db.query(func.count(models.StorefrontStockNotification.id))
        .filter(
            models.StorefrontStockNotification.user_id == user_id,
            models.StorefrontStockNotification.notified.is_(False),
        )
        .scalar()
    ) or 0

    # ── Top products (units + revenue from paid orders in the period) ──
    top_limit = max(1, min(int(top_limit or 5), 100))
    top_rows = (
        db.query(
            models.InvoiceLine.description.label("name"),
            func.coalesce(func.sum(models.InvoiceLine.quantity), 0).label("units"),
            func.coalesce(
                func.sum(models.InvoiceLine.quantity * models.InvoiceLine.unit_price), 0
            ).label("revenue"),
        )
        .join(models.Invoice, models.InvoiceLine.invoice_id == models.Invoice.id)
        .join(Escrow, Escrow.invoice_id == models.Invoice.id)
        .filter(
            Escrow.seller_id == user_id,
            Escrow.status.in_(PAID),
            Escrow.created_at >= start_dt,
            Escrow.created_at <= end_dt,
        )
        .group_by(models.InvoiceLine.description)
        .order_by(func.sum(models.InvoiceLine.quantity).desc())
        .limit(top_limit)
        .all()
    )
    # Total DISTINCT products sold in the period, so the UI knows whether there
    # are more beyond what it's showing (drives a "Show all" control).
    top_products_total = (
        db.query(func.count(func.distinct(models.InvoiceLine.description)))
        .join(models.Invoice, models.InvoiceLine.invoice_id == models.Invoice.id)
        .join(Escrow, Escrow.invoice_id == models.Invoice.id)
        .filter(
            Escrow.seller_id == user_id,
            Escrow.status.in_(PAID),
            Escrow.created_at >= start_dt,
            Escrow.created_at <= end_dt,
        )
        .scalar()
    ) or 0
    top_products = [
        {
            "name": r.name,
            "units": int(r.units or 0),
            "revenue": float(Decimal(str(r.revenue or 0)) / conversion_rate),
        }
        for r in top_rows
    ]

    slug = getattr(user, "storefront_slug", None)
    from app.core.config import settings

    base = (getattr(settings, "FRONTEND_URL", "") or "").rstrip("/")
    store_url = f"{base}/store/{slug}" if (slug and base) else None

    return {
        "enabled": True,
        "slug": slug,
        "store_url": store_url,
        "views": views,
        "reviews": reviews,
        "avg_rating": avg_rating,
        "conversion_rate": round(conversion, 2),
        "orders": int(row.orders or 0),
        "paid_orders": paid_orders,
        "abandoned_orders": int(row.abandoned or 0),
        "gmv": float(gmv),
        "avg_order_value": float(avg_order_value),
        "awaiting_release": float(awaiting_release),
        "refunds": int(row.refunds or 0),
        "disputes": int(row.disputes or 0),
        "restock_requests": int(restock_requests),
        "top_products": top_products,
        "top_products_total": int(top_products_total),
    }


# ── Cash-First Dashboard ─────────────────────────────────────────────


def calculate_cash_position(db: Session, user_id: int) -> dict:
    """Cash-first dashboard: collected, outstanding, overdue, expected inflow.

    Gives business owners an instant snapshot of *money movement* rather
    than abstract accounting metrics.
    """
    today = date.today()
    week_ago = today - timedelta(days=7)
    next_week = today + timedelta(days=7)
    start_of_today = datetime.combine(today, datetime.min.time())
    start_of_week = datetime.combine(week_ago, datetime.min.time())
    end_of_next_week = datetime.combine(next_week, datetime.max.time())

    base = [
        models.Invoice.issuer_id == user_id,
        models.Invoice.invoice_type == "revenue",
        exclude_abandoned_storefront(),
    ]

    # Cash collected this week
    cash_this_week = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(
            *base,
            models.Invoice.status == "paid",
            models.Invoice.paid_at >= start_of_week,
        )
        .scalar()
    )

    # Cash collected today
    cash_today = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(
            *base,
            models.Invoice.status == "paid",
            models.Invoice.paid_at >= start_of_today,
        )
        .scalar()
    )

    # Total outstanding (unpaid revenue invoices)
    outstanding = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(
            *base,
            models.Invoice.status.in_(["pending", "awaiting_confirmation"]),
        )
        .scalar()
    )

    # Overdue amount + count
    overdue_filters = [
        *base,
        models.Invoice.status == "pending",
        models.Invoice.due_date != None,  # noqa: E711
        models.Invoice.due_date < start_of_today,
    ]
    overdue_amount = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(*overdue_filters)
        .scalar()
    )
    overdue_count = (
        db.query(func.count(models.Invoice.id))
        .filter(*overdue_filters)
        .scalar()
    ) or 0

    # Expected inflow next 7 days
    expected_inflow = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(
            *base,
            models.Invoice.status.in_(["pending", "awaiting_confirmation"]),
            models.Invoice.due_date != None,  # noqa: E711
            models.Invoice.due_date >= start_of_today,
            models.Invoice.due_date <= end_of_next_week,
        )
        .scalar()
    )

    # Invoices created today
    invoices_today = (
        db.query(func.count(models.Invoice.id))
        .filter(*base, models.Invoice.created_at >= start_of_today)
        .scalar()
    ) or 0

    # Expenses today
    expenses_today = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "expense",
            models.Invoice.created_at >= start_of_today,
        )
        .scalar()
    )

    return {
        "cash_collected_today": float(cash_today),
        "cash_collected_this_week": float(cash_this_week),
        "total_outstanding": float(outstanding),
        "total_overdue": float(overdue_amount),
        "overdue_count": overdue_count,
        "expected_inflow_7_days": float(expected_inflow),
        "invoices_created_today": invoices_today,
        "expenses_today": float(expenses_today),
        "net_today": float(cash_today) - float(expenses_today),
    }


# ── Customer Insights ────────────────────────────────────────────────


def calculate_customer_insights(
    db: Session,
    user_id: int,
    limit: int = 20,
) -> dict:
    """Customer value, payment speed, dormancy insights.

    Segments customers into VIP / Active / New / At-Risk / Dormant so the
    business knows who to nurture and who to re-engage.
    """
    today = date.today()

    customer_stats = (
        db.query(
            models.Customer.id,
            models.Customer.name,
            models.Customer.phone,
            func.sum(models.Invoice.amount).label("total_spent"),
            func.count(models.Invoice.id).label("invoice_count"),
            func.max(models.Invoice.created_at).label("last_invoice_date"),
            func.sum(
                case((models.Invoice.status == "paid", 1), else_=0)
            ).label("paid_count"),
        )
        .join(models.Invoice, models.Invoice.customer_id == models.Customer.id)
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
        )
        .group_by(models.Customer.id, models.Customer.name, models.Customer.phone)
        .order_by(func.sum(models.Invoice.amount).desc())
        .limit(limit)
        .all()
    )

    customers = []
    for row in customer_stats:
        last_dt = row.last_invoice_date
        days_since_last = (today - last_dt.date()).days if last_dt else 999

        total = float(row.total_spent or 0)
        count = row.invoice_count or 0
        paid = row.paid_count or 0

        # Status segmentation
        if count == 1 and days_since_last < 30:
            status = "new"
        elif days_since_last > 90:
            status = "dormant"
        elif total >= 100_000 and count >= 3:
            status = "vip"
        elif days_since_last <= 30:
            status = "active"
        else:
            status = "at_risk"

        payment_rate = round(paid / count * 100, 1) if count else 0.0

        customers.append(
            {
                "id": row.id,
                "name": row.name,
                "phone": row.phone,
                "total_spent": total,
                "invoice_count": count,
                "paid_count": paid,
                "payment_rate": payment_rate,
                "last_purchase_days_ago": days_since_last,
                "status": status,
            }
        )

    status_counts: dict[str, int] = {}
    for c in customers:
        status_counts[c["status"]] = status_counts.get(c["status"], 0) + 1

    dormant = [c for c in customers if c["status"] in ("dormant", "at_risk")]

    return {
        "customers": customers,
        "summary": status_counts,
        "dormant_customers": dormant,
        "total_analyzed": len(customers),
    }


# ── Professionalism Score ────────────────────────────────────────────


def calculate_professionalism_score(db: Session, user_id: int) -> dict:
    """Score how professional the business looks to customers (0-100).

    Five checks, 20 points each:
    1. Has business name
    2. Has logo
    3. Has bank details (= payment instructions on invoices)
    4. Uses due dates on recent invoices
    5. Sends receipts on payment
    """
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if not user:
        return {"score": 0, "checks": {}, "tips": []}

    checks: dict[str, bool] = {}
    tips: list[str] = []

    # 1. Has business name (+20)
    has_name = bool(user.business_name and user.business_name.strip())
    checks["has_business_name"] = has_name
    if not has_name:
        tips.append("Set your business name — it appears on every invoice and receipt.")

    # 2. Has logo (+20)
    has_logo = bool(user.logo_url)
    checks["has_logo"] = has_logo
    if not has_logo:
        tips.append("Upload your business logo to look more credible on invoices.")

    # 3. Has bank details (+20)
    # Bank details ARE the payment instructions shown on invoices.
    has_bank = bool(user.bank_name and user.account_number)
    checks["has_bank_details"] = has_bank
    if not has_bank:
        tips.append("Add your bank details so customers know where to pay.")

    # 4. Uses due dates on recent invoices (+20)
    # Only look at the last 5 invoices created in the past 30 days so
    # old invoices (created before the user learned about due dates)
    # don't permanently drag down the score.
    thirty_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=30)
    recent_invoices = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= thirty_days_ago,
        )
        .order_by(models.Invoice.created_at.desc())
        .limit(5)
        .all()
    )
    if recent_invoices:
        with_due_date = sum(1 for inv in recent_invoices if inv.due_date)
        due_ratio = with_due_date / len(recent_invoices)
        checks["uses_due_dates"] = due_ratio >= 0.6
    else:
        # No recent invoices — don't penalise; pass by default
        checks["uses_due_dates"] = True
    if not checks["uses_due_dates"]:
        tips.append("Set due dates on your invoices — businesses that do collect 30% faster.")

    # 5. Sends receipts on payment (+20)
    paid_invoices = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.issuer_id == user_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "paid",
        )
        .order_by(models.Invoice.created_at.desc())
        .limit(10)
        .all()
    )
    if paid_invoices:
        with_receipt = sum(1 for inv in paid_invoices if inv.receipt_pdf_url)
        receipt_ratio = with_receipt / len(paid_invoices)
        checks["sends_receipts"] = receipt_ratio >= 0.7
    else:
        # No paid invoices yet — don't penalise
        checks["sends_receipts"] = True
    if not checks["sends_receipts"]:
        tips.append("Send receipts when customers pay — it builds trust and repeat business.")

    score = sum(20 for v in checks.values() if v)
    if score >= 80:
        level = "Excellent"
    elif score >= 60:
        level = "Good"
    elif score >= 40:
        level = "Fair"
    else:
        level = "Needs Work"

    return {"score": score, "checks": checks, "tips": tips, "level": level}


# ── Margin & Discount Insights ───────────────────────────────────────


def calculate_margin_insights(
    db: Session,
    user_id: int,
    start_date: date,
    end_date: date,
) -> dict:
    """Discount leakage and product margin analysis.

    Shows how much revenue is lost to discounts and which products have
    thin margins — so the business can price more profitably.
    """
    start_dt = datetime.combine(start_date, datetime.min.time())
    end_dt = datetime.combine(end_date, datetime.max.time())
    period_filter = [
        models.Invoice.issuer_id == user_id,
        models.Invoice.invoice_type == "revenue",
        models.Invoice.created_at >= start_dt,
        models.Invoice.created_at <= end_dt,
    ]

    # ── Discount leakage ──
    disc_row = (
        db.query(
            func.count(models.Invoice.id).label("disc_count"),
            func.coalesce(func.sum(models.Invoice.discount_amount), 0).label("total_discounts"),
        )
        .filter(*period_filter, models.Invoice.discount_amount > 0)
        .first()
    )

    total_rev = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(*period_filter)
        .scalar()
    )

    total_discounts = float(disc_row.total_discounts) if disc_row else 0.0
    total_revenue = float(total_rev) if total_rev else 0.0
    disc_count = disc_row.disc_count if disc_row else 0

    # Top discounted customers
    top_discounted = (
        db.query(
            models.Customer.name,
            func.count(models.Invoice.id).label("count"),
            func.sum(models.Invoice.discount_amount).label("total_discount"),
        )
        .join(models.Customer, models.Invoice.customer_id == models.Customer.id)
        .filter(*period_filter, models.Invoice.discount_amount > 0)
        .group_by(models.Customer.name)
        .order_by(func.sum(models.Invoice.discount_amount).desc())
        .limit(5)
        .all()
    )

    # ── Product margins (from inventory) ──
    product_margins: list[dict] = []
    try:
        from app.models.inventory_models import Product

        products = (
            db.query(Product)
            .filter(
                Product.user_id == user_id,
                Product.cost_price > 0,
                Product.selling_price > 0,
                Product.is_active.is_(True),
            )
            .all()
        )
        for p in products:
            margin = float(
                (p.selling_price - p.cost_price) / p.selling_price * 100
            )
            product_margins.append(
                {
                    "name": p.name,
                    "cost_price": float(p.cost_price),
                    "selling_price": float(p.selling_price),
                    "margin_percent": round(margin, 1),
                    "stock": p.quantity_in_stock,
                }
            )
        product_margins.sort(key=lambda x: x["margin_percent"])
    except Exception:
        pass  # Inventory module may not be in use

    return {
        "total_discounts": total_discounts,
        "discount_count": disc_count,
        "total_revenue": total_revenue,
        "discount_as_percent_of_revenue": (
            round(total_discounts / total_revenue * 100, 1) if total_revenue else 0.0
        ),
        "top_discounted_customers": [
            {
                "name": c.name,
                "count": c.count,
                "total_discount": float(c.total_discount),
            }
            for c in (top_discounted or [])
        ],
        "product_margins": product_margins[:10],
        "low_margin_count": sum(1 for p in product_margins if p["margin_percent"] < 20),
    }


# ── Business Snapshot ─────────────────────────────────────────────────
# An alternative-data activity summary assembled entirely from numbers
# SuoOps already computes elsewhere (aging, professionalism, monthly
# trends, tax profile). No ML, no external bureau, no new data source —
# this exists so a business's own SuoOps activity can be handed to a
# financial institution as one input alongside their own underwriting.


def calculate_business_snapshot(db: Session, user_id: int) -> dict:
    """Composite SME activity snapshot — NOT a credit score.

    Every component below is a plain, auditable calculation reusing
    existing analytics functions; the weights are explicit so a reader
    (a business owner or a bank reviewing it) can see exactly what feeds
    the number instead of a black-box score.
    """
    today = date.today()
    twelve_months_ago = today - timedelta(days=365)
    twelve_months_ago_dt = datetime.combine(twelve_months_ago, datetime.min.time())

    base_revenue_filter = [
        models.Invoice.issuer_id == user_id,
        models.Invoice.invoice_type == "revenue",
        exclude_abandoned_storefront(),
    ]

    # ── 1. Payment reliability (35%) ──────────────────────────────────
    # Share of everything billed in the last 12 months that's actually
    # paid, penalised for how much of it is sitting overdue 60+ days.
    counts_row = (
        db.query(
            func.count(models.Invoice.id).label("total"),
            func.sum(case((models.Invoice.status == "paid", 1), else_=0)).label("paid"),
        )
        .filter(*base_revenue_filter, models.Invoice.created_at >= twelve_months_ago_dt)
        .first()
    )
    total_count = counts_row.total or 0
    paid_count = counts_row.paid or 0
    # No billing history yet — neutral, not penalised (nothing to judge).
    paid_ratio = (paid_count / total_count * 100) if total_count else 100.0

    aging = calculate_aging_report(db, user_id, today, Decimal("1"))
    total_billed = (
        db.query(func.coalesce(func.sum(models.Invoice.amount), 0))
        .filter(*base_revenue_filter, models.Invoice.created_at >= twelve_months_ago_dt)
        .scalar()
    ) or 0
    overdue_ratio = (
        float(aging.over_90_days + aging.days_61_90) / float(total_billed) * 100
        if total_billed
        else 0.0
    )
    payment_reliability_score = max(0.0, min(100.0, paid_ratio - overdue_ratio * 0.5))

    # ── 2. Revenue consistency (20%) ──────────────────────────────────
    # Months, out of the last 6, with any paid revenue — rewards steady
    # trading over one lucky month. Reuses the existing monthly-trends
    # calculation rather than a new query.
    monthly_trends = calculate_monthly_trends(db, user_id, today, Decimal("1"))
    last_six_months = monthly_trends[-6:]
    months_with_revenue = sum(1 for m in last_six_months if m.revenue > 0)
    revenue_consistency_score = min(100.0, months_with_revenue / 6 * 100)

    # ── 3. Professionalism (15%) — reuse the existing 0-100 score ─────
    professionalism = calculate_professionalism_score(db, user_id)

    # ── 4. Tax / VAT compliance signal (15%) ──────────────────────────
    from app.models.tax_models import MonthlyTaxReport, TaxProfile

    tax_profile = db.query(TaxProfile).filter(TaxProfile.user_id == user_id).first()
    has_tax_report = (
        db.query(MonthlyTaxReport.id).filter(MonthlyTaxReport.user_id == user_id).first()
        is not None
    )
    vat_registered = bool(tax_profile.vat_registered) if tax_profile else False
    # VAT registration isn't required below Nigeria's ₦25M threshold, so
    # being unregistered is NOT penalised — only evidence of active
    # tracking (a generated tax report, or VAT registration) is rewarded;
    # everyone else gets a neutral baseline rather than a penalty.
    tax_compliance_score = 100.0 if (vat_registered or has_tax_report) else 50.0

    # ── 5. Activity depth (15%) ───────────────────────────────────────
    # More recorded transactions -> more confidence in every other number
    # above. Caps at 100 around ~5 transactions/month over 6 months.
    six_months_ago_dt = datetime.combine(today - timedelta(days=180), datetime.min.time())
    activity_count = (
        db.query(func.count(models.Invoice.id))
        .filter(*base_revenue_filter, models.Invoice.created_at >= six_months_ago_dt)
        .scalar()
    ) or 0
    activity_depth_score = min(100.0, activity_count / 30 * 100)

    weights = {
        "payment_reliability": 0.35,
        "revenue_consistency": 0.20,
        "professionalism": 0.15,
        "tax_compliance": 0.15,
        "activity_depth": 0.15,
    }
    components = {
        "payment_reliability": round(payment_reliability_score, 1),
        "revenue_consistency": round(revenue_consistency_score, 1),
        "professionalism": round(float(professionalism["score"]), 1),
        "tax_compliance": round(tax_compliance_score, 1),
        "activity_depth": round(activity_depth_score, 1),
    }
    composite = round(sum(components[k] * w for k, w in weights.items()), 1)

    if composite >= 80:
        level = "Excellent"
    elif composite >= 60:
        level = "Good"
    elif composite >= 40:
        level = "Fair"
    else:
        level = "Early stage"

    # ── Activity mix: billed-to-a-customer vs walk-in sales, for context ──
    quick_sale_row = (
        db.query(
            func.count(models.Invoice.id),
            func.coalesce(func.sum(models.Invoice.amount), 0),
        )
        .filter(
            *base_revenue_filter,
            models.Invoice.channel == "quick_sale",
            models.Invoice.created_at >= twelve_months_ago_dt,
        )
        .first()
    )
    billed_row = (
        db.query(
            func.count(models.Invoice.id),
            func.coalesce(func.sum(models.Invoice.amount), 0),
        )
        .filter(
            *base_revenue_filter,
            or_(models.Invoice.channel != "quick_sale", models.Invoice.channel.is_(None)),
            models.Invoice.created_at >= twelve_months_ago_dt,
        )
        .first()
    )

    # ── Data provenance: gateway-confirmed vs self-reported paid amounts ──
    # A payment is "gateway confirmed" when a webhook (Paystack/Flutterwave)
    # flipped it to paid with no human clicking "mark paid"
    # (status_updated_by_user_id is unset), or it came through the
    # storefront/escrow checkout. Everything else — including quick sales —
    # is the business itself reporting that it got paid (cash in hand).
    # Both are useful signals to a bank; they just carry different trust
    # levels, so they're kept separate rather than blended into one figure.
    provenance_row = (
        db.query(
            func.coalesce(
                func.sum(
                    case(
                        (
                            or_(
                                models.Invoice.channel == "storefront",
                                models.Invoice.status_updated_by_user_id.is_(None),
                            ),
                            models.Invoice.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("gateway_confirmed"),
            func.coalesce(
                func.sum(
                    case(
                        (
                            and_(
                                or_(
                                    models.Invoice.channel != "storefront",
                                    models.Invoice.channel.is_(None),
                                ),
                                models.Invoice.status_updated_by_user_id.isnot(None),
                            ),
                            models.Invoice.amount,
                        ),
                        else_=0,
                    )
                ),
                0,
            ).label("self_reported"),
        )
        .filter(
            *base_revenue_filter,
            models.Invoice.status == "paid",
            models.Invoice.created_at >= twelve_months_ago_dt,
        )
        .first()
    )

    return {
        "generated_at": datetime.now(timezone.utc),
        "period_months": 12,
        "composite_score": composite,
        "level": level,
        "components": components,
        "component_weights": weights,
        "payment_reliability": {
            "paid_ratio": round(paid_ratio, 1),
            "overdue_ratio": round(overdue_ratio, 1),
            "aging": aging,
        },
        "revenue_consistency": {
            "months_with_revenue": months_with_revenue,
            "months_checked": 6,
        },
        "professionalism_score": professionalism["score"],
        "tax_compliance": {
            "vat_registered": vat_registered,
            "has_generated_tax_report": has_tax_report,
            "business_size": tax_profile.business_size if tax_profile else None,
        },
        "activity_mix": {
            "billed_invoice_count": billed_row[0] or 0,
            "billed_invoice_amount": float(billed_row[1] or 0),
            "walk_in_sale_count": quick_sale_row[0] or 0,
            "walk_in_sale_amount": float(quick_sale_row[1] or 0),
        },
        "data_provenance": {
            "gateway_confirmed_amount": float(provenance_row.gateway_confirmed or 0),
            "self_reported_amount": float(provenance_row.self_reported or 0),
        },
        "disclaimer": (
            "This is an alternative-data activity snapshot generated from a "
            "business's own SuoOps records. It is NOT a credit score and does "
            "not assess default risk — it is intended as one input alongside "
            "a financial institution's own underwriting and cross-bank data."
        ),
    }
