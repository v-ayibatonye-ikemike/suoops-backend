"""Coverage-focused tests for app/bot/product_invoice_flow.py.

Seeds real Product rows for an issuer in the in-memory SQLite DB and drives
the 3-message product→invoice flow (browse → items → customer) with a mocked
WhatsApp client.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import Mock

import pytest

from app.bot.product_invoice_flow import (
    CartItem,
    CartSession,
    ProductInvoiceFlow,
    _carts,
    _fuzzy_match_product,
    _parse_item_entries,
    clear_cart,
    get_cart,
)
from app.models.inventory_models import Product
from app.models.models import User


@pytest.fixture(autouse=True)
def _clear_carts():
    _carts.clear()
    yield
    _carts.clear()


@pytest.fixture
def issuer(db_session):
    user = User(
        phone="+2348012345678",
        name="Shop Owner",
        phone_verified=True,
        business_name="Shop",
        wallet_balance_kobo=10_000_000,
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


def _add_product(db, user_id, name, sku, price, *, stock=0, track=False):
    p = Product(
        user_id=user_id,
        sku=sku,
        name=name,
        selling_price=Decimal(str(price)),
        quantity_in_stock=stock,
        track_stock=track,
    )
    db.add(p)
    db.commit()
    db.refresh(p)
    return p


@pytest.fixture
def flow(db_session):
    return ProductInvoiceFlow(db=db_session, client=Mock())


def _last_text(flow) -> str:
    return flow.client.send_text.call_args[0][1]


# ── Pure helpers ─────────────────────────────────────────────────


def test_is_trigger():
    assert ProductInvoiceFlow.is_trigger("products") is True
    assert ProductInvoiceFlow.is_trigger("  Inventory ") is True
    assert ProductInvoiceFlow.is_trigger("invoice joy") is False


def test_parse_item_entries_variants():
    assert _parse_item_entries("3 wig, 2 shoe") == [(3, "wig"), (2, "shoe")]
    assert _parse_item_entries("wig") == [(1, "wig")]
    assert _parse_item_entries("wig 3") == [(3, "wig")]
    # space-separated multi
    assert _parse_item_entries("3 wig 2 shoe") == [(3, "wig"), (2, "shoe")]
    # large number treated as name, not quantity
    assert _parse_item_entries("5000 wig") == [(1, "5000 wig")]


def test_fuzzy_match_product():
    products = [
        ("brazilian wig", SimpleObj(name="Brazilian Wig")),
        ("red shoe", SimpleObj(name="Red Shoe")),
    ]
    assert _fuzzy_match_product("wig", products).name == "Brazilian Wig"
    assert _fuzzy_match_product("red", products).name == "Red Shoe"
    assert _fuzzy_match_product("", products) is None
    assert _fuzzy_match_product("nonexistent", products) is None


def test_fuzzy_match_starts_with_and_word():
    products = [("shoe", SimpleObj(name="Shoe")), ("brazilian wig", SimpleObj(name="Brazilian Wig"))]
    assert _fuzzy_match_product("sh", products).name == "Shoe"
    assert _fuzzy_match_product("br", products).name == "Brazilian Wig"


class SimpleObj:
    def __init__(self, name):
        self.name = name


# ── Cart session ─────────────────────────────────────────────────


def test_cart_session_total_and_summary():
    session = CartSession(user_id=1)
    session.items.append(CartItem(1, "Wig", 2, Decimal("5000")))
    assert session.total == Decimal("10000")
    assert "Wig" in session.cart_summary
    # empty cart
    empty = CartSession(user_id=1)
    assert "empty" in empty.cart_summary.lower()


def test_get_cart_expired():
    session = CartSession(user_id=1)
    session.created_at -= 10_000  # older than 900s TTL
    _carts["234801"] = session
    assert get_cart("234801") is None


def test_clear_cart():
    _carts["234801"] = CartSession(user_id=1)
    clear_cart("234801")
    assert get_cart("234801") is None


# ── start_browsing ───────────────────────────────────────────────


def test_start_browsing_no_products(flow, issuer):
    flow.start_browsing("234801", issuer.id)
    assert "no products" in _last_text(flow).lower()


def test_start_browsing_search_no_match(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id, search="zzz")
    assert "No products found" in _last_text(flow)


def test_start_browsing_lists_products(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000, stock=3, track=True)
    _add_product(db_session, issuer.id, "Shoe", "SKU2", 4000)
    flow.start_browsing("234801", issuer.id)
    body = _last_text(flow)
    assert "Wig" in body and "Shoe" in body
    session = get_cart("234801")
    assert session is not None
    assert session.step == "awaiting_items"


# ── handle_items_reply ───────────────────────────────────────────


def test_handle_items_reply_no_session(flow):
    assert flow.handle_items_reply("234801", "3 wig") is False


def test_handle_items_reply_matches(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    _add_product(db_session, issuer.id, "Shoe", "SKU2", 4000)
    flow.start_browsing("234801", issuer.id)
    handled = flow.handle_items_reply("234801", "2 wig, 1 shoe")
    assert handled is True
    session = get_cart("234801")
    assert session.step == "awaiting_customer"
    assert session.total == Decimal("14000")


def test_handle_items_reply_unmatched(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id)
    handled = flow.handle_items_reply("234801", "3 unicorn")
    assert handled is True
    assert "Couldn't find" in _last_text(flow)


def test_handle_items_reply_stock_warning(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000, stock=2, track=True)
    flow.start_browsing("234801", issuer.id)
    handled = flow.handle_items_reply("234801", "10 wig")
    assert handled is True
    assert "in stock" in _last_text(flow)


def test_handle_items_reply_merges_duplicates(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id)
    flow.handle_items_reply("234801", "2 wig")
    # go back to items step and add more of the same
    session = get_cart("234801")
    session.step = "awaiting_items"
    flow.handle_items_reply("234801", "3 wig")
    session = get_cart("234801")
    assert session.items[0].quantity == 5


# ── handle_customer_reply ────────────────────────────────────────


def test_handle_customer_reply_no_session(flow):
    assert flow.handle_customer_reply("234801", "Joy") is None


def test_handle_customer_reply_builds_invoice_data(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id)
    flow.handle_items_reply("234801", "2 wig")
    data = flow.handle_customer_reply("234801", "Joy 08012345678")
    assert data is not None
    assert data["customer_name"] == "Joy"
    # phone is normalised to intl format by the NLP extractor
    assert data["customer_phone"] == "+2348012345678"
    assert data["amount"] == Decimal("10000")
    assert data["lines"][0]["product_id"] is not None
    # cart cleared
    assert get_cart("234801") is None


def test_handle_customer_reply_more(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id)
    flow.handle_items_reply("234801", "2 wig")
    result = flow.handle_customer_reply("234801", "more")
    assert result is None
    # still has a cart after add-more (browsing keeps items)
    assert get_cart("234801") is not None


def test_handle_customer_reply_clear(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id)
    flow.handle_items_reply("234801", "2 wig")
    result = flow.handle_customer_reply("234801", "clear")
    assert result is None
    assert get_cart("234801") is None
    assert "cleared" in _last_text(flow).lower()


# ── add-more / clear / search wrappers ───────────────────────────


def test_handle_add_more_no_session(flow):
    flow.handle_add_more("234801")
    assert "expired" in _last_text(flow).lower()


def test_handle_add_more_with_session(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Wig", "SKU1", 5000)
    flow.start_browsing("234801", issuer.id)
    flow.handle_add_more("234801")
    assert "Your Products" in _last_text(flow)


def test_handle_clear_cart(flow, issuer):
    _carts["234801"] = CartSession(user_id=issuer.id)
    flow.handle_clear_cart("234801")
    assert get_cart("234801") is None


def test_handle_search(flow, issuer, db_session):
    _add_product(db_session, issuer.id, "Brazilian Wig", "SKU1", 5000)
    flow.handle_search("234801", issuer.id, "wig")
    assert "Brazilian Wig" in _last_text(flow)
