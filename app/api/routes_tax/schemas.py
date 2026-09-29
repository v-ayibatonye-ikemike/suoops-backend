"""
Shared Pydantic schemas for tax-related routes.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class TaxProfileUpdate(BaseModel):
    """Tax profile update request."""

    annual_turnover: Decimal | None = Field(None, ge=0, description="Annual turnover in Naira")
    fixed_assets: Decimal | None = Field(None, ge=0, description="Fixed assets value in Naira")
    tin: str | None = Field(None, max_length=20, description="Tax Identification Number")
    vat_registration_number: str | None = Field(None, max_length=20, description="VAT registration number")
    vat_registered: bool | None = Field(None, description="VAT registration status")
    business_type: str | None = Field(
        None,
        description="Business type: goods, services, or mixed",
        pattern="^(goods|services|mixed)$",
    )
    vat_apply_to: str | None = Field(
        None,
        description="Apply VAT to: all or selected invoices",
        pattern="^(all|selected)$",
    )
    withholding_vat_applies: bool | None = Field(
        None,
        description="Whether customers sometimes withhold VAT",
    )


class FiscalizationStatus(BaseModel):
    """Accreditation / fiscalization readiness status response."""

    accredited: bool
    generated_count: int
    pending_external_count: int
    timestamp: str


class DevelopmentLevyResponse(BaseModel):
    """Development levy computation response."""

    user_id: int
    business_size: str
    is_small_business: bool
    assessable_profit: float
    levy_rate_percent: float
    levy_applicable: bool
    levy_amount: float
    exemption_reason: str | None
    period: str | None = None
    source: str | None = None


class AlertEventOut(BaseModel):
    """Alert event output schema."""

    id: int
    category: str
    severity: str
    message: str
    created_at: str | None

    model_config = {"from_attributes": True}


# ── Tax profile responses ─────────────────────────────────────────────


class SmallBusinessThreshold(BaseModel):
    turnover: float
    assets: float


class TaxClassification(BaseModel):
    annual_turnover: float
    fixed_assets: float
    small_business_threshold: SmallBusinessThreshold
    meets_small_criteria: bool


class TaxRegistration(BaseModel):
    tin: str | None = None
    vat_registered: bool
    vat_number: str | None = None
    firs_registered: bool
    firs_merchant_id: str | None = None
    business_type: str = "mixed"
    vat_apply_to: str = "all"
    withholding_vat_applies: bool = False


class TaxBenefits(BaseModel):
    company_income_tax: str
    capital_gains_tax: str | None = None
    development_levy: str
    vat: str
    annual_savings: str | None = None
    note: str | None = None


class TaxSummaryOut(BaseModel):
    """GET /tax/profile — excludes internal IDs."""

    user_id: int
    business_size: str
    is_small_business: bool
    classification: TaxClassification
    tax_rates: dict[str, object]
    registration: TaxRegistration
    tax_benefits: TaxBenefits


class TaxProfileUpdateOut(BaseModel):
    """POST /tax/profile response."""

    message: str
    summary: TaxSummaryOut


class SmallBusinessCheckOut(BaseModel):
    """GET /tax/small-business-check response."""

    eligible: bool
    business_size: str
    current_turnover: float
    turnover_limit: float
    turnover_remaining: float
    current_assets: float
    assets_limit: float
    assets_remaining: float
    tax_rates: dict[str, object]
    benefits: list[str]
    approaching_limit: bool


class ComplianceRequirements(BaseModel):
    tin_registered: bool
    vat_registered: bool
    firs_registered: bool


class ComplianceSummaryOut(BaseModel):
    """GET /tax/compliance response."""

    compliance_status: str
    compliance_score: float
    requirements: ComplianceRequirements
    next_actions: list[str]
    business_size: str
    small_business_benefits: bool
    last_check: str | None = None


class TaxConfigOut(BaseModel):
    """GET /tax/config response."""

    small_business_turnover_limit: float
    small_business_assets_limit: float
    vat_rate: float
    cit_rate: float
    min_tax_rate: float
    development_levy_rate: float
    pit_bands: list[dict[str, object]]


# ── Tax report responses ──────────────────────────────────────────────


class TaxAlertItem(BaseModel):
    type: str
    severity: str
    message: str


class TaxReportOut(BaseModel):
    """POST /tax/reports/generate — excludes debug_info."""

    id: int
    period_type: str
    period_label: str
    start_date: str | None = None
    end_date: str | None = None
    year: int | None = None
    month: int | None = None
    total_revenue: float
    total_expenses: float
    documented_expenses: float
    self_reported_expenses: float
    flagged_expenses: float
    cogs_amount: float
    assessable_profit: float
    levy_amount: float
    pit_amount: float
    cit_amount: float
    vat_collected: float
    taxable_sales: float
    zero_rated_sales: float
    exempt_sales: float
    pdf_url: str | None = None
    basis: str
    user_plan: str
    is_vat_eligible: bool
    is_cit_eligible: bool
    pit_band_info: str
    alerts: list[TaxAlertItem]
    annual_revenue_estimate: float


class ReportDownloadOut(BaseModel):
    """GET /tax/reports/{id}/download response."""

    pdf_url: str
    period_type: str
    start_date: str | None = None
    end_date: str | None = None


class ReportCsvOut(BaseModel):
    """GET /tax/reports/{id}/csv response."""

    csv_url: str
    basis: str


# ── VAT responses ─────────────────────────────────────────────────────


class VATSummaryOut(BaseModel):
    """GET /tax/vat/summary response."""

    current_month: dict[str, object] | None = None
    recent_returns: list[dict[str, object]] | None = None
    compliance_status: str | None = None
    next_action: str | None = None
    vat_registered: bool | None = None
    total_vat_collected: float | None = None
    total_vat_paid: float | None = None


class VATCalculateOut(BaseModel):
    """GET /tax/vat/calculate response."""

    subtotal: float
    vat_rate: float
    vat_amount: float
    total: float
    category: str


class VATReturnOut(BaseModel):
    """POST /tax/vat/return response."""

    tax_period: str
    output_vat: float
    input_vat: float
    net_vat: float
    zero_rated_sales: float
    exempt_sales: float
    total_invoices: int
    fiscalized_invoices: int
    status: str
    message: str


# ── Fiscalization responses ───────────────────────────────────────────


class VATBreakdown(BaseModel):
    subtotal: float
    vat_rate: float
    vat_amount: float
    total: float


class FiscalizeInvoiceOut(BaseModel):
    """POST /tax/invoice/{id}/fiscalize — excludes fiscal_signature, firs_transaction_id."""

    message: str
    fiscal_code: str
    vat_breakdown: VATBreakdown
    fiscalization_status: str
    status: str | None = None
