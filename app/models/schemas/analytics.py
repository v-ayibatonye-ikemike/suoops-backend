"""Analytics-related schemas."""
from __future__ import annotations

import datetime as dt

from pydantic import BaseModel


class RevenueMetrics(BaseModel):
    """Revenue breakdown and growth metrics."""
    total_revenue: float
    paid_revenue: float
    pending_revenue: float
    overdue_revenue: float
    growth_rate: float  # Percentage change from previous period
    average_invoice_value: float


class InvoiceMetrics(BaseModel):
    """Invoice counts and conversion metrics."""
    total_invoices: int
    paid_invoices: int
    pending_invoices: int
    failed_invoices: int
    awaiting_confirmation: int
    cancelled_invoices: int
    conversion_rate: float  # Percentage of paid invoices


class CustomerMetrics(BaseModel):
    """Customer engagement metrics."""
    total_customers: int
    active_customers: int  # Customers with invoices in period
    new_customers: int
    repeat_customer_rate: float  # Percentage with multiple invoices


class AgingReport(BaseModel):
    """Accounts receivable aging buckets."""
    current: float  # 0-30 days
    days_31_60: float
    days_61_90: float
    over_90_days: float
    total_outstanding: float


class MonthlyTrend(BaseModel):
    """Monthly revenue, expenses, and profit trend."""
    month: str  # "Jan 2025"
    revenue: float
    expenses: float
    profit: float
    invoice_count: int


class AnalyticsDashboard(BaseModel):
    """Complete analytics dashboard data."""
    period: str
    currency: str
    start_date: dt.date
    end_date: dt.date
    revenue: RevenueMetrics
    invoices: InvoiceMetrics
    customers: CustomerMetrics
    aging: AgingReport
    monthly_trends: list[MonthlyTrend]


class PaymentReliabilityOut(BaseModel):
    """How much of what's been billed (last 12 months) actually got paid."""
    paid_ratio: float  # % of revenue invoices that are paid
    overdue_ratio: float  # % of billed amount stuck 60+ days overdue
    aging: AgingReport


class RevenueConsistencyOut(BaseModel):
    """Trading steadiness over the last 6 months."""
    months_with_revenue: int
    months_checked: int


class TaxComplianceOut(BaseModel):
    """Tax/VAT tracking signal — not registration status alone."""
    vat_registered: bool
    has_generated_tax_report: bool
    business_size: str | None = None
    # Independently confirmed via Mono Lookup — a stronger signal than the
    # self-declared fields above, since it's verified against an external
    # registry (FIRS for TIN, CAC for company registration) rather than
    # taken at the business's word.
    tin_verified: bool = False
    cac_verified: bool = False
    cac_registered_name: str | None = None


class ActivityMixOut(BaseModel):
    """Billed-to-a-customer sales vs walk-in (Quick Sale) sales."""
    billed_invoice_count: int
    billed_invoice_amount: float
    walk_in_sale_count: int
    walk_in_sale_amount: float


class DataProvenanceOut(BaseModel):
    """Gateway-confirmed (Paystack/Flutterwave/storefront) vs self-reported
    (business marked it paid itself, e.g. cash) paid amounts — different
    trust levels, kept separate rather than blended into one figure."""
    gateway_confirmed_amount: float
    self_reported_amount: float


class BusinessSnapshotOut(BaseModel):
    """Composite SME activity snapshot assembled from existing SuoOps data.

    NOT a credit score — see `disclaimer`. Intended as one alternative-data
    input a business can share with a financial institution alongside the
    institution's own underwriting and cross-bank data.
    """
    generated_at: dt.datetime
    period_months: int
    composite_score: float
    level: str
    components: dict[str, float]
    component_weights: dict[str, float]
    payment_reliability: PaymentReliabilityOut
    revenue_consistency: RevenueConsistencyOut
    professionalism_score: float
    tax_compliance: TaxComplianceOut
    activity_mix: ActivityMixOut
    data_provenance: DataProvenanceOut
    disclaimer: str
