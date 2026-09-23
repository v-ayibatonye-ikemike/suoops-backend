"""Tests for the Business Snapshot composite (analytics_service.calculate_business_snapshot).

Follows the same in-memory-SQLite pattern as test_analytics_service.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.models import Customer, Invoice, User
from app.services.analytics_service import calculate_business_snapshot

engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


@pytest.fixture
def db_session():
    session = SessionLocal()
    yield session
    session.close()


@pytest.fixture
def test_user(db_session):
    import uuid

    unique_id = str(uuid.uuid4().hex)[:8]
    user = User(
        phone=f"+235{unique_id}",
        name="SnapshotUser",
        email=f"snapshot-{unique_id}@example.com",
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def test_customer(db_session):
    import uuid

    unique_id = str(uuid.uuid4().hex)[:8]
    customer = Customer(name="SnapshotCustomer", phone=f"+236{unique_id}")
    db_session.add(customer)
    db_session.commit()
    return customer


def _make_invoice(
    db_session,
    issuer_id: int,
    customer_id: int,
    amount: Decimal,
    *,
    status: str = "paid",
    channel: str | None = None,
    status_updated_by_user_id: int | None = None,
    created_at: datetime | None = None,
    due_date: datetime | None = None,
) -> Invoice:
    created_at = created_at or datetime.now(timezone.utc)
    invoice = Invoice(
        invoice_id=f"INV-{issuer_id}-{created_at.timestamp()}-{amount}",
        issuer_id=issuer_id,
        customer_id=customer_id,
        amount=amount,
        status=status,
        invoice_type="revenue",
        channel=channel,
        status_updated_by_user_id=status_updated_by_user_id,
        created_at=created_at,
        due_date=due_date,
        paid_at=created_at if status == "paid" else None,
    )
    db_session.add(invoice)
    db_session.commit()
    return invoice


def test_snapshot_with_no_history_is_neutral_not_penalised(db_session, test_user):
    """A brand-new business with zero invoices shouldn't be scored as risky —
    there's nothing to judge yet, so payment reliability defaults neutral."""
    snapshot = calculate_business_snapshot(db_session, test_user.id)

    assert snapshot["payment_reliability"]["paid_ratio"] == 100.0
    assert 0.0 <= snapshot["composite_score"] <= 100.0
    assert "not a credit score" in snapshot["disclaimer"].lower()


def test_snapshot_splits_activity_mix_and_data_provenance(db_session, test_user, test_customer):
    now = datetime.now(timezone.utc)

    # A webhook-confirmed online payment (no human clicked "paid").
    _make_invoice(
        db_session, test_user.id, test_customer.id, Decimal("5000"),
        status="paid", status_updated_by_user_id=None, created_at=now,
    )
    # A storefront order (gateway-confirmed via escrow/checkout).
    _make_invoice(
        db_session, test_user.id, test_customer.id, Decimal("3000"),
        status="paid", channel="storefront", status_updated_by_user_id=test_user.id, created_at=now,
    )
    # A walk-in quick sale — self-reported (the business marked it paid itself).
    _make_invoice(
        db_session, test_user.id, test_customer.id, Decimal("2000"),
        status="paid", channel="quick_sale", status_updated_by_user_id=test_user.id, created_at=now,
    )
    # A manually confirmed regular invoice — also self-reported.
    _make_invoice(
        db_session, test_user.id, test_customer.id, Decimal("1000"),
        status="paid", status_updated_by_user_id=test_user.id, created_at=now,
    )

    snapshot = calculate_business_snapshot(db_session, test_user.id)

    # Activity mix: quick_sale is walk-in, everything else is "billed".
    assert snapshot["activity_mix"]["walk_in_sale_count"] == 1
    assert snapshot["activity_mix"]["walk_in_sale_amount"] == pytest.approx(2000.0)
    assert snapshot["activity_mix"]["billed_invoice_count"] == 3
    assert snapshot["activity_mix"]["billed_invoice_amount"] == pytest.approx(9000.0)

    # Data provenance: webhook-confirmed (5000) + storefront (3000) = gateway;
    # quick sale (2000) + manual self-confirm (1000) = self-reported.
    assert snapshot["data_provenance"]["gateway_confirmed_amount"] == pytest.approx(8000.0)
    assert snapshot["data_provenance"]["self_reported_amount"] == pytest.approx(3000.0)


def test_snapshot_penalises_heavy_overdue_load(db_session, test_user, test_customer):
    now = datetime.now(timezone.utc)
    very_overdue = now - timedelta(days=100)

    # Mostly overdue billed amount, nothing paid.
    _make_invoice(
        db_session, test_user.id, test_customer.id, Decimal("10000"),
        status="pending", created_at=very_overdue, due_date=very_overdue,
    )

    snapshot = calculate_business_snapshot(db_session, test_user.id)

    assert snapshot["payment_reliability"]["overdue_ratio"] > 0
    assert snapshot["payment_reliability"]["paid_ratio"] == 0.0
    assert snapshot["components"]["payment_reliability"] < 50.0


def test_snapshot_component_weights_sum_to_one(db_session, test_user):
    snapshot = calculate_business_snapshot(db_session, test_user.id)
    assert sum(snapshot["component_weights"].values()) == pytest.approx(1.0)

    # The composite score is exactly the weighted sum of the components —
    # no hidden adjustment, so a reader can verify the number themselves.
    expected = sum(
        snapshot["components"][k] * w for k, w in snapshot["component_weights"].items()
    )
    assert snapshot["composite_score"] == pytest.approx(round(expected, 1))
