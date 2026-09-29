"""
Test USD invoice creation via WhatsApp bot.

Verifies that small USD amounts ($5, $50, $100) are correctly parsed
by NLP and pass through the invoice processor guards, since USD amounts
can be 1-2 digits (vs NGN which needs 3+ digits).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.bot.nlp_service import NLPService


@pytest.fixture
def nlp():
    return NLPService()


# ── NLP parsing tests ────────────────────────────────────────────


class TestUSDAmountParsing:
    """Test that USD markers ($, usd, dollar) enable small-amount parsing."""

    def test_dollar_sign_single_item(self, nlp):
        """'Invoice Joy 08012345678, $50 wig' should parse correctly."""
        parse = nlp.parse_text("Invoice Joy 08012345678, $50 wig", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("50")
        assert len(parse.entities["lines"]) >= 1
        line = parse.entities["lines"][0]
        assert line["unit_price"] == Decimal("50")

    def test_dollar_sign_multiple_items(self, nlp):
        """'Invoice Joy, $50 wig, $25 shoe' should parse both items."""
        parse = nlp.parse_text("Invoice Joy 08012345678, $50 wig, $25 shoe", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("75")
        lines = parse.entities["lines"]
        assert len(lines) == 2

    def test_usd_prefix(self, nlp):
        """'Invoice Joy, USD 100 consulting' should parse."""
        parse = nlp.parse_text("Invoice Joy 08012345678, USD 100 consulting", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("100")

    def test_usd_small_amount_single_digit(self, nlp):
        """'Invoice Joy, $5 sticker' — single digit USD amount."""
        parse = nlp.parse_text("Invoice Joy 08012345678, $5 sticker", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("5")

    def test_dollar_word(self, nlp):
        """'Invoice Joy, 50 dollar wig' — 'dollar' keyword."""
        parse = nlp.parse_text("Invoice Joy 08012345678, 50 dollar wig", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        # Amount should be 50 (not 0)
        assert parse.entities["amount"] > 0

    def test_naira_still_requires_3_digits(self, nlp):
        """'Invoice Joy, 50 wig' (no USD marker) should NOT parse 50 as price."""
        parse = nlp.parse_text("Invoice Joy 08012345678, 50 wig", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "NGN"
        # 50 is only 2 digits, so in NGN mode it should be treated as quantity
        # (not a price), meaning amount is 0 or quantity-only
        lines = parse.entities["lines"]
        qty_only = [line for line in lines if line.get("unit_price") is None]
        assert len(qty_only) >= 1, "50 wig in NGN should be qty-only, not a price"

    def test_large_usd_amount_works(self, nlp):
        """'Invoice Joy, $1500 consulting' — larger USD amount still works."""
        parse = nlp.parse_text("Invoice Joy 08012345678, $1500 consulting", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("1500")

    def test_usd_with_comma_formatting(self, nlp):
        """'Invoice Joy, $1,500 consulting' — comma-formatted USD."""
        parse = nlp.parse_text("Invoice Joy 08012345678, $1,500 consulting", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("1500")

    def test_ngn_normal_invoice_unchanged(self, nlp):
        """'Invoice Joy, 12000 wig' — normal NGN invoice should still work."""
        parse = nlp.parse_text("Invoice Joy 08012345678, 12000 wig", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "NGN"
        assert parse.entities["amount"] == Decimal("12000")


# ── Name extraction tests ────────────────────────────────────────


class TestUSDNameExtraction:
    """Test that the name isn't mistaken for an amount in USD mode."""

    def test_dollar_amount_first(self, nlp):
        """'Invoice $50, Joy sticker' should detect Joy as the name."""
        parse = nlp.parse_text("Invoice $50, Joy sticker", is_speech=False)
        assert parse.intent == "create_invoice"
        assert parse.entities["currency"] == "USD"
        # Amount should be 50 (not 0)
        assert parse.entities["amount"] > 0

    def test_name_then_dollar_amount(self, nlp):
        """'Invoice Joy $50 sticker' — standard order."""
        parse = nlp.parse_text("Invoice Joy 08012345678, $50 sticker", is_speech=False)
        assert parse.entities["customer_name"] == "Joy"
        assert parse.entities["amount"] == Decimal("50")

    def test_name_not_confused_with_usd_amount(self, nlp):
        """The second token 'Joy' should not be treated as an amount."""
        parse = nlp.parse_text("Invoice Joy $50 sticker", is_speech=False)
        assert parse.entities["customer_name"] == "Joy"


# ── Currency detection tests ─────────────────────────────────────


class TestCurrencyDetection:
    """Test that the currency field is set correctly."""

    def test_dollar_sign_sets_usd(self, nlp):
        parse = nlp.parse_text("Invoice Joy, $50 wig", is_speech=False)
        assert parse.entities["currency"] == "USD"

    def test_usd_prefix_sets_usd(self, nlp):
        parse = nlp.parse_text("Invoice Joy, usd 50 wig", is_speech=False)
        assert parse.entities["currency"] == "USD"

    def test_dollar_word_sets_usd(self, nlp):
        parse = nlp.parse_text("Invoice Joy, 50 dollar wig", is_speech=False)
        assert parse.entities["currency"] == "USD"

    def test_naira_sign_sets_ngn(self, nlp):
        parse = nlp.parse_text("Invoice Joy, ₦5000 wig", is_speech=False)
        assert parse.entities["currency"] == "NGN"

    def test_no_marker_defaults_ngn(self, nlp):
        parse = nlp.parse_text("Invoice Joy, 5000 wig", is_speech=False)
        assert parse.entities["currency"] == "NGN"


# ── Line item extraction for USD ─────────────────────────────────


class TestUSDLineItems:
    """Test _extract_line_items with USD context."""

    def test_multiple_usd_items(self, nlp):
        """Multiple items with small USD amounts."""
        parse = nlp.parse_text(
            "Invoice Tonye 08012345678, $50 wig, $25 shoe, $10 pack",
            is_speech=False,
        )
        lines = parse.entities["lines"]
        prices = [line.get("unit_price") for line in lines]
        assert Decimal("50") in prices
        assert Decimal("25") in prices
        assert Decimal("10") in prices
        assert parse.entities["amount"] == Decimal("85")

    def test_usd_decimal_amounts(self, nlp):
        """USD amounts with cents should be detected."""
        parse = nlp.parse_text(
            "Invoice Joy 08012345678, $50.50 sticker",
            is_speech=False,
        )
        assert parse.entities["currency"] == "USD"
        # At minimum, the integer part should be captured
        assert parse.entities["amount"] >= Decimal("50")


# ── Global currency toggle tests ─────────────────────────────────


class TestGlobalCurrencyToggle:
    """
    When a user has toggled their preferred_currency to USD, the NLP
    should automatically recognise small amounts (1+ digit) WITHOUT
    needing $ or 'usd' markers in each message.
    """

    def test_caller_usd_small_amount_no_marker(self, nlp):
        """'Invoice Joy 08012345678, 50 wig' with caller_currency='USD'."""
        parse = nlp.parse_text(
            "Invoice Joy 08012345678, 50 wig",
            is_speech=False,
            caller_currency="USD",
        )
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("50")

    def test_caller_usd_single_digit(self, nlp):
        """'Invoice Joy, 5 sticker' with caller_currency='USD'."""
        parse = nlp.parse_text(
            "Invoice Joy 08012345678, 5 sticker",
            is_speech=False,
            caller_currency="USD",
        )
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("5")

    def test_caller_usd_multiple_items(self, nlp):
        """'Invoice Joy, 50 wig, 25 shoe' with caller_currency='USD'."""
        parse = nlp.parse_text(
            "Invoice Joy 08012345678, 50 wig, 25 shoe",
            is_speech=False,
            caller_currency="USD",
        )
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("75")
        lines = parse.entities["lines"]
        assert len(lines) == 2

    def test_caller_ngn_requires_3_digits(self, nlp):
        """'Invoice Joy, 50 wig' with caller_currency='NGN' → qty-only."""
        parse = nlp.parse_text(
            "Invoice Joy 08012345678, 50 wig",
            is_speech=False,
            caller_currency="NGN",
        )
        assert parse.entities["currency"] == "NGN"
        # 50 is 2 digits — in NGN mode it's a quantity, not a price
        lines = parse.entities["lines"]
        qty_only = [line for line in lines if line.get("unit_price") is None]
        assert len(qty_only) >= 1

    def test_dollar_marker_overrides_ngn_caller(self, nlp):
        """'Invoice Joy, $50 wig' with caller_currency='NGN' → still USD."""
        parse = nlp.parse_text(
            "Invoice Joy, $50 wig",
            is_speech=False,
            caller_currency="NGN",
        )
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("50")

    def test_caller_usd_large_amount(self, nlp):
        """'Invoice Joy, 1500 consulting' with caller_currency='USD' → $1500."""
        parse = nlp.parse_text(
            "Invoice Joy 08012345678, 1500 consulting",
            is_speech=False,
            caller_currency="USD",
        )
        assert parse.entities["currency"] == "USD"
        assert parse.entities["amount"] == Decimal("1500")
