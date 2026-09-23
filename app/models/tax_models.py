"""
Tax and Fiscalization Models (Pre-Fiscalization Integration).

Models for:
- Business tax profiles and classification
- VAT tracking and returns
- Invoice fiscalization data

Note: Field names formerly referencing NRS have been updated to FIRS terminology to
reflect Federal Inland Revenue Service. External API integration is pending; current
fields store provisional registration metadata only.
"""
from datetime import datetime, timezone
from enum import Enum
from typing import Dict

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Numeric,
    String,
)
from sqlalchemy.orm import relationship

from app.db.base_class import Base


class BusinessSize(str, Enum):
    """Business size classification based on Nigeria Tax Act 2025 (NTA 2025) effective Jan 1, 2026"""
    SMALL = "small"      # Turnover ≤ ₦100M, Assets ≤ ₦250M (CIT exempt - 0%)
    MEDIUM = "medium"    # Turnover ₦100M-₦250M (CIT 20%)
    LARGE = "large"      # Turnover > ₦250M (CIT 30%)


class VATCategory(str, Enum):
    """VAT categories for goods and services (current Nigeria VAT rules; future FIRS enhancements pending)"""
    STANDARD = "standard"        # 7.5% VAT
    ZERO_RATED = "zero_rated"   # 0% VAT (medical, education, basic food)
    EXEMPT = "exempt"            # No VAT (financial services)
    EXPORT = "export"            # 0% for exports


class TaxProfile(Base):
    """
    Business tax profile and compliance tracking.
    
    Tracks:
    - Business classification (small/medium/large)
    - Tax registration numbers (TIN, VAT)
    - Provisional FIRS fiscalization placeholders
    - Compliance status
    """
    __tablename__ = "tax_profiles"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), unique=True, nullable=False)
    
    # Business classification (auto-calculated)
    business_size = Column(String(20), default=BusinessSize.SMALL)
    annual_turnover = Column(Numeric(15, 2), default=0)
    fixed_assets = Column(Numeric(15, 2), default=0)
    
    # Tax registration details
    tin = Column(String(20), nullable=True, index=True)
    tin_verified = Column(Boolean, default=False)
    vat_verified = Column(Boolean, default=False)
    verification_status = Column(String(20), default="pending")  # pending, verified, failed
    verification_attempts = Column(Integer, default=0)
    last_verification_at = Column(DateTime, nullable=True)
    vat_registered = Column(Boolean, default=False)
    vat_registration_number = Column(String(20), nullable=True)

    # CAC (Corporate Affairs Commission) registration — verified via Mono
    # Lookup. Distinct from tin_verified: this confirms the business is a
    # formally registered legal entity, which is itself a meaningful signal
    # even for businesses too small to be VAT-registered.
    rc_number = Column(String(20), nullable=True, index=True)
    cac_verified = Column(Boolean, default=False)
    cac_registered_name = Column(String(200), nullable=True)  # name Mono returned
    cac_verified_at = Column(DateTime, nullable=True)

    # Business type (for reporting clarity only, not enforcement)
    business_type = Column(String(20), default="mixed")  # goods, services, mixed
    # VAT application rules
    vat_apply_to = Column(String(20), default="all")  # all, selected
    withholding_vat_applies = Column(Boolean, default=False)  # banks, telcos, govt withhold VAT
    
    # Compliance tracking
    last_vat_return = Column(DateTime, nullable=True)
    last_compliance_check = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Fiscalization / FIRS integration (pending external API availability)
    firs_registered = Column(Boolean, default=False)
    firs_merchant_id = Column(String(50), nullable=True)
    firs_api_key = Column(String(100), nullable=True)
    
    # Timestamps
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    
    # Relationships
    user = relationship("User", back_populates="tax_profile")
    
    @property
    def is_small_business(self) -> bool:
        """Check if qualifies as small business per NTA 2025 (≤ ₦100M turnover and ≤ ₦250M assets)"""
        return (
            float(self.annual_turnover or 0) <= 100_000_000 and 
            float(self.fixed_assets or 0) <= 250_000_000
        )
    
    @property
    def tax_rates(self) -> Dict[str, float]:
        """Get applicable tax rates based on business size (NTA 2025 rates)"""
        from app.core.config import settings
        vat_rate = settings.VAT_RATE
        if self.is_small_business:
            return {
                "CIT": 0,           # Company Income Tax - EXEMPT (≤₦100M)
                "CGT": 0,           # Capital Gains Tax - EXEMPT
                "DEV_LEVY": 0,      # Development Levy - EXEMPT
                "VAT": vat_rate     # VAT still applicable (exemption at ₦25M separately)
            }
        else:
            return {
                "CIT": 20,          # Company Income Tax (medium: 20%, large: 30%)
                "CGT": 30,          # Capital Gains Tax
                "DEV_LEVY": 4,      # 4% Development Levy
                "VAT": vat_rate     # Standard VAT rate
            }

    def mark_verified(self, tin: bool = False, vat: bool = False, cac: bool = False) -> None:
        """Helper to update verified flags consistently."""
        if tin:
            self.tin_verified = True
        if vat:
            self.vat_verified = True
        if cac:
            self.cac_verified = True
            self.cac_verified_at = datetime.now(timezone.utc)
        if self.tin_verified or self.vat_verified or self.cac_verified:
            self.verification_status = "verified"


class FiscalInvoice(Base):
    """
    Fiscalized invoice data (provisional FIRS compliance readiness).
    
    Stores:
    - Fiscal codes and signatures
    - QR codes for validation
    - VAT breakdown
    - External transmission metadata (pending accreditation)
    """
    __tablename__ = "fiscal_invoices"
    
    id = Column(Integer, primary_key=True, index=True)
    invoice_id = Column(Integer, ForeignKey("invoice.id"), unique=True, nullable=False)
    
    # Fiscal identifiers
    fiscal_code = Column(String(100), unique=True, nullable=False, index=True)
    fiscal_signature = Column(String(500), nullable=False)
    qr_code_data = Column(String(5000), nullable=False)  # Base64 encoded QR image
    
    # VAT breakdown
    subtotal = Column(Numeric(15, 2), nullable=False)
    vat_rate = Column(Float, default=7.5)
    vat_amount = Column(Numeric(15, 2), nullable=False)
    total_amount = Column(Numeric(15, 2), nullable=False)
    
    # Zero-rated tracking
    zero_rated_amount = Column(Numeric(15, 2), default=0)
    zero_rated_items = Column(JSON, nullable=True)
    
    # Fiscalization transmission tracking (FIRS pending)
    transmitted_at = Column(DateTime, nullable=True)
    firs_response = Column(JSON, nullable=True)
    firs_validation_status = Column(String(20), default="pending")
    firs_transaction_id = Column(String(100), nullable=True)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    
    # Relationships
    invoice = relationship("Invoice", back_populates="fiscal_data")


class VATReturn(Base):
    """
    Monthly VAT returns (internal aggregation; external submission pipeline pending).
    
    Aggregates:
    - Output VAT (collected from customers)
    - Input VAT (paid to suppliers)
    - Net VAT payable
    - Zero-rated and exempt sales
    """
    __tablename__ = "vat_returns"
    
    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    
    # Tax period
    tax_period = Column(String(7), nullable=False, index=True)  # Format: YYYY-MM
    start_date = Column(DateTime, nullable=False)
    end_date = Column(DateTime, nullable=False)
    
    # VAT calculations
    output_vat = Column(Numeric(15, 2), default=0)
    input_vat = Column(Numeric(15, 2), default=0)
    net_vat = Column(Numeric(15, 2), default=0)
    
    # Special categories
    zero_rated_sales = Column(Numeric(15, 2), default=0)
    exempt_sales = Column(Numeric(15, 2), default=0)
    
    # Invoice statistics
    total_invoices = Column(Integer, default=0)
    fiscalized_invoices = Column(Integer, default=0)
    
    # Submission tracking
    status = Column(String(20), default="draft")  # draft, submitted, accepted, rejected
    submitted_at = Column(DateTime, nullable=True)
    firs_submission_id = Column(String(100), nullable=True)
    
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
    
    # Relationships
    user = relationship("User", back_populates="vat_returns")


class MonthlyTaxReport(Base):
    """Tax compliance report metadata supporting multiple time aggregations.

    Stores:
    - Assessable profit & development levy
    - VAT breakdown (taxable, zero-rated, exempt)
    - Inventory COGS (Cost of Goods Sold) for accurate profit calculation
    - Generated PDF URL
    - Period type: day, week, month, year
    - Date range: start_date to end_date
    
    Backward compatible with monthly reports (year + month).
    """
    __tablename__ = "monthly_tax_reports"
    __table_args__ = ({},)

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("user.id"), nullable=False, index=True)
    
    # Period type and date range (new fields)
    period_type = Column(String(10), nullable=False, default="month", server_default="month")
    start_date = Column(Date, nullable=True)
    end_date = Column(Date, nullable=True)
    
    # Legacy fields for backward compatibility (nullable for non-monthly reports)
    year = Column(Integer, nullable=True)
    month = Column(Integer, nullable=True)
    
    # Financial data
    assessable_profit = Column(Numeric(15, 2), default=0)
    levy_amount = Column(Numeric(15, 2), default=0)
    pit_amount = Column(Numeric(15, 2), default=0, nullable=True)  # Personal Income Tax
    cit_amount = Column(Numeric(15, 2), default=0, nullable=True)  # Company Income Tax (PRO+ plans)
    vat_collected = Column(Numeric(15, 2), default=0)
    taxable_sales = Column(Numeric(15, 2), default=0)
    zero_rated_sales = Column(Numeric(15, 2), default=0)
    exempt_sales = Column(Numeric(15, 2), default=0)
    
    # Inventory/COGS fields for accurate profit calculation
    cogs_amount = Column(Numeric(15, 2), default=0, nullable=True)  # Cost of Goods Sold
    inventory_purchases = Column(Numeric(15, 2), default=0, nullable=True)  # Purchases in period
    inventory_value = Column(Numeric(15, 2), default=0, nullable=True)  # Ending inventory value
    
    pdf_url = Column(String(500), nullable=True)
    generated_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(
        DateTime,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

