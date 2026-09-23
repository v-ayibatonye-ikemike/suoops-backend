"""Analytics dashboard endpoints for business metrics and insights."""

from __future__ import annotations

import logging
from datetime import datetime
from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.api.dependencies import get_data_owner_id
from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import models
from app.utils.feature_gate import require_plan_feature
from app.models.schemas import AnalyticsDashboard, BusinessSnapshotOut
from app.services.analytics_service import (
    calculate_aging_report,
    calculate_business_snapshot,
    calculate_cash_position,
    calculate_customer_insights,
    calculate_customer_metrics,
    calculate_invoice_metrics,
    calculate_margin_insights,
    calculate_monthly_trends,
    calculate_professionalism_score,
    calculate_revenue_metrics,
    calculate_storefront_insights,
    exclude_abandoned_storefront,
    get_conversion_rate,
    get_date_range,
)

from pydantic import BaseModel

router = APIRouter()
logger = logging.getLogger(__name__)

# Type aliases for dependency injection
CurrentUserDep = Annotated[int, Depends(get_current_user_id)]
DataOwnerDep = Annotated[int, Depends(get_data_owner_id)]
DbDep = Annotated[Session, Depends(get_db)]


# ── Analytics response schemas ─────────────────────────────────────────

class CustomerRevenueItem(BaseModel):
    name: str
    total_revenue: float
    invoice_count: int


class RevenueByCustomerOut(BaseModel):
    period: str
    customers: list[CustomerRevenueItem]


class ConversionFunnel(BaseModel):
    created: int
    sent: int
    viewed: int
    awaiting_confirmation: int
    paid: int
    cancelled: int


class ConversionRates(BaseModel):
    sent_to_viewed: float
    viewed_to_paid: float
    overall: float


class ConversionFunnelOut(BaseModel):
    period: str
    funnel: ConversionFunnel
    conversion_rates: ConversionRates


class ExchangeRateOut(BaseModel):
    rate: float
    currency_pair: str
    description: str


@router.get("/exchange-rate", response_model=ExchangeRateOut)
def get_exchange_rate(
    current_user_id: CurrentUserDep,
):
    """Get the current NGN/USD exchange rate used for conversions."""
    from app.services.exchange_rate import get_exchange_rate_info

    return get_exchange_rate_info()


@router.post("/exchange-rate/refresh", response_model=ExchangeRateOut)
def refresh_exchange_rate(
    current_user_id: CurrentUserDep,
):
    """Force-refresh the exchange rate (busts the server cache)."""
    from app.services.exchange_rate import force_refresh_rate, get_exchange_rate_info

    force_refresh_rate()
    return get_exchange_rate_info()


@router.get("/dashboard", response_model=AnalyticsDashboard)
def get_analytics_dashboard(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period: str = Query("30d", pattern="^(7d|30d|90d|1y|all)$"),
    currency: str = Query("NGN", pattern="^(NGN|USD)$"),
) -> AnalyticsDashboard:
    """
    Get comprehensive analytics dashboard with revenue, invoices, customers, and aging.
    
    Requires a paid plan (Pro or higher).
    """
    require_plan_feature(db, current_user_id, "cash_dashboard", "Analytics Dashboard")

    # Calculate date range and conversion rate
    start_date, end_date = get_date_range(period)
    conversion_rate = get_conversion_rate(currency)
    
    # Calculate all metrics using data_owner_id for team context
    revenue_metrics = calculate_revenue_metrics(
        db, data_owner_id, start_date, end_date, conversion_rate
    )
    
    invoice_metrics = calculate_invoice_metrics(
        db, data_owner_id, start_date, end_date
    )
    
    customer_metrics = calculate_customer_metrics(
        db, data_owner_id, start_date, end_date
    )
    
    aging_report = calculate_aging_report(
        db, data_owner_id, end_date, conversion_rate
    )
    
    monthly_trends = calculate_monthly_trends(
        db, data_owner_id, end_date, conversion_rate
    )
    
    return AnalyticsDashboard(
        period=period,
        currency=currency,
        start_date=start_date,
        end_date=end_date,
        revenue=revenue_metrics,
        invoices=invoice_metrics,
        customers=customer_metrics,
        aging=aging_report,
        monthly_trends=monthly_trends,
    )


@router.get("/revenue-by-customer", response_model=RevenueByCustomerOut)
def get_revenue_by_customer(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period: str = Query("30d", pattern="^(7d|30d|90d|1y|all)$"),
    limit: int = Query(10, ge=1, le=100),
    currency: str = Query("NGN", pattern="^(NGN|USD)$"),
):
    """Get top customers by revenue (team data for team members)."""
    require_plan_feature(db, current_user_id, "cash_dashboard", "Revenue by Customer")

    start_date, _ = get_date_range(period)
    conversion_rate = get_conversion_rate(currency)
    
    # Query top customers using data_owner_id
    top_customers = (
        db.query(
            models.Customer.name,
            func.sum(models.Invoice.amount).label("total_revenue"),
            func.count(models.Invoice.id).label("invoice_count"),
        )
        .join(models.Invoice, models.Invoice.customer_id == models.Customer.id)
        .filter(
            models.Invoice.issuer_id == data_owner_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "paid",
            models.Invoice.created_at >= datetime.combine(start_date, datetime.min.time()),
        )
        .group_by(models.Customer.id, models.Customer.name)
        .order_by(func.sum(models.Invoice.amount).desc())
        .limit(limit)
        .all()
    )
    
    return {
        "period": period,
        "customers": [
            {
                "name": customer.name,
                "total_revenue": float(
                    Decimal(str(customer.total_revenue)) / conversion_rate
                ),
                "invoice_count": customer.invoice_count,
            }
            for customer in top_customers
        ],
    }


@router.get("/conversion-funnel", response_model=ConversionFunnelOut)
def get_conversion_funnel(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period: str = Query("30d", pattern="^(7d|30d|90d|1y|all)$"),
):
    """Get invoice conversion funnel (created → paid). Returns team data for team members."""
    require_plan_feature(db, current_user_id, "cash_dashboard", "Conversion Funnel")

    start_date, _ = get_date_range(period)
    
    # Count invoices by status using data_owner_id
    stats = (
        db.query(
            func.count(models.Invoice.id).label("total"),
            func.sum(case((models.Invoice.status == "paid", 1), else_=0)).label("paid"),
            func.sum(case((models.Invoice.status == "awaiting_confirmation", 1), else_=0)).label("awaiting"),
            func.sum(case((models.Invoice.status == "pending", 1), else_=0)).label("pending"),
            func.sum(case((models.Invoice.status == "cancelled", 1), else_=0)).label("cancelled"),
        )
        .filter(
            models.Invoice.issuer_id == data_owner_id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= datetime.combine(start_date, datetime.min.time()),
            # Exclude abandoned/unpaid storefront orders from the funnel.
            exclude_abandoned_storefront(),
        )
        .first()
    )
    
    total = stats.total or 0
    paid = stats.paid or 0
    awaiting = stats.awaiting or 0
    cancelled = stats.cancelled or 0
    
    return {
        "period": period,
        "funnel": {
            "created": total,
            "sent": total,  # All created invoices are "sent"
            "viewed": awaiting + paid,  # Assuming viewed if status changed
            "awaiting_confirmation": awaiting,
            "paid": paid,
            "cancelled": cancelled,
        },
        "conversion_rates": {
            "sent_to_viewed": ((awaiting + paid) / total * 100) if total > 0 else 0,
            "viewed_to_paid": (paid / (awaiting + paid) * 100) if (awaiting + paid) > 0 else 0,
            "overall": (paid / total * 100) if total > 0 else 0,
        },
    }


# ── Cash-First Dashboard ─────────────────────────────────────────────


class CashPositionOut(BaseModel):
    cash_collected_today: float
    cash_collected_this_week: float
    total_outstanding: float
    total_overdue: float
    overdue_count: int
    expected_inflow_7_days: float
    invoices_created_today: int
    expenses_today: float
    net_today: float


@router.get("/cash-position", response_model=CashPositionOut)
def get_cash_position(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Cash-first business snapshot.

    Shows real money movement: what came in, what's owed, what's overdue,
    and what to expect this week — the numbers that matter most to a
    Nigerian small business owner.
    """
    require_plan_feature(db, current_user_id, "cash_dashboard", "Cash Dashboard")
    return calculate_cash_position(db, data_owner_id)


# ── Customer Insights ────────────────────────────────────────────────


class CustomerInsightItem(BaseModel):
    id: int
    name: str
    phone: str | None = None
    total_spent: float
    invoice_count: int
    paid_count: int
    payment_rate: float
    last_purchase_days_ago: int
    status: str  # vip | active | new | at_risk | dormant


class CustomerInsightsOut(BaseModel):
    customers: list[CustomerInsightItem]
    summary: dict[str, int]
    dormant_customers: list[CustomerInsightItem]
    total_analyzed: int


@router.get("/customer-insights", response_model=CustomerInsightsOut)
def get_customer_insights(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    limit: int = Query(20, ge=1, le=100),
):
    """Customer value, payment behaviour, and dormancy insights.

    Segments customers into VIP / Active / New / At-Risk / Dormant so the
    business knows who to nurture and who to re-engage.
    """
    require_plan_feature(db, current_user_id, "customer_insights", "Customer Insights")
    return calculate_customer_insights(db, data_owner_id, limit)


# ── Professionalism Score ────────────────────────────────────────────


class ProfessionalismScoreOut(BaseModel):
    score: int  # 0-100
    checks: dict[str, bool]
    tips: list[str]
    level: str  # Excellent | Good | Fair | Needs Work


@router.get("/professionalism-score", response_model=ProfessionalismScoreOut)
def get_professionalism_score(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Score how professional the business looks (0-100).

    Five checks, 20 points each: logo, bank details, due dates, receipts,
    payment instructions. Only visible to the business — not to customers.
    """
    require_plan_feature(db, current_user_id, "professionalism_score", "Professionalism Score")
    return calculate_professionalism_score(db, data_owner_id)


# ── Business Snapshot ─────────────────────────────────────────────────


@router.get("/business-snapshot", response_model=BusinessSnapshotOut)
def get_business_snapshot(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Composite SME activity snapshot for sharing with a financial
    institution — payment reliability, revenue consistency,
    professionalism, tax/VAT compliance signal, and activity depth,
    assembled from data SuoOps already has.

    NOT a credit score (see the `disclaimer` field) — an alternative-data
    input intended to sit alongside a bank's own underwriting and
    cross-bank data, not replace it.
    """
    require_plan_feature(db, current_user_id, "cash_dashboard", "Business Snapshot")
    return calculate_business_snapshot(db, data_owner_id)


# ── Margin & Discount Insights ───────────────────────────────────────


class DiscountedCustomer(BaseModel):
    name: str
    count: int
    total_discount: float


class ProductMargin(BaseModel):
    name: str
    cost_price: float
    selling_price: float
    margin_percent: float
    stock: int


class MarginInsightsOut(BaseModel):
    total_discounts: float
    discount_count: int
    total_revenue: float
    discount_as_percent_of_revenue: float
    top_discounted_customers: list[DiscountedCustomer]
    product_margins: list[ProductMargin]
    low_margin_count: int


@router.get("/margin-insights", response_model=MarginInsightsOut)
def get_margin_insights(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period: str = Query("30d", pattern="^(7d|30d|90d|1y|all)$"),
):
    """Discount leakage and product margin analysis.

    Shows total discounts given, who gets the most, and which products have
    thin margins — so the business can price more profitably.
    """
    require_plan_feature(db, current_user_id, "margin_insights", "Margin & Discount Insights")
    start_date, end_date = get_date_range(period)
    return calculate_margin_insights(db, data_owner_id, start_date, end_date)


# ── Storefront Insights ──────────────────────────────────────────────


class StorefrontTopProduct(BaseModel):
    name: str
    units: int
    revenue: float


class StorefrontInsightsOut(BaseModel):
    enabled: bool
    slug: str | None = None
    store_url: str | None = None
    # Store-lifetime counters
    views: int = 0
    reviews: int = 0
    avg_rating: float | None = None
    conversion_rate: float = 0.0  # lifetime paid orders / views (%)
    # Period-scoped order metrics
    period: str
    orders: int = 0
    paid_orders: int = 0
    abandoned_orders: int = 0
    gmv: float = 0.0
    avg_order_value: float = 0.0
    awaiting_release: float = 0.0
    refunds: int = 0
    disputes: int = 0
    restock_requests: int = 0
    top_products: list[StorefrontTopProduct] = []
    top_products_total: int = 0


@router.get("/storefront-insights", response_model=StorefrontInsightsOut)
def get_storefront_insights(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    period: str = Query("30d", pattern="^(7d|30d|90d|1y|all)$"),
    currency: str = Query("NGN", pattern="^(NGN|USD)$"),
    top_limit: int = Query(5, ge=1, le=100),
):
    """Storefront performance: views, orders, GMV, ratings, top products, demand.

    Store-lifetime counters (views, rating, conversion) plus period-scoped order
    metrics (orders, GMV of goods, avg order value, refunds/disputes, top
    products, restock demand). Returns ``enabled=false`` when the business has no
    storefront so the UI can prompt setup.
    """
    require_plan_feature(db, current_user_id, "cash_dashboard", "Storefront Insights")
    start_date, end_date = get_date_range(period)
    conversion_rate = get_conversion_rate(currency)
    result = calculate_storefront_insights(
        db, data_owner_id, start_date, end_date, conversion_rate, top_limit=top_limit
    )
    result["period"] = period
    return result
