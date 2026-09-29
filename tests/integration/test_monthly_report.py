"""Tests for MonthlyTaxReport aggregation logic."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.models import Customer, Invoice, User
from app.services.tax_reporting_service import TaxReportingService

engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


def seed_user(session, user_id: int = 1):
    """Create a user + matching customer with unique phone/email.

    Ensures uniqueness across test invocations.
    """
    from app.models.models import SubscriptionPlan

    u = User(
        id=user_id,
        phone=f"+234{user_id:09d}",
        name="ReportUser",
        email=f"ru{user_id}@example.com",
        plan=SubscriptionPlan.PRO,  # VAT tracking requires PRO or BUSINESS plan
    )
    c = Customer(
        id=user_id,
        name="ReportCust",
        phone=f"+234{user_id:09d}9",
        email=f"rc{user_id}@example.com",
    )
    session.add_all([u, c])
    session.commit()
    return u


def add_invoice(
    session,
    issuer_id: int,
    amount: float,
    category: str,
    vat_amount: float | None,
    year: int,
    month: int,
    day: int = 15,
):
    """Insert a paid invoice with deterministic timestamps."""
    created = datetime(year, month, day, tzinfo=timezone.utc)
    inv = Invoice(
        invoice_id=f"INV-{issuer_id}-{year}{month}{day}-{category}",
        issuer_id=issuer_id,
        customer_id=issuer_id,
        amount=Decimal(str(amount)),
        discount_amount=None,
        status="paid",
        due_date=created,
        created_at=created,
        invoice_type="revenue",  # Must be revenue for tax report aggregation
        vat_category=category,
        vat_amount=Decimal(str(vat_amount)) if vat_amount is not None else None,
    )
    session.add(inv)
    session.commit()
    return inv


def test_monthly_report_aggregation_vat_and_levy():
    session = SessionLocal()
    seed_user(session, 1)
    # VAT is opt-in: register this user as VAT-registered
    from app.models.tax_models import TaxProfile

    profile = TaxProfile(user_id=1, vat_registered=True)
    session.add(profile)
    session.commit()
    service = TaxReportingService(session)
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    # Standard taxable invoice with VAT
    add_invoice(session, 1, 10000, "standard", 750, year, month)
    # Zero-rated invoice
    add_invoice(session, 1, 4000, "zero_rated", None, year, month)
    # Exempt invoice
    add_invoice(session, 1, 6000, "exempt", None, year, month)

    report = service.generate_monthly_report(1, year, month, basis="paid", force_regenerate=True)

    assert float(report.taxable_sales) == 9250.0  # VAT-exclusive (10000 - 750)
    assert float(report.zero_rated_sales) == 4000.0
    assert float(report.exempt_sales) == 6000.0
    assert float(report.vat_collected) == 750.0
    # Assessable profit equals sum of all (no discounts)
    assert float(report.assessable_profit) == 10000 + 4000 + 6000
    # Update classification to medium and regenerate levy
    service.update_profile(
        1,
        annual_turnover=Decimal("150000000"),
        fixed_assets=Decimal("300000000"),
    )
    report2 = service.generate_monthly_report(1, year, month, basis="paid", force_regenerate=True)
    assert float(report2.levy_amount) > 0


def test_monthly_report_regeneration_retains_pdf_url():
    session = SessionLocal()
    seed_user(session, 2)
    # VAT is opt-in: register this user as VAT-registered
    from app.models.tax_models import TaxProfile

    profile = TaxProfile(user_id=2, vat_registered=True)
    session.add(profile)
    session.commit()
    service = TaxReportingService(session)
    now = datetime.now(timezone.utc)
    year = now.year
    month = now.month
    add_invoice(session, 2, 5000, "standard", 375, year, month)
    report = service.generate_monthly_report(2, year, month, basis="paid", force_regenerate=True)
    # Simulate PDF attach and ensure retained when not forcing regeneration
    service.attach_report_pdf(report, "http://example.com/report.pdf")
    regenerated = service.generate_monthly_report(
        2,
        year,
        month,
        basis="paid",
        force_regenerate=False,
    )
    assert regenerated.pdf_url == "http://example.com/report.pdf"
