from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot.product_invoice_flow import CartItem, CartSession, get_cart, save_cart
from app.bot.whatsapp_adapter import WhatsAppHandler
from app.core.security import create_access_token
from app.models.models import Customer, Invoice, User
from app.models.team_models import Team, TeamMember
from app.services.analytics_service import calculate_cash_position


@pytest.fixture
def merchant(db_session):
    owner = User(name="Ikemike", business_name="Ikemike Store", phone="+2348012345001", phone_verified=True)
    customer = Customer(name="Ada")
    db_session.add_all([owner, customer])
    db_session.commit()
    return owner, customer


def add_invoice(db, merchant, reference, amount, *, status="pending", channel="web", currency="NGN", **fields):
    owner, customer = merchant
    invoice = Invoice(
        invoice_id=reference, issuer_id=owner.id, customer_id=customer.id,
        amount=Decimal(str(amount)), status=status, channel=channel,
        currency=currency, invoice_type="revenue", **fields,
    )
    db.add(invoice)
    db.commit()
    return invoice


def handler(db):
    return WhatsAppHandler(client=Mock(), nlp=Mock(), db=db)


def body(bot):
    return bot.client.send_text.call_args.args[1]


def test_screenshot_greeting_excludes_fourteen_abandoned_checkouts(client, db_session, merchant):
    owner, _ = merchant
    for index in range(14):
        add_invoice(db_session, merchant, f"INV-CART-{index}", 3000 if index < 13 else 8746, channel="storefront")
    response = client.get(
        "/analytics/cash-position",
        headers={"Authorization": f"Bearer {create_access_token(str(owner.id))}"},
    )
    assert response.status_code == 200
    assert response.json()["total_outstanding"] == 0
    assert response.json()["outstanding_count"] == 0
    bot = handler(db_session)
    bot._send_business_greeting(owner.phone, owner.id)
    assert "47,746" not in body(bot)
    assert "No outstanding" in body(bot)


def test_greeting_status_and_owed_share_receivable_rules(db_session, merchant, monkeypatch):
    owner, _ = merchant
    add_invoice(db_session, merchant, "INV-REAL-1", 1000)
    add_invoice(db_session, merchant, "INV-REAL-2", 500, status="awaiting_confirmation", channel="storefront")
    add_invoice(db_session, merchant, "INV-DROPOFF", 20000, channel="storefront")
    add_invoice(db_session, merchant, "INV-CANCELLED", 900, status="cancelled")
    saved = Mock(return_value=True)
    monkeypatch.setattr("app.bot.owed_list_session.save_owed_list", saved)
    bot = handler(db_session)
    bot._send_business_greeting(owner.phone, owner.id)
    assert "₦1,500" in body(bot)
    bot._send_quick_status(owner.phone, owner.id)
    assert "₦1,500" in body(bot)
    assert "Wallet low" not in body(bot)
    bot._send_owed_list(owner.phone, owner.id)
    assert set(saved.call_args.args[1]) == {"INV-REAL-1", "INV-REAL-2"}


def test_team_member_reads_same_workspace_as_web_but_cannot_mark_paid(db_session, merchant, monkeypatch):
    owner, _ = merchant
    invoice = add_invoice(db_session, merchant, "INV-SHARED", 2500)
    member = User(name="Member", phone="+2348012345002", phone_verified=True)
    db_session.add(member)
    db_session.flush()
    team = Team(name="Team", admin_user_id=owner.id)
    db_session.add(team)
    db_session.flush()
    db_session.add(TeamMember(team_id=team.id, user_id=member.id))
    db_session.commit()
    bot = handler(db_session)
    bot._send_business_greeting(member.phone, member.id)
    assert "₦2,500" in body(bot)
    bot._send_quick_status(member.phone, member.id)
    assert "₦2,500" in body(bot)
    monkeypatch.setattr("app.bot.owed_list_session.get_owed_list", lambda phone: [invoice.invoice_id])
    monkeypatch.setattr(bot.invoice_processor, "_resolve_issuer_id", lambda sender: member.id)
    pay = Mock()
    monkeypatch.setattr(bot, "_owed_action_mark_paid", pay)
    assert bot._maybe_handle_owed_list_reply(member.phone, "1 paid")
    pay.assert_not_called()
    assert invoice.status == "pending"


def test_mixed_currency_cash_is_normalized_before_display(db_session, merchant, monkeypatch):
    owner, _ = merchant
    monkeypatch.setattr("app.services.analytics_service.get_conversion_rate", lambda currency: Decimal("1000"))
    monkeypatch.setattr("app.utils.currency_fmt._get_rate", lambda: Decimal("1000"))
    add_invoice(db_session, merchant, "INV-NGN", 3000)
    add_invoice(db_session, merchant, "INV-USD", 10, currency="USD")
    assert calculate_cash_position(db_session, owner.id)["total_outstanding"] == 13000
    owner.preferred_currency = "USD"
    db_session.commit()
    bot = handler(db_session)
    bot._send_business_greeting(owner.phone, owner.id)
    assert "$13" in body(bot)
    assert "$3,010" not in body(bot)
    bot._send_quick_status(owner.phone, owner.id)
    assert "$13" in body(bot)
    assert "₦" not in body(bot)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "method"),
    [("report", "_send_analytics"), ("status", "_send_quick_status"), ("owed", "_send_owed_list"),
     ("hey", "_send_business_greeting")],
)
async def test_read_commands_do_not_become_customer_names(db_session, merchant, monkeypatch, command, method):
    owner, _ = merchant
    save_cart(owner.phone, CartSession(
        user_id=owner.id, step="awaiting_customer", items=[CartItem(1, "Soap", 1, Decimal("500"))],
    ))
    bot = handler(db_session)
    response = Mock()
    monkeypatch.setattr(bot, method, response)
    monkeypatch.setattr(bot.invoice_processor, "_resolve_issuer_id", lambda sender: owner.id)
    create = AsyncMock()
    monkeypatch.setattr(bot.invoice_processor, "_create_invoice", create)
    await bot._handle_text_message(owner.phone, {"text": command})
    response.assert_called_once()
    create.assert_not_awaited()
    assert get_cart(owner.phone).step == "awaiting_customer"


def test_numbered_invoice_selection_does_not_mark_paid(db_session, merchant, monkeypatch):
    owner, _ = merchant
    invoice = add_invoice(db_session, merchant, "INV-SELECT", 1000)
    monkeypatch.setattr("app.bot.owed_list_session.get_owed_list", lambda phone: [invoice.invoice_id])
    bot = handler(db_session)
    monkeypatch.setattr(bot.invoice_processor, "_resolve_issuer_id", lambda sender: owner.id)
    pay = Mock()
    monkeypatch.setattr(bot, "_owed_action_mark_paid", pay)
    assert bot._maybe_handle_owed_list_reply(owner.phone, "1")
    pay.assert_not_called()
    db_session.refresh(invoice)
    assert invoice.status == "pending"
    assert invoice.invoice_id in body(bot)


def test_cash_today_uses_wat_boundaries(db_session, merchant, monkeypatch):
    from app.services import analytics_service

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 14, 23, 30, tzinfo=dt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(analytics_service, "datetime", FrozenDateTime)
    owner, _ = merchant
    add_invoice(
        db_session, merchant, "INV-WAT-TODAY", 2500, status="paid",
        paid_at=dt.datetime(2026, 10, 14, 23, 10, tzinfo=dt.timezone.utc),
    )
    add_invoice(
        db_session, merchant, "INV-WAT-YESTERDAY", 1000, status="paid",
        paid_at=dt.datetime(2026, 10, 14, 22, 59, tzinfo=dt.timezone.utc),
    )
    assert calculate_cash_position(db_session, owner.id)["cash_collected_today"] == 2500


@pytest.mark.asyncio
async def test_selecting_owed_invoice_does_not_submit_an_unfinished_cart(db_session, merchant, monkeypatch):
    owner, _ = merchant
    invoice = add_invoice(db_session, merchant, "INV-VIEW", 1000)
    save_cart(owner.phone, CartSession(user_id=owner.id, step="awaiting_customer"))
    monkeypatch.setattr("app.bot.owed_list_session.get_owed_list", lambda phone: [invoice.invoice_id])
    bot = handler(db_session)
    monkeypatch.setattr(bot.invoice_processor, "_resolve_issuer_id", lambda sender: owner.id)
    create = AsyncMock()
    monkeypatch.setattr(bot.invoice_processor, "_create_invoice", create)
    await bot._handle_text_message(owner.phone, {"text": "1"})
    assert invoice.invoice_id in body(bot)
    assert "No changes" in body(bot)
    assert get_cart(owner.phone).step == "awaiting_customer"
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_typo_opens_guided_web_workflow_not_an_invoice(db_session, merchant, monkeypatch):
    owner, _ = merchant
    bot = handler(db_session)
    monkeypatch.setattr(bot.invoice_processor, "_resolve_issuer_id", lambda sender: owner.id)
    await bot._handle_text_message(owner.phone, {"text": "I want to add produccts"})
    assert "/dashboard/inventory?create=1&assistant=product" in body(bot)
    assert "Nothing has been saved" in body(bot)
    assert db_session.query(Invoice).count() == 0


@pytest.mark.parametrize("status,channel", [("paid", "web"), ("cancelled", "web"), ("pending", "storefront")])
def test_stale_owed_actions_do_not_change_non_receivables(db_session, merchant, monkeypatch, status, channel):
    owner, _ = merchant
    invoice = add_invoice(db_session, merchant, "INV-STALE", 1000, status=status, channel=channel)
    monkeypatch.setattr("app.bot.owed_list_session.get_owed_list", lambda phone: [invoice.invoice_id])
    bot = handler(db_session)
    monkeypatch.setattr(bot.invoice_processor, "_resolve_issuer_id", lambda sender: owner.id)
    pay = Mock()
    monkeypatch.setattr(bot, "_owed_action_mark_paid", pay)
    assert bot._maybe_handle_owed_list_reply(owner.phone, "1 paid")
    pay.assert_not_called()
    assert "no longer outstanding" in body(bot)


def test_failed_list_storage_does_not_offer_unsafe_numbered_actions(db_session, merchant, monkeypatch):
    owner, _ = merchant
    add_invoice(db_session, merchant, "INV-PENDING", 1000)
    monkeypatch.setattr("app.bot.owed_list_session.save_owed_list", lambda phone, ids: False)
    bot = handler(db_session)
    bot._send_owed_list(owner.phone, owner.id)
    assert "couldn't prepare safe numbered actions" in body(bot)
    assert "1 paid" not in body(bot)


def test_unavailable_exchange_rate_does_not_fabricate_a_zero_balance(db_session, merchant, monkeypatch):
    owner, _ = merchant
    add_invoice(db_session, merchant, "INV-USD-ERROR", 10, currency="USD")
    monkeypatch.setattr("app.services.analytics_service.get_conversion_rate", lambda currency: Decimal("0"))
    bot = handler(db_session)
    with pytest.raises(ValueError, match="valid exchange rate"):
        bot._send_quick_status(owner.phone, owner.id)
    bot.client.send_text.assert_not_called()


def test_seven_day_windows_do_not_include_an_eighth_day(db_session, merchant, monkeypatch):
    from app.services import analytics_service

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime(2026, 10, 15, 12, tzinfo=dt.timezone.utc).astimezone(tz)

    monkeypatch.setattr(analytics_service, "datetime", FrozenDateTime)
    owner, _ = merchant
    add_invoice(db_session, merchant, "INV-WEEK-IN", 1000, status="paid", paid_at=dt.datetime(2026, 10, 8, 23))
    add_invoice(db_session, merchant, "INV-WEEK-OUT", 2000, status="paid", paid_at=dt.datetime(2026, 10, 8, 22, 59))
    add_invoice(db_session, merchant, "INV-DUE-IN", 3000, due_date=dt.datetime(2026, 10, 21, 22, 59))
    add_invoice(db_session, merchant, "INV-DUE-OUT", 4000, due_date=dt.datetime(2026, 10, 21, 23))
    cash = calculate_cash_position(db_session, owner.id)
    assert cash["cash_collected_this_week"] == 1000
    assert cash["expected_inflow_7_days"] == 3000


def test_cash_image_preserves_usd_cents(monkeypatch):
    from app.services import cash_dashboard_image

    monkeypatch.setattr("app.utils.currency_fmt._get_rate", lambda: Decimal("1000"))
    render = Mock(return_value=b"png")
    monkeypatch.setattr(cash_dashboard_image, "render_cash_dashboard_png", render)
    assert cash_dashboard_image.build_cash_snapshot_png(
        {"cash_collected_today": 10250}, "Ikemike Store", "USD",
    ) == b"png"
    assert render.call_args.kwargs["highlights"][0]["value"] == "$10.25"
