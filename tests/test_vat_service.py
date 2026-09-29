"""Tests for VAT service."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.models import Customer, Invoice, User
from app.models.tax_models import TaxProfile, VATReturn
from app.services.vat_service import ComplianceChecker, VATService

engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


@pytest.fixture
def db_session():
    """Create a fresh database session for each test."""
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def test_user(db_session, request):
    """Create a test user."""
    # Generate unique email/phone per test using test node name hash
    test_hash = str(hash(request.node.name) % 100000).zfill(5)
    user = User(
        phone=f"+23412345680{test_hash[:2]}",
        name="VATTestUser",
        email=f"vat-test-{test_hash}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def tax_profile_factory(db_session):
    """Factory to create tax profiles for tests."""

    def _create(user, **overrides):
        data = {
            "user_id": user.id,
            "vat_registered": True,
            "vat_registration_number": "VAT-TEST-123",
            "tin": "TIN-TEST-123",
        }
        data.update(overrides)
        profile = TaxProfile(**data)
        db_session.add(profile)
        db_session.commit()
        return profile

    return _create


def test_vat_service_initialization(db_session):
    """Test VAT service can be initialized."""
    service = VATService(db_session)
    assert service.db == db_session


def test_calculate_monthly_vat(db_session, test_user):
    """Test monthly VAT calculation."""
    service = VATService(db_session)
    now = datetime.now(timezone.utc)

    result = service.calculate_monthly_vat(test_user.id, now.year, now.month)

    # Should return dict with VAT fields
    assert isinstance(result, dict)
    assert "output_vat" in result
    assert "input_vat" in result


def test_calculate_monthly_vat_includes_final_microsecond(db_session, test_user):
    customer = Customer(name="Month End Customer", phone="+2348000000000")
    db_session.add(customer)
    db_session.flush()
    invoice = Invoice(
        invoice_id="INV-MONTH-END",
        issuer_id=test_user.id,
        customer_id=customer.id,
        amount=Decimal("1075"),
        vat_amount=Decimal("75"),
        vat_category="standard",
        status="pending",
        created_at=datetime(2026, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc),
    )
    db_session.add(invoice)
    db_session.commit()

    result = VATService(db_session).calculate_monthly_vat(test_user.id, 2026, 1)

    assert result["total_invoices"] == 1
    assert result["output_vat"] == Decimal("75")
    assert result["end_date"] == datetime(2026, 1, 31, 23, 59, 59, 999999, tzinfo=timezone.utc)


def test_compliance_uses_last_month_when_current_month_return_exists():
    profile = SimpleNamespace(vat_registered=True)
    now = datetime.now(timezone.utc)
    current_period = f"{now.year:04d}-{now.month:02d}"
    last_month = now.replace(day=1) - timedelta(days=1)
    last_period = f"{last_month.year:04d}-{last_month.month:02d}"
    current_return = VATReturn(tax_period=current_period, status="draft")
    submitted_return = VATReturn(tax_period=last_period, status="submitted")

    status = ComplianceChecker.check_status(profile, [current_return, submitted_return])

    assert status == "compliant"


def test_get_vat_summary(db_session, test_user):
    """Test VAT summary retrieval."""
    service = VATService(db_session)

    summary = service.get_vat_summary(test_user.id)

    # Should return dict with summary fields
    assert isinstance(summary, dict)
    assert summary["registered"] is False
    assert "message" in summary


def test_get_vat_summary_registered_user(db_session, test_user, tax_profile_factory):
    """VAT summary should include compliance data for registered users."""
    profile = tax_profile_factory(test_user, vat_registration_number="VAT-12345")
    service = VATService(db_session)
    now = datetime.now(timezone.utc)
    last_month_end = now.replace(day=1) - timedelta(days=1)
    last_month_start = last_month_end.replace(day=1)
    tax_period = f"{last_month_end.year:04d}-{last_month_end.month:02d}"
    vat_return = VATReturn(
        user_id=test_user.id,
        tax_period=tax_period,
        start_date=last_month_start,
        end_date=last_month_end,
        output_vat=Decimal("1000"),
        input_vat=Decimal("200"),
        net_vat=Decimal("800"),
        zero_rated_sales=Decimal("0"),
        exempt_sales=Decimal("0"),
        total_invoices=5,
        fiscalized_invoices=2,
        status="submitted",
        submitted_at=now,
    )
    db_session.add(vat_return)
    db_session.commit()

    summary = service.get_vat_summary(test_user.id)

    assert summary["registered"] is True
    assert summary["vat_number"] == profile.vat_registration_number
    assert summary["compliance_status"] == "compliant"
    assert summary["next_action"] == "Up to date - no action needed"
    assert summary["recent_returns"][0]["period"] == tax_period
    assert summary["recent_returns"][0]["status"] == "submitted"
    assert summary["current_month"]["tax_period"]
    assert "net_vat" in summary["current_month"]
