"""Tests for analytics service."""

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.models import Customer, Invoice, User
from app.services.analytics_service import (
    calculate_aging_report,
    calculate_customer_metrics,
    calculate_invoice_metrics,
    calculate_monthly_trends,
    calculate_revenue_metrics,
)

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
    # Generate unique email/phone per test using test node name hash with more uniqueness
    import uuid

    unique_id = str(uuid.uuid4().hex)[:8]
    user = User(
        phone=f"+234{unique_id}",
        name="TestUser",
        email=f"analytics-test-{unique_id}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def test_customer(db_session):
    """Create a test customer."""
    customer = Customer(
        name="TestCustomer",
        phone="+2349876543210",
        email="analytics-customer@example.com",
    )
    db_session.add(customer)
    db_session.commit()
    return customer


def create_invoice(
    db_session,
    issuer_id: int,
    customer_id: int,
    amount: Decimal,
    status: str = "pending",
    invoice_type: str = "revenue",
    created_at: datetime = None,
    due_date: datetime = None,
):
    """Helper to create an invoice."""
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    if due_date is None:
        due_date = created_at + timedelta(days=30)

    invoice = Invoice(
        invoice_id=f"INV-{issuer_id}-{created_at.timestamp()}",
        issuer_id=issuer_id,
        customer_id=customer_id,
        amount=amount,
        status=status,
        invoice_type=invoice_type,
        created_at=created_at,
        due_date=due_date,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_calculate_revenue_metrics_no_invoices(db_session, test_user):
    """Test revenue metrics with no invoices."""
    start = date.today() - timedelta(days=30)
    end = date.today()

    metrics = calculate_revenue_metrics(db_session, test_user.id, start, end, Decimal("1.0"))

    assert metrics.total_revenue == pytest.approx(0.0)
    assert metrics.paid_revenue == pytest.approx(0.0)
    assert metrics.pending_revenue == pytest.approx(0.0)
    assert metrics.overdue_revenue == pytest.approx(0.0)


def test_calculate_revenue_metrics_with_invoices(db_session, test_user, test_customer):
    """Test revenue metrics with various invoice statuses."""
    today = datetime.now(timezone.utc)

    # Create paid invoice
    create_invoice(db_session, test_user.id, test_customer.id, Decimal("1000"), "paid")

    # Create pending invoice
    create_invoice(db_session, test_user.id, test_customer.id, Decimal("500"), "pending")

    # Create overdue invoice
    overdue_date = today - timedelta(days=60)
    create_invoice(
        db_session,
        test_user.id,
        test_customer.id,
        Decimal("300"),
        "pending",
        created_at=overdue_date,
        due_date=overdue_date + timedelta(days=30),
    )

    start = date.today() - timedelta(days=90)
    end = date.today()

    metrics = calculate_revenue_metrics(db_session, test_user.id, start, end, Decimal("1.0"))

    assert metrics.total_revenue == pytest.approx(1800.0)
    assert metrics.paid_revenue == pytest.approx(1000.0)
    assert metrics.pending_revenue == pytest.approx(500.0)
    assert metrics.overdue_revenue == pytest.approx(300.0)


def test_calculate_invoice_metrics(db_session, test_user, test_customer):
    """Test invoice metrics calculation."""
    # Create various invoices
    create_invoice(db_session, test_user.id, test_customer.id, Decimal("1000"), "paid")
    create_invoice(db_session, test_user.id, test_customer.id, Decimal("500"), "pending")
    create_invoice(db_session, test_user.id, test_customer.id, Decimal("200"), "failed")

    start = date.today() - timedelta(days=30)
    end = date.today()

    metrics = calculate_invoice_metrics(db_session, test_user.id, start, end)

    assert metrics.total_invoices == 3
    assert metrics.paid_invoices == 1
    assert metrics.pending_invoices == 1
    assert metrics.failed_invoices == 1


def test_calculate_customer_metrics(db_session, test_user):
    """Test customer metrics calculation."""
    # Create customers with invoices
    for i in range(5):
        customer = Customer(
            id=i + 10,
            name=f"Customer{i}",
            phone=f"+23490000000{i}",
            email=f"cust{i}@example.com",
        )
        db_session.add(customer)
        db_session.commit()

        # Create invoices for some customers
        if i < 3:
            create_invoice(
                db_session,
                test_user.id,
                customer.id,
                Decimal("100") * (i + 1),
                "paid",
            )

    start = date.today() - timedelta(days=30)
    end = date.today()

    metrics = calculate_customer_metrics(db_session, test_user.id, start, end)

    assert metrics.total_customers >= 3
    assert metrics.active_customers == 3
    assert metrics.repeat_customer_rate >= 0


def test_calculate_aging_report(db_session, test_user, test_customer):
    """Test aging report calculation."""
    today = datetime.now(timezone.utc)

    # Create invoices with different ages
    create_invoice(
        db_session,
        test_user.id,
        test_customer.id,
        Decimal("100"),
        "pending",
        due_date=today - timedelta(days=10),
    )
    create_invoice(
        db_session,
        test_user.id,
        test_customer.id,
        Decimal("200"),
        "pending",
        due_date=today - timedelta(days=40),
    )
    create_invoice(
        db_session,
        test_user.id,
        test_customer.id,
        Decimal("300"),
        "pending",
        due_date=today - timedelta(days=70),
    )

    report = calculate_aging_report(db_session, test_user.id, date.today(), Decimal("1.0"))

    assert report.current >= 0
    assert report.days_31_60 >= 0
    assert report.days_61_90 >= 0
    assert report.over_90_days >= 0


def test_calculate_monthly_trends(db_session, test_user, test_customer):
    """Test monthly trends calculation."""
    today = datetime.now(timezone.utc)

    # Create invoices for the last 3 months
    for i in range(3):
        month_ago = today - timedelta(days=30 * i)
        create_invoice(
            db_session,
            test_user.id,
            test_customer.id,
            Decimal("1000") * (i + 1),
            "paid",
            created_at=month_ago,
        )

    trends = calculate_monthly_trends(db_session, test_user.id, date.today(), Decimal("1.0"))

    assert len(trends) == 12
    assert all(isinstance(trend.month, str) for trend in trends)
    assert all(trend.revenue >= Decimal("0") for trend in trends)


# ── Storefront insights + abandoned-cart consistency ─────────────────


def _seed_storefront_order(db_session, seller, customer, *, gross_naira, status="held", desc="Widget", qty=2):
    from app.models.models import InvoiceLine, StorefrontOrderEscrow

    inv = create_invoice(db_session, seller.id, customer.id, Decimal(str(gross_naira)), status="paid")
    inv.channel = "storefront"
    db_session.add(
        InvoiceLine(
            invoice_id=inv.id,
            description=desc,
            quantity=qty,
            unit_price=Decimal(str(gross_naira)) / qty,
        )
    )
    gross_kobo = int(Decimal(str(gross_naira)) * 100)
    esc = StorefrontOrderEscrow(
        invoice_id=inv.id,
        seller_id=seller.id,
        status=status,
        gross_kobo=gross_kobo,
        fee_kobo=int(gross_kobo * 0.03),
        payout_kobo=gross_kobo,
    )
    db_session.add(esc)
    db_session.commit()
    return inv, esc


def test_storefront_insights_disabled_for_non_store(db_session, test_user):
    from app.services.analytics_service import calculate_storefront_insights

    out = calculate_storefront_insights(
        db_session,
        test_user.id,
        date.today() - timedelta(days=30),
        date.today(),
        Decimal("1.0"),
    )
    assert out["enabled"] is False
    assert out["orders"] == 0
    assert out["top_products"] == []


def test_storefront_insights_counts_orders_views_and_top_products(db_session, test_user, test_customer):
    from app.services.analytics_service import calculate_storefront_insights

    test_user.storefront_enabled = True
    test_user.storefront_slug = "teststore"
    test_user.storefront_views = 200
    db_session.commit()

    _seed_storefront_order(db_session, test_user, test_customer, gross_naira=10000, status="held", desc="Cake", qty=2)
    _seed_storefront_order(
        db_session, test_user, test_customer, gross_naira=5000, status="released", desc="Bread", qty=1
    )
    # An abandoned cart (pending) must NOT count as paid / GMV / a top product.
    _seed_storefront_order(
        db_session, test_user, test_customer, gross_naira=9999, status="pending", desc="Ghost", qty=1
    )

    out = calculate_storefront_insights(
        db_session,
        test_user.id,
        date.today() - timedelta(days=30),
        date.today(),
        Decimal("1.0"),
    )

    assert out["enabled"] is True
    assert out["views"] == 200
    assert out["orders"] == 3
    assert out["paid_orders"] == 2
    assert out["abandoned_orders"] == 1
    assert out["gmv"] == pytest.approx(15000.0)  # 10,000 + 5,000 goods
    assert out["awaiting_release"] == pytest.approx(10000.0)  # held payout only
    assert out["conversion_rate"] == pytest.approx(1.0)  # 2 paid / 200 views
    names = [p["name"] for p in out["top_products"]]
    assert names[0] == "Cake"  # 2 units beats Bread's 1
    assert "Ghost" not in names


def test_revenue_metrics_excludes_abandoned_storefront(db_session, test_user, test_customer):
    """Abandoned storefront carts (channel=storefront + pending) must not inflate
    total/pending revenue — consistent with the conversion funnel."""
    # A normal manual pending invoice DOES count as pending revenue.
    create_invoice(db_session, test_user.id, test_customer.id, Decimal("500"), "pending")
    # An abandoned storefront cart must be excluded from the metrics.
    ghost = create_invoice(db_session, test_user.id, test_customer.id, Decimal("999"), "pending")
    ghost.channel = "storefront"
    db_session.commit()

    metrics = calculate_revenue_metrics(
        db_session,
        test_user.id,
        date.today() - timedelta(days=30),
        date.today(),
        Decimal("1.0"),
    )
    assert metrics.pending_revenue == pytest.approx(500.0)
    assert metrics.total_revenue == pytest.approx(500.0)


def test_aging_report_excludes_abandoned_storefront(db_session, test_user, test_customer):
    """A manual pending invoice is a receivable; an abandoned storefront cart is
    not — it must not inflate Total Outstanding / accounts receivable."""
    today = datetime.now(timezone.utc)
    create_invoice(
        db_session,
        test_user.id,
        test_customer.id,
        Decimal("700"),
        "pending",
        due_date=today - timedelta(days=5),
    )
    ghost = create_invoice(
        db_session,
        test_user.id,
        test_customer.id,
        Decimal("2500"),
        "pending",
        due_date=today - timedelta(days=5),
    )
    ghost.channel = "storefront"
    db_session.commit()

    report = calculate_aging_report(db_session, test_user.id, date.today(), Decimal("1.0"))
    assert report.total_outstanding == pytest.approx(700.0)
    assert report.current == pytest.approx(700.0)
