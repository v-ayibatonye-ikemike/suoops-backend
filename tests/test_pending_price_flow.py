"""
Test conversational price-gathering flow for quantity-only invoices.

When a user types "Invoice Tonye 08078557662, 5 wig, 10 shoe" and has NO
product catalog, the bot should ask for prices instead of blocking.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from app.bot.invoice_intent_processor import (
    InvoiceIntentProcessor,
    PendingPriceSession,
    _pending_prices,
    clear_pending_price_session,
    get_pending_price_session,
)
from app.bot.nlp_service import NLPService

# ── Helpers ───────────────────────────────────────────────────────


def _make_processor() -> tuple[InvoiceIntentProcessor, Mock]:
    """Return (processor, mock_client) with mocked DB."""
    client = Mock()
    client.send_text = Mock()
    client.send_template = Mock(return_value=True)
    db = MagicMock()
    proc = InvoiceIntentProcessor(db=db, client=client)
    return proc, client


# ── Session store tests ──────────────────────────────────────────


class TestPendingPriceSession:
    def setup_method(self):
        _pending_prices.clear()

    def teardown_method(self):
        _pending_prices.clear()

    def test_store_and_retrieve(self):
        session = PendingPriceSession(
            user_id=1,
            lines=[{"description": "wig", "quantity": 5}],
            data={"customer_name": "Tonye"},
        )
        _pending_prices["2348012345678"] = session
        assert get_pending_price_session("2348012345678") is session

    def test_missing_returns_none(self):
        assert get_pending_price_session("9999999") is None

    def test_expired_returns_none(self):
        session = PendingPriceSession(
            user_id=1,
            lines=[{"description": "wig", "quantity": 5}],
            data={},
        )
        session.created_at -= 1000  # expired (> 900s TTL)
        _pending_prices["2348012345678"] = session
        assert get_pending_price_session("2348012345678") is None
        assert "2348012345678" not in _pending_prices

    def test_clear_session(self):
        _pending_prices["2348012345678"] = PendingPriceSession(
            user_id=1,
            lines=[],
            data={},
        )
        clear_pending_price_session("2348012345678")
        assert "2348012345678" not in _pending_prices


# ── Price reply parsing tests ────────────────────────────────────


class TestParsePriceReply:
    """Test the _parse_price_reply static method."""

    lines_3 = [
        {"description": "wig", "quantity": 5},
        {"description": "shoe", "quantity": 10},
        {"description": "pack", "quantity": 20},
    ]
    lines_1 = [{"description": "wig", "quantity": 5}]

    def test_comma_separated(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000, 3000, 2000",
            self.lines_3,
        )
        assert result == [5000.0, 3000.0, 2000.0]

    def test_space_separated(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000 3000 2000",
            self.lines_3,
        )
        assert result == [5000.0, 3000.0, 2000.0]

    def test_thousand_formatted(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5,000  3,000  2,000",
            self.lines_3,
        )
        assert result == [5000.0, 3000.0, 2000.0]

    def test_thousand_formatted_comma_separated(self):
        """Handle '5,000, 3,000, 2,000' — commas as both thousands + separators."""
        result = InvoiceIntentProcessor._parse_price_reply(
            "5,000, 3,000, 2,000",
            self.lines_3,
        )
        assert result == [5000.0, 3000.0, 2000.0]

    def test_price_with_item_names(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000 wig, 3000 shoe, 2000 pack",
            self.lines_3,
        )
        assert result == [5000.0, 3000.0, 2000.0]

    def test_single_item(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000",
            self.lines_1,
        )
        assert result == [5000.0]

    def test_single_item_with_name(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000 wig",
            self.lines_1,
        )
        assert result == [5000.0]

    def test_no_numbers_returns_none(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "hello there",
            self.lines_3,
        )
        assert result is None

    def test_wrong_count_returns_none(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000, 3000",
            self.lines_3,  # 2 prices but 3 items
        )
        assert result is None

    def test_decimal_prices(self):
        result = InvoiceIntentProcessor._parse_price_reply(
            "5000.50, 3000.75, 2000.25",
            self.lines_3,
        )
        assert result == [5000.50, 3000.75, 2000.25]


# ── Start session (prompt) tests ─────────────────────────────────


class TestStartPendingPriceSession:
    def setup_method(self):
        _pending_prices.clear()

    def teardown_method(self):
        _pending_prices.clear()

    def test_stores_session_and_sends_prompt(self):
        proc, client = _make_processor()
        lines = [
            {"description": "wig", "quantity": 5},
            {"description": "shoe", "quantity": 10},
        ]
        data = {
            "customer_name": "Tonye",
            "customer_phone": "08078557662",
            "lines": lines,
            "amount": 0,
        }

        with patch(
            "app.bot.invoice_intent_processor.get_user_currency",
            return_value="NGN",
        ):
            proc._start_pending_price_session(
                "2348012345678",
                1,
                lines,
                data,
            )

        # Session stored
        session = _pending_prices.get("2348012345678")
        assert session is not None
        assert session.user_id == 1
        assert len(session.lines) == 2
        assert session.lines[0]["description"] == "wig"
        assert session.lines[0]["quantity"] == 5

        # Prompt sent
        assert client.send_text.called
        msg = client.send_text.call_args[0][1]
        assert "Tonye" in msg
        assert "Wig" in msg
        assert "Shoe" in msg
        assert "price" in msg.lower()


# ── Handle price reply tests ─────────────────────────────────────


class TestHandlePriceReply:
    def setup_method(self):
        _pending_prices.clear()

    def teardown_method(self):
        _pending_prices.clear()

    def test_no_session_returns_false(self):
        proc, _ = _make_processor()
        result = asyncio.run(
            proc.handle_price_reply("2348012345678", "5000, 3000"),
        )
        assert result is False

    def test_valid_prices_creates_invoice(self):
        proc, client = _make_processor()
        sender = "2348012345678"

        # Set up pending session
        _pending_prices[sender] = PendingPriceSession(
            user_id=42,
            lines=[
                {"description": "wig", "quantity": 5},
                {"description": "shoe", "quantity": 10},
            ],
            data={"customer_name": "Tonye", "customer_phone": "08078557662"},
        )

        with (
            patch.object(proc, "_enforce_quota", return_value=True),
            patch.object(proc, "_create_invoice", new_callable=AsyncMock) as mock_create,
            patch(
                "app.bot.invoice_intent_processor.build_invoice_service",
            ),
        ):
            result = asyncio.run(
                proc.handle_price_reply(sender, "5000, 3000"),
            )

        assert result is True
        # Session cleared
        assert sender not in _pending_prices
        # Invoice created with correct data
        assert mock_create.called
        call_data = mock_create.call_args[0][3]  # 4th arg is data
        assert call_data["amount"] == Decimal("55000")  # 5*5000 + 10*3000
        assert len(call_data["lines"]) == 2
        assert call_data["lines"][0]["unit_price"] == Decimal("5000")
        assert call_data["lines"][0]["quantity"] == 5
        assert call_data["customer_name"] == "Tonye"

    def test_bad_prices_sends_hint(self):
        proc, client = _make_processor()
        sender = "2348012345678"

        _pending_prices[sender] = PendingPriceSession(
            user_id=42,
            lines=[
                {"description": "wig", "quantity": 5},
                {"description": "shoe", "quantity": 10},
            ],
            data={"customer_name": "Tonye", "customer_phone": "08078557662"},
        )

        result = asyncio.run(
            proc.handle_price_reply(sender, "hello world"),
        )

        assert result is True
        # Session NOT cleared (user can retry)
        assert sender in _pending_prices
        # Hint sent
        msg = client.send_text.call_args[0][1]
        assert "couldn't read" in msg.lower()


# ── No-catalog triggers price session (integration) ─────────────


class TestNoCatalogTriggersSession:
    def setup_method(self):
        _pending_prices.clear()

    def teardown_method(self):
        _pending_prices.clear()

    def test_no_catalog_starts_price_session(self):
        """When user has no products, _resolve_prices_from_inventory
        should start a pending-price session instead of blocking."""
        proc, client = _make_processor()
        sender = "2348012345678"
        lines = [
            {"description": "wig", "quantity": 5},
            {"description": "shoe", "quantity": 10},
        ]
        data = {
            "customer_name": "Tonye",
            "customer_phone": "08078557662",
            "lines": lines,
            "amount": 0,
        }

        with (
            patch(
                "app.services.inventory.product_service.ProductService",
            ) as MockProdSvc,
            patch(
                "app.bot.invoice_intent_processor.get_user_currency",
                return_value="NGN",
            ),
        ):
            # Simulate empty catalog
            MockProdSvc.return_value.list_products.return_value = ([], 0)

            result = proc._resolve_prices_from_inventory(
                issuer_id=42,
                lines=lines,
                sender=sender,
                data=data,
            )

        assert result is None
        # Session was stored
        session = _pending_prices.get(sender)
        assert session is not None
        assert session.user_id == 42
        assert len(session.lines) == 2
        # Prompt was sent
        assert client.send_text.called
        msg = client.send_text.call_args[0][1]
        assert "price" in msg.lower()


# ── NLP → price session end-to-end ───────────────────────────────


class TestNLPQuantityOnlyWithoutCatalog:
    """Test that 'Invoice Tonye 08078557662, 5 wig, 10 shoe' parses correctly
    and the quantity-only items trigger the price-gathering flow."""

    def test_nlp_detects_quantity_only_items(self):
        nlp = NLPService()
        parse = nlp.parse_text(
            "Invoice Tonye 08078557662, 5 wig, 10 shoe, 20 pack",
            is_speech=False,
        )
        assert parse.intent == "create_invoice"
        lines = parse.entities.get("lines", [])
        # Should detect quantity-only items
        qty_only = [line for line in lines if line.get("unit_price") is None]
        assert len(qty_only) >= 2, f"Expected quantity-only items, got {lines}"
