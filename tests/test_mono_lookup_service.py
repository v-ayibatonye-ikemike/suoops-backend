"""Mono Lookup (TIN/CAC verification) service tests.

No real network calls — MonoLookupClient's HTTP methods are monkeypatched
per-test so the wallet-charging and idempotency logic is exercised against
predictable, controlled responses.
"""
from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.exceptions import (
    InvalidCACError,
    InvalidTINError,
    LookupBalanceExhaustedError,
)
from app.db.base_class import Base
from app.models.models import User
from app.models.tax_models import TaxProfile
from app.services.mono_lookup_service import (
    LOOKUP_COST_KOBO,
    MonoLookupClient,
    verify_business_cac,
    verify_business_tin,
)

engine = create_engine("sqlite:///:memory:")
SessionLocal = sessionmaker(bind=engine)
Base.metadata.create_all(bind=engine)


class _FakeMonoClient(MonoLookupClient):
    """Stub that never touches the network — returns canned results."""

    def __init__(self, *, tin_ok: bool = True, cac_ok: bool = True, cac_name: str = "Acme Nigeria Ltd"):
        self.tin_ok = tin_ok
        self.cac_ok = cac_ok
        self.cac_name = cac_name

    def verify_tin(self, tin: str) -> dict:  # noqa: D401
        if not self.tin_ok:
            raise InvalidTINError(tin=tin, reason="Not found")
        return {"tin": tin, "status": "valid"}

    def verify_cac(self, rc_number: str) -> dict:  # noqa: D401
        if not self.cac_ok:
            raise InvalidCACError(rc_number=rc_number, reason="Not found")
        return {"rc_number": rc_number, "company_name": self.cac_name}


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
        phone=f"+237{unique_id}",
        name="MonoTestUser",
        email=f"mono-{unique_id}@example.com",
        wallet_balance_kobo=10_000,  # ₦100 — enough for one of each lookup
    )
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture
def tax_profile(db_session, test_user):
    profile = TaxProfile(user_id=test_user.id, tin="12345678-0001")
    db_session.add(profile)
    db_session.commit()
    return profile


def test_verify_tin_success_charges_wallet_and_marks_verified(db_session, test_user, tax_profile):
    starting_balance = test_user.wallet_balance_kobo

    result = verify_business_tin(db_session, test_user.id, client=_FakeMonoClient())

    assert result.tin_verified is True
    assert result.verification_status == "verified"
    assert result.last_verification_at is not None

    db_session.refresh(test_user)
    assert test_user.wallet_balance_kobo == starting_balance - LOOKUP_COST_KOBO["tin"]


def test_verify_tin_failure_does_not_charge_wallet(db_session, test_user, tax_profile):
    starting_balance = test_user.wallet_balance_kobo

    with pytest.raises(InvalidTINError):
        verify_business_tin(db_session, test_user.id, client=_FakeMonoClient(tin_ok=False))

    db_session.refresh(test_user)
    db_session.refresh(tax_profile)
    assert test_user.wallet_balance_kobo == starting_balance  # untouched
    assert tax_profile.tin_verified is False
    # Attempt is still recorded even though the lookup failed.
    assert tax_profile.verification_attempts == 1


def test_verify_tin_is_idempotent_no_double_charge(db_session, test_user, tax_profile):
    verify_business_tin(db_session, test_user.id, client=_FakeMonoClient())
    db_session.refresh(test_user)
    balance_after_first = test_user.wallet_balance_kobo

    # Second call should short-circuit — already verified, no Mono call, no charge.
    verify_business_tin(db_session, test_user.id, client=_FakeMonoClient(tin_ok=False))
    db_session.refresh(test_user)

    assert test_user.wallet_balance_kobo == balance_after_first


def test_verify_tin_raises_when_wallet_cannot_cover_fee(db_session, tax_profile):
    import uuid

    poor_user = User(
        phone=f"+238{uuid.uuid4().hex[:8]}",
        name="PoorUser",
        email=f"poor-{uuid.uuid4().hex[:8]}@example.com",
        wallet_balance_kobo=10,  # ₦0.10 — not enough for ₦50 TIN lookup
    )
    db_session.add(poor_user)
    db_session.commit()

    profile = TaxProfile(user_id=poor_user.id, tin="98765432-0001")
    db_session.add(profile)
    db_session.commit()

    with pytest.raises(LookupBalanceExhaustedError):
        verify_business_tin(db_session, poor_user.id, client=_FakeMonoClient())

    db_session.refresh(poor_user)
    assert poor_user.wallet_balance_kobo == 10  # untouched


def test_verify_cac_success_stores_registered_name_and_charges_wallet(db_session, test_user, tax_profile):
    starting_balance = test_user.wallet_balance_kobo

    result = verify_business_cac(
        db_session, test_user.id, "RC1234567", client=_FakeMonoClient(cac_name="Corner Shop Ventures Ltd")
    )

    assert result.cac_verified is True
    assert result.rc_number == "RC1234567"
    assert result.cac_registered_name == "Corner Shop Ventures Ltd"
    assert result.cac_verified_at is not None

    db_session.refresh(test_user)
    assert test_user.wallet_balance_kobo == starting_balance - LOOKUP_COST_KOBO["cac"]


def test_verify_cac_failure_does_not_charge_wallet(db_session, test_user, tax_profile):
    starting_balance = test_user.wallet_balance_kobo

    with pytest.raises(InvalidCACError):
        verify_business_cac(db_session, test_user.id, "RC0000000", client=_FakeMonoClient(cac_ok=False))

    db_session.refresh(test_user)
    assert test_user.wallet_balance_kobo == starting_balance
