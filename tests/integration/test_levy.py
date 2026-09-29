# ruff: noqa: I001 - import grouping acceptable for test clarity
"""Unit tests for development levy computation (non-integration).
Run with: pytest -q test_levy.py
"""

from __future__ import annotations

from decimal import Decimal

from app.services.tax_service import TaxProfileService  # wrappers retained


class DummySession:
    """Minimal stub for SQLAlchemy Session used by TaxProfileService.get_or_create_profile.
    We only support the query(TaxProfile).filter(...).first() path returning None to force creation.
    Creation/commit/refresh are no-ops for this isolated test.
    """

    def __init__(self):
        self.created = []

    def query(self, model):  # noqa: D401
        class Q:
            def __init__(self_inner):
                self_inner._model = model

            def filter(self_inner, *args, **kwargs):
                return self_inner

            def first(self_inner):
                # Return first created instance of model if exists
                for obj in self.created:
                    if isinstance(obj, self_inner._model):
                        return obj
                return None

        return Q()

    def add(self, obj):
        self.created.append(obj)

    def commit(self):
        pass

    def refresh(self, obj):
        pass


def test_compute_development_levy_small_business():
    session = DummySession()
    service = TaxProfileService(session)
    # Force creation of profile with small business thresholds
    service.update_profile(
        user_id=1,
        annual_turnover=Decimal("50000000"),  # below 100M
        fixed_assets=Decimal("200000000"),  # below 250M
    )
    result = service.compute_development_levy(1, Decimal("10000000"))  # 10M profit
    assert result["levy_applicable"] is False
    assert result["levy_amount"] == 0.0
    assert result["exemption_reason"] == "small_business"


def test_compute_development_levy_medium_business():
    session = DummySession()
    service = TaxProfileService(session)
    service.update_profile(
        user_id=2,
        annual_turnover=Decimal("150000000"),  # > 100M triggers medium
        fixed_assets=Decimal("300000000"),  # > 250M
    )
    result = service.compute_development_levy(2, Decimal("50000000"))  # 50M profit
    assert result["levy_applicable"] is True
    # 4% of 50,000,000 = 2,000,000.00
    assert result["levy_amount"] == 2000000.0
    assert result["exemption_reason"] is None
