"""Unit tests for assessable profit and levy endpoint logic (service-level)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.models import Customer, Invoice, User
from app.services.tax_reporting_service import TaxReportingService

# NOTE: These tests use SQLAlchemy; ensure 'sqlalchemy' is in requirements for local runs.
# In-memory SQLite (simplified) – if models have vendor-specific types, adjust accordingly.
engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


def make_user(session, user_id: int = 1):
    """Create a user + matching customer with unique phone/email."""
    u = User(
        id=user_id,
        phone=f"+234{user_id:09d}",
        name="Test",
        email=f"test{user_id}@example.com",
    )
    session.add(u)
    session.flush()
    c = Customer(
        id=user_id,
        name="Cust",
        phone="+234000000001",
        email="cust@example.com",
    )
    session.add(c)
    session.commit()
    return u


def make_invoice(
    session,
    issuer_id: int,
    amount: float,
    status: str = "paid",
    discount: float | None = None,
    days_offset: int = -1,
):
    """Insert an invoice with deterministic due/created timestamps."""
    now_base = datetime.now(timezone.utc) + timedelta(days=days_offset)
    inv = Invoice(
        invoice_id=f"INV-{issuer_id}-{amount}-{status}-{days_offset}",
        issuer_id=issuer_id,
        customer_id=issuer_id,
        amount=Decimal(str(amount)),
        discount_amount=Decimal(str(discount)) if discount else None,
        status=status,
        due_date=now_base,
        created_at=now_base,
    )
    session.add(inv)
    session.commit()
    return inv


def test_assessable_profit_paid_basis_excludes_future_and_discount():
    session = SessionLocal()
    make_user(session, 1)
    service = TaxReportingService(session)
    # Paid invoice past due
    make_invoice(session, 1, 10000, status="paid", discount=1000, days_offset=-10)
    # Paid invoice future due (should be excluded)
    make_invoice(session, 1, 5000, status="paid", discount=None, days_offset=5)
    # Pending invoice past due (excluded for paid basis)
    make_invoice(session, 1, 8000, status="pending", days_offset=-3)
    profit = service.compute_assessable_profit(1, basis="paid")
    # Only first invoice counts: 10000 - 1000 = 9000
    assert profit == Decimal("9000")


def test_assessable_profit_all_basis_includes_pending_not_future():
    session = SessionLocal()
    make_user(session, 2)
    service = TaxReportingService(session)
    make_invoice(session, 2, 10000, status="paid", discount=2000, days_offset=-2)
    make_invoice(session, 2, 6000, status="pending", discount=None, days_offset=-1)
    # Future due excluded
    make_invoice(
        session,
        2,
        4000,
        status="paid",
        discount=None,
        days_offset=10,
    )
    profit = service.compute_assessable_profit(2, basis="all")
    # (10000-2000) + 6000 = 14000
    assert profit == Decimal("14000")


def test_development_levy_medium_business_calculation():
    # Repeat medium classification scenario to ensure wrapper path works after earlier assertions
    session2 = SessionLocal()
    make_user(session2, 7)
    service2 = TaxReportingService(session2)
    service2.update_profile(
        7,
        annual_turnover=Decimal("150000000"),
        fixed_assets=Decimal("300000000"),
    )
    make_invoice(session2, 7, 200000, status="paid", discount=20000, days_offset=-1)
    profit2 = service2.compute_assessable_profit(7, basis="paid")
    levy2 = service2.compute_development_levy(7, profit2)
    assert levy2["levy_amount"] == 7200.0


def test_assessable_profit_month_filter():
    session = SessionLocal()
    make_user(session, 4)
    service = TaxReportingService(session)
    now = datetime.now(timezone.utc)
    target_year = now.year if now.month > 2 else now.year - 1
    # Explicit dates to avoid approximate timedelta drift
    jan_date = datetime(target_year, 1, 15, tzinfo=timezone.utc)
    feb_date = datetime(target_year, 2, 15, tzinfo=timezone.utc)
    inv_jan = Invoice(
        invoice_id="INV-JAN-4",
        issuer_id=4,
        customer_id=4,
        amount=Decimal("5000"),
        discount_amount=Decimal("500"),
        status="paid",
        due_date=jan_date,
        created_at=jan_date,
    )
    inv_feb = Invoice(
        invoice_id="INV-FEB-4",
        issuer_id=4,
        customer_id=4,
        amount=Decimal("7000"),
        discount_amount=Decimal("0"),
        status="paid",
        due_date=feb_date,
        created_at=feb_date,
    )
    session.add_all([inv_jan, inv_feb])
    session.commit()
    profit = service.compute_assessable_profit(4, year=target_year, month=2, basis="paid")
    assert profit == Decimal("7000")


def test_assessable_profit_discount_subtraction():
    session = SessionLocal()
    make_user(session, 5)
    service = TaxReportingService(session)
    make_invoice(session, 5, 10000, status="paid", discount=1234, days_offset=-1)
    profit = service.compute_assessable_profit(5, basis="paid")
    assert profit == Decimal("8766")


def test_assessable_profit_basis_toggle_difference():
    session = SessionLocal()
    make_user(session, 6)
    service = TaxReportingService(session)
    make_invoice(session, 6, 4000, status="paid", discount=None, days_offset=-2)
    make_invoice(session, 6, 3000, status="pending", discount=None, days_offset=-2)
    paid_profit = service.compute_assessable_profit(6, basis="paid")
    all_profit = service.compute_assessable_profit(6, basis="all")
    assert paid_profit == Decimal("4000")
    assert all_profit == Decimal("7000")
    session = SessionLocal()
    make_user(session, 3)
    service = TaxReportingService(session)
    # Force medium classification
    service.update_profile(
        3,
        annual_turnover=Decimal("150000000"),
        fixed_assets=Decimal("300000000"),
    )
    make_invoice(session, 3, 200000, status="paid", discount=20000, days_offset=-1)
    profit = service.compute_assessable_profit(3, basis="paid")
    levy = service.compute_development_levy(3, profit)
    # Profit = 180000; levy 4% = 7200
    assert levy["levy_amount"] == 7200.0
