"""Tests for feature gate utilities."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base_class import Base
from app.models.models import SubscriptionPlan, User
from app.utils.feature_gate import FeatureGate

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
        phone=f"+234123{test_hash}",
        name="TestUser",
        email=f"featuregate-test-{test_hash}@example.com",
        plan=SubscriptionPlan.FREE,
    )
    db_session.add(user)
    db_session.commit()
    return user


def test_feature_gate_initialization(db_session, test_user):
    """Test FeatureGate can be initialized."""
    gate = FeatureGate(db_session, test_user.id)
    assert gate.user_id == test_user.id
    assert gate.user.id == test_user.id


def test_is_free_tier(db_session, test_user):
    """Test is_free_tier check."""
    gate = FeatureGate(db_session, test_user.id)
    assert gate.is_free_tier() is True
    assert gate.is_paid_tier() is False


def test_is_paid_tier(db_session):
    """Test is_paid_tier check for paid plan."""
    user = User(
        phone="+2349876543210",
        name="PaidUser",
        email="paid@example.com",
        plan=SubscriptionPlan.PRO,
    )
    db_session.add(user)
    db_session.commit()

    gate = FeatureGate(db_session, user.id)
    assert gate.is_free_tier() is False
    assert gate.is_paid_tier() is True


def test_get_monthly_invoice_count(db_session, test_user):
    """Test monthly invoice count retrieval."""
    gate = FeatureGate(db_session, test_user.id)
    count = gate.get_monthly_invoice_count()
    assert isinstance(count, int)
    assert count >= 0
