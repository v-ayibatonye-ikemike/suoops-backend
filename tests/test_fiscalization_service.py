from decimal import Decimal

import pytest

from app.services.fiscalization_service import FiscalCodeGenerator, FiscalizationService, VATCalculator


class DummyInvoice:
    def __init__(self):
        from datetime import datetime, timezone

        self.id = 1
        self.invoice_id = "INV-001"
        self.issuer_id = 123
        self.amount = Decimal("1000.00")
        self.vat_amount = None
        self.vat_rate = None
        self.vat_category = "standard"
        self.created_at = datetime.now(timezone.utc)
        self.customer = type("C", (), {"name": "Test Customer"})()
        self.lines = []
        self.fiscal_data = None


class DummySession:
    """Minimal stub of SQLAlchemy Session for constructor injection."""

    def query(self, *args, **kwargs):  # pragma: no cover - not used in these tests
        raise RuntimeError("Query not supported in dummy session")


def test_vat_calculator_standard():
    result = VATCalculator.calculate(Decimal("1075.00"), "standard")
    assert result["vat_rate"] == Decimal("7.5")
    # VAT amount should be a portion of inclusive total
    assert result["vat_amount"] > 0
    assert result["total"] == Decimal("1075.00")


@pytest.mark.parametrize(
    "desc,expected",
    [
        ("Bread and milk", "zero_rated"),
        ("Insurance premium", "exempt"),
        ("Export shipment", "export"),
        ("Generic service", "standard"),
    ],
)
def test_detect_category(desc, expected):
    assert VATCalculator.detect_category(desc) == expected


def test_fiscal_code_generation():
    inv = DummyInvoice()
    code = FiscalCodeGenerator.generate(inv)
    assert code.startswith("NGR-")
    assert len(code.split("-")) == 5


def test_service_initialization():
    svc = FiscalizationService(DummySession())
    assert svc.vat_calculator is not None
    assert svc.code_generator is not None
    assert svc.qr_generator is not None
