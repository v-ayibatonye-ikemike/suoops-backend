"""Coverage-focused tests for app/bot/onboarding_flow.py.

Drives the guided first-invoice state machine (customer_name → amount →
phone → review → done) with a mocked WhatsApp client. Redis is patched to
None so all session state stays in the in-memory fallback dict, keeping the
tests fast and deterministic.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import pytest

import app.bot.onboarding_flow as ob
from app.bot.onboarding_flow import (
    OnboardingSession,
    _handle_amount,
    _handle_customer_name,
    _handle_phone,
    _handle_review,
    _lookup_customer_phone,
    _money_examples,
    _money_label,
    _money_min,
    _send_review,
    clear_onboarding,
    get_onboarding_session,
    handle_onboarding_reply,
    send_onboarding_prompt,
    start_guided_invoice,
    start_onboarding,
)


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Force the in-memory fallback store (no Redis round-trips)."""
    monkeypatch.setattr(ob, "_redis", lambda: None)
    ob._sessions.clear()
    yield
    ob._sessions.clear()


def _client():
    """Mock client whose interactive-buttons call reports success."""
    c = MagicMock()
    c.send_text = Mock()
    c.send_interactive_buttons = Mock(return_value=True)
    return c


def _last_text(client) -> str:
    return client.send_text.call_args[0][1]


# ── Money helpers ────────────────────────────────────────────────


def test_money_examples_ngn_and_usd():
    assert _money_examples("NGN")[0] == "5000 wig"
    assert _money_examples("USD")[0] == "50 wig"
    # None defaults to NGN
    assert _money_examples(None)[0] == "5000 wig"


def test_money_min_ngn_and_usd():
    assert _money_min("NGN") == (100.0, "₦100")
    assert _money_min("usd") == (1.0, "$1")


def test_money_label_variants():
    # fmt_money is available in the app, so this hits the primary branch.
    assert "5,000" in _money_label(5000, "NGN")
    usd = _money_label(50, "USD")
    assert "50" in usd


# ── Customer-phone lookup ────────────────────────────────────────


def test_lookup_customer_phone_guard_clauses():
    assert _lookup_customer_phone(None, 1, "Joy") is None
    assert _lookup_customer_phone(MagicMock(), 1, "") is None
    assert _lookup_customer_phone(MagicMock(), 0, "Joy") is None


def test_lookup_customer_phone_match():
    db = MagicMock()
    row = SimpleNamespace(phone="08011112222")
    (db.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value) = row
    assert _lookup_customer_phone(db, 1, "Joy") == "08011112222"


def test_lookup_customer_phone_swallows_errors():
    db = MagicMock()
    db.query.side_effect = RuntimeError("boom")
    assert _lookup_customer_phone(db, 1, "Joy") is None


# ── Session store ────────────────────────────────────────────────


def test_start_get_and_clear_session():
    session = start_onboarding("2348010000000", user_id=7)
    assert session.user_id == 7
    assert get_onboarding_session("2348010000000") is session
    clear_onboarding("2348010000000")
    assert get_onboarding_session("2348010000000") is None


def test_get_session_expired_returns_none():
    session = start_onboarding("2348010000001", user_id=7)
    session.updated_at -= 10_000  # older than the 30-min TTL
    assert get_onboarding_session("2348010000001") is None


def test_send_onboarding_prompt():
    client = _client()
    send_onboarding_prompt(client, "234801", "Mike")
    assert "Mike" in _last_text(client)


# ── start_guided_invoice: first-prompt branches ──────────────────


def test_guided_first_prompt_missing_name():
    client = _client()
    session = start_guided_invoice(client, "234801", 1)
    assert session.step == "customer_name"
    assert "billing" in _last_text(client).lower()


def test_guided_first_prompt_missing_amount():
    client = _client()
    session = start_guided_invoice(
        client,
        "234801",
        1,
        customer_name="Joy",
    )
    assert session.step == "amount"
    assert "How much" in _last_text(client)


def test_guided_first_prompt_missing_phone():
    client = _client()
    session = start_guided_invoice(
        client,
        "234801",
        1,
        customer_name="Joy",
        amount=5000,
        description="wig",
    )
    assert session.step == "phone"
    assert "phone" in _last_text(client).lower()


def test_guided_all_filled_jumps_to_review():
    client = _client()
    session = start_guided_invoice(
        client,
        "234801",
        1,
        customer_name="Joy",
        amount=5000,
        customer_phone="08012345678",
        description="wig",
    )
    assert session.step == "review"
    # review uses interactive buttons
    assert client.send_interactive_buttons.called


def test_guided_ignores_placeholder_customer_name():
    client = _client()
    session = start_guided_invoice(client, "234801", 1, customer_name="customer")
    assert session.customer_name == ""
    assert session.step == "customer_name"


def test_guided_autocompletes_phone_from_history():
    client = _client()
    db = MagicMock()
    row = SimpleNamespace(phone="08099998888")
    (db.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value) = row
    session = start_guided_invoice(
        client,
        "234801",
        1,
        customer_name="Joy",
        amount=5000,
        db=db,
    )
    # phone auto-filled → skip straight past the phone prompt to review
    assert session.customer_phone == "08099998888"
    assert session.step == "review"


# ── _send_review: button + text fallback ─────────────────────────


def test_send_review_uses_buttons():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", amount=5000, description="wig")
    _send_review(client, "234801", session)
    assert client.send_interactive_buttons.called
    client.send_text.assert_not_called()


def test_send_review_text_fallback_when_buttons_fail():
    client = _client()
    client.send_interactive_buttons = Mock(return_value=False)
    session = OnboardingSession(user_id=1, customer_name="Joy", amount=5000, description="wig")
    _send_review(client, "234801", session)
    assert client.send_text.called
    assert "send" in _last_text(client).lower()


def test_send_review_multi_line_breakdown():
    client = _client()
    session = OnboardingSession(
        user_id=1,
        customer_name="Joy",
        amount=9000,
        lines=[
            {"description": "shoe", "quantity": 1, "unit_price": 5000},
            {"description": "belt", "quantity": 1, "unit_price": 4000},
        ],
    )
    _send_review(client, "234801", session)
    body = client.send_interactive_buttons.call_args[0][1]
    assert "shoe" in body and "belt" in body


# ── handle_onboarding_reply: cancel / skip ───────────────────────


def test_reply_cancel_clears_session():
    client = _client()
    session = OnboardingSession(user_id=1, step="amount", customer_name="Joy")
    ob._sessions["234801"] = session
    result = handle_onboarding_reply(session, client, "234801", "cancel")
    assert result is None
    assert "234801" not in ob._sessions


def test_reply_skip_at_name_cancels():
    client = _client()
    session = OnboardingSession(user_id=1, step="customer_name")
    result = handle_onboarding_reply(session, client, "234801", "skip")
    assert result is None


# ── _handle_customer_name ────────────────────────────────────────


def test_handle_customer_name_too_short():
    client = _client()
    session = OnboardingSession(user_id=1)
    _handle_customer_name(session, client, "234801", "J")
    assert "at least 2" in _last_text(client)
    assert session.step == "customer_name"


def test_handle_customer_name_digits_only():
    client = _client()
    session = OnboardingSession(user_id=1)
    _handle_customer_name(session, client, "234801", "12345")
    assert "number, not a name" in _last_text(client)


def test_handle_customer_name_valid_advances():
    client = _client()
    session = OnboardingSession(user_id=1)
    _handle_customer_name(session, client, "234801", "Joy")
    assert session.customer_name == "Joy"
    assert session.step == "amount"
    assert "How much" in _last_text(client)


def test_handle_customer_name_autocompletes_phone():
    client = _client()
    db = MagicMock()
    row = SimpleNamespace(phone="08055554444")
    (db.query.return_value.join.return_value.filter.return_value.order_by.return_value.first.return_value) = row
    session = OnboardingSession(user_id=1)
    _handle_customer_name(session, client, "234801", "Joy", db=db)
    assert session.customer_phone == "08055554444"
    assert "saved from before" in _last_text(client)


# ── _handle_amount ───────────────────────────────────────────────


def test_handle_amount_single_item():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", step="amount")
    _handle_amount(session, client, "234801", "5000 wig")
    assert session.amount == 5000
    assert session.description == "wig"
    assert session.step == "phone"


def test_handle_amount_too_low():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", step="amount")
    _handle_amount(session, client, "234801", "50")
    assert "too low" in _last_text(client)
    assert session.step == "amount"


def test_handle_amount_no_number():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", step="amount")
    _handle_amount(session, client, "234801", "just some words")
    assert "couldn't find an amount" in _last_text(client)


def test_handle_amount_with_existing_phone_jumps_to_review():
    client = _client()
    session = OnboardingSession(
        user_id=1,
        customer_name="Joy",
        step="amount",
        customer_phone="08012345678",
    )
    _handle_amount(session, client, "234801", "5000 wig")
    assert session.step == "review"
    assert client.send_interactive_buttons.called


def test_handle_amount_multi_item():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", step="amount")
    _handle_amount(session, client, "234801", "5000 shoe, 4000 belt")
    # Multi-item parsing should capture two lines totalling 9000.
    assert session.amount == 9000
    assert len(session.lines) == 2
    assert session.step == "phone"


# ── _handle_phone ────────────────────────────────────────────────


def test_handle_phone_valid():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", amount=5000, step="phone")
    _handle_phone(session, client, "234801", "08012345678")
    assert session.customer_phone == "08012345678"
    assert session.step == "review"


def test_handle_phone_skip():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", amount=5000, step="phone")
    _handle_phone(session, client, "234801", "skip")
    assert session.customer_phone == ""
    assert session.step == "review"


def test_handle_phone_invalid():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", amount=5000, step="phone")
    _handle_phone(session, client, "234801", "123")
    assert "valid phone number" in _last_text(client)
    assert session.step == "phone"


# ── _handle_review ───────────────────────────────────────────────


def test_handle_review_confirm_returns_invoice_data():
    client = _client()
    session = OnboardingSession(
        user_id=1,
        customer_name="Joy",
        amount=5000,
        description="wig",
        customer_phone="08012345678",
        step="review",
    )
    ob._sessions["234801"] = session
    result = _handle_review(session, client, "234801", "send")
    assert result is not None
    assert result["customer_name"] == "Joy"
    assert result["amount"] == 5000
    assert result["lines"][0]["unit_price"] == 5000
    assert "234801" not in ob._sessions


def test_handle_review_confirm_with_lines():
    client = _client()
    session = OnboardingSession(
        user_id=1,
        customer_name="Joy",
        amount=9000,
        step="review",
        lines=[
            {"description": "shoe", "quantity": 1, "unit_price": 5000},
            {"description": "belt", "quantity": 1, "unit_price": 4000},
        ],
    )
    result = _handle_review(session, client, "234801", "yes")
    assert len(result["lines"]) == 2


def test_handle_review_edit_resets_flow():
    client = _client()
    session = OnboardingSession(
        user_id=1,
        customer_name="Joy",
        amount=5000,
        step="review",
    )
    result = _handle_review(session, client, "234801", "edit")
    assert result is None
    assert session.step == "customer_name"
    assert session.customer_name == ""


def test_handle_review_cancel():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", step="review")
    ob._sessions["234801"] = session
    result = _handle_review(session, client, "234801", "cancel")
    assert result is None
    assert "234801" not in ob._sessions


def test_handle_review_unknown_reshows():
    client = _client()
    session = OnboardingSession(user_id=1, customer_name="Joy", step="review")
    result = _handle_review(session, client, "234801", "huh?")
    assert result is None
    assert "Tap" in _last_text(client)


# ── End-to-end via handle_onboarding_reply dispatch ──────────────


def test_full_flow_through_dispatch():
    client = _client()
    session = start_guided_invoice(client, "234801", 1)  # step=customer_name
    handle_onboarding_reply(session, client, "234801", "Joy")
    assert session.step == "amount"
    handle_onboarding_reply(session, client, "234801", "5000 wig")
    assert session.step == "phone"
    handle_onboarding_reply(session, client, "234801", "08012345678")
    assert session.step == "review"
    result = handle_onboarding_reply(session, client, "234801", "send")
    assert result is not None
    assert result["customer_name"] == "Joy"
