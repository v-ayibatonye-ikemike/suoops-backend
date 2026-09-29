"""Coverage-focused tests for app/bot/invoice_intent_processor.py.

Targets the large uncovered ranges: the ``handle`` entry point, the many
guard branches inside ``_create_invoice`` (zero amount, min-amount, missing
name, suspicious amount, currency handling, error mapping), the wallet-empty
quota path, the success + notification paths, inventory price resolution, and
the customer opt-in / paid handlers.

All external I/O (WhatsApp send, PDF, S3, email, invoice service) is mocked.
"""

from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import app.bot.invoice_intent_processor as iip
from app.bot.invoice_intent_processor import InvoiceIntentProcessor
from app.core.exceptions import (
    InvoiceBalanceExhaustedError,
    MissingBankDetailsError,
)
from app.models.models import Customer, Invoice, User

# ── Helpers ──────────────────────────────────────────────────────


def _make_processor(db=None):
    client = Mock()
    client.send_text = Mock()
    client.send_document = Mock()
    client.send_template = Mock(return_value=True)
    client.send_interactive_buttons = Mock(return_value=True)
    proc = InvoiceIntentProcessor(db=db or MagicMock(), client=client)
    return proc, client


def _parse(**entities):
    return SimpleNamespace(intent="create_invoice", entities=entities)


def _last_text(client) -> str:
    return client.send_text.call_args[0][1]


def _fake_invoice(**overrides):
    """A lightweight stand-in for a persisted Invoice ORM object."""
    defaults = dict(
        invoice_id="INV-abc123def456",
        amount=Decimal("5000"),
        currency="NGN",
        status="pending",
        due_date=None,
        pdf_url=None,
        customer=SimpleNamespace(name="Joy", id=1),
        lines=[SimpleNamespace(description="wig", quantity=1)],
        whatsapp_delivery_pending=False,
        channel="whatsapp",
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _quota(can_create=True):
    return {
        "can_create": can_create,
        "invoice_balance": 50,
        "wallet_balance_naira": 5000 if can_create else 0,
        "topup_from": 1250,
    }


# ── handle() entry point ─────────────────────────────────────────


async def test_handle_ignores_non_invoice_intent():
    proc, client = _make_processor()
    await proc.handle("234801", SimpleNamespace(intent="greeting", entities={}), {})
    client.send_text.assert_not_called()


async def test_handle_unknown_sender():
    proc, client = _make_processor()
    proc._resolve_issuer_id = Mock(return_value=None)
    await proc.handle("234801", _parse(amount=5000), {})
    assert "don't recognise" in _last_text(client)


async def test_handle_full_success_no_contact():
    proc, client = _make_processor()
    proc._resolve_issuer_id = Mock(return_value=1)
    service = Mock()
    service.check_invoice_quota = Mock(return_value=_quota(True))
    service.create_invoice = Mock(return_value=_fake_invoice())
    with patch.object(iip, "build_invoice_service", return_value=service):
        await proc.handle(
            "234801",
            _parse(customer_name="Joy", amount=5000, lines=[{"description": "wig", "quantity": 1, "unit_price": 5000}]),
            {},
        )
    # business confirmation sent
    assert service.create_invoice.called
    assert "Invoice" in _last_text(client)


# ── _enforce_quota ───────────────────────────────────────────────


async def test_handle_wallet_empty_shows_topup_buttons():
    proc, client = _make_processor()
    proc._resolve_issuer_id = Mock(return_value=1)
    service = Mock()
    service.check_invoice_quota = Mock(return_value=_quota(False))
    with patch.object(iip, "build_invoice_service", return_value=service):
        await proc.handle("234801", _parse(customer_name="Joy", amount=5000), {})
    assert client.send_interactive_buttons.called
    service.create_invoice.assert_not_called()


def test_enforce_quota_text_fallback():
    proc, client = _make_processor()
    client.send_interactive_buttons = Mock(return_value=False)
    service = Mock()
    service.check_invoice_quota = Mock(return_value=_quota(False))
    ok = proc._enforce_quota(service, 1, "234801")
    assert ok is False
    assert "wallet is empty" in _last_text(client)


def test_enforce_quota_swallows_check_error():
    proc, _ = _make_processor()
    service = Mock()
    service.check_invoice_quota = Mock(side_effect=RuntimeError("db down"))
    # On error it defaults to allowing creation.
    assert proc._enforce_quota(service, 1, "234801") is True


# ── _create_invoice guard branches ───────────────────────────────


async def test_create_invoice_zero_amount_starts_guided_flow():
    proc, client = _make_processor()
    service = Mock()
    with patch("app.bot.onboarding_flow.start_guided_invoice") as guided:
        await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 0}, {})
    assert guided.called
    service.create_invoice.assert_not_called()


async def test_create_invoice_min_amount_ngn():
    proc, client = _make_processor()
    service = Mock()
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 50}, {})
    assert "too low" in _last_text(client)
    service.create_invoice.assert_not_called()


async def test_create_invoice_min_amount_usd():
    proc, client = _make_processor()
    service = Mock()
    await proc._create_invoice(
        service,
        1,
        "234801",
        {"customer_name": "Joy", "amount": 0.5, "currency": "USD"},
        {},
    )
    assert "$0.50" in _last_text(client)
    service.create_invoice.assert_not_called()


async def test_create_invoice_missing_customer_name():
    proc, client = _make_processor()
    service = Mock()
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Customer", "amount": 5000}, {})
    assert "couldn't find a customer name" in _last_text(client)
    service.create_invoice.assert_not_called()


async def test_create_invoice_suspicious_large_amount_still_creates():
    proc, client = _make_processor()
    service = Mock()
    service.check_invoice_quota = Mock(return_value=_quota(True))
    service.create_invoice = Mock(return_value=_fake_invoice(amount=Decimal("9000000")))
    await proc._create_invoice(
        service,
        1,
        "234801",
        {
            "customer_name": "Joy",
            "amount": 9_000_000,
            "lines": [{"description": "job", "quantity": 1, "unit_price": 9_000_000}],
        },
        {},
    )
    # a heads-up warning is sent, but the invoice is still created
    assert service.create_invoice.called
    texts = " ".join(c.args[1] for c in client.send_text.call_args_list)
    assert "Heads up" in texts


async def test_create_invoice_suspicious_small_multi_item():
    proc, client = _make_processor()
    service = Mock()
    service.check_invoice_quota = Mock(return_value=_quota(True))
    service.create_invoice = Mock(return_value=_fake_invoice(amount=Decimal("300")))
    await proc._create_invoice(
        service,
        1,
        "234801",
        {
            "customer_name": "Joy",
            "amount": 300,
            "lines": [
                {"description": "a", "quantity": 1, "unit_price": 100},
                {"description": "b", "quantity": 1, "unit_price": 200},
            ],
        },
        {},
    )
    texts = " ".join(c.args[1] for c in client.send_text.call_args_list)
    assert "Heads up" in texts


# ── _create_invoice error mapping ────────────────────────────────


async def test_create_invoice_balance_exhausted_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=InvoiceBalanceExhaustedError())
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "wallet is too low" in _last_text(client)


async def test_create_invoice_missing_bank_details_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=MissingBankDetailsError())
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "bank details" in _last_text(client)


async def test_create_invoice_generic_amount_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=ValueError("Amount must be positive"))
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "valid amount" in _last_text(client)


async def test_create_invoice_generic_name_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=ValueError("customer name required"))
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "customer name" in _last_text(client).lower()


async def test_create_invoice_constraint_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=Exception("NOT-NULL constraint failed"))
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "Something was missing" in _last_text(client)


async def test_create_invoice_connection_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=Exception("connection timeout"))
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "Network issue" in _last_text(client)


async def test_create_invoice_generic_fallback_error():
    proc, client = _make_processor()
    service = Mock()
    service.create_invoice = Mock(side_effect=Exception("weird failure xyz"))
    await proc._create_invoice(service, 1, "234801", {"customer_name": "Joy", "amount": 5000}, {})
    assert "couldn't create that invoice" in _last_text(client)


# ── Success + notification paths ─────────────────────────────────


async def test_create_invoice_success_with_pdf_and_wallet_low():
    proc, client = _make_processor()
    service = Mock()
    service.check_invoice_quota = Mock(return_value={"can_create": True, "wallet_balance_naira": 100})
    service.create_invoice = Mock(return_value=_fake_invoice(pdf_url="https://x/invoice.pdf", status="paid"))
    await proc._create_invoice(
        service,
        1,
        "234801",
        {"customer_name": "Joy", "amount": 5000, "lines": [{"description": "wig", "quantity": 1, "unit_price": 5000}]},
        {},
    )
    # PDF document sent + low-wallet nudge present
    assert client.send_document.called
    body = _last_text(client)
    assert "Wallet low" in body


async def test_create_invoice_success_with_email(db_session):
    user = User(phone="+2348012345678", name="Biz", phone_verified=True, wallet_balance_kobo=10_000_000)
    db_session.add(user)
    db_session.commit()
    proc, client = _make_processor(db=db_session)
    service = Mock()
    service.check_invoice_quota = Mock(return_value=_quota(True))
    service.create_invoice = Mock(return_value=_fake_invoice())
    with patch("app.services.notification.service.NotificationService") as NS:
        NS.return_value.send_invoice_email = AsyncMock(return_value=True)
        await proc._create_invoice(
            service,
            user.id,
            "234801",
            {
                "customer_name": "Joy",
                "amount": 5000,
                "customer_email": "joy@example.com",
                "lines": [{"description": "wig", "quantity": 1, "unit_price": 5000}],
            },
            {},
        )
    assert NS.return_value.send_invoice_email.await_count == 1


# ── _notify_customer (no template → full invoice) ────────────────


def test_notify_customer_no_phone_returns_false():
    proc, _ = _make_processor()
    assert proc._notify_customer(_fake_invoice(), {}, 1) is False


def test_notify_customer_full_invoice_no_template(db_session):
    user = User(
        phone="+2348012345678",
        name="Biz",
        phone_verified=True,
        bank_name="GTB",
        account_number="123",
        account_name="Biz",
        wallet_balance_kobo=10_000_000,
    )
    db_session.add(user)
    db_session.commit()
    proc, client = _make_processor(db=db_session)
    invoice = _fake_invoice(pdf_url="https://x/i.pdf")
    with patch.object(iip.settings, "WHATSAPP_TEMPLATE_INVOICE_PAYMENT", None), patch.object(
        iip.settings, "WHATSAPP_TEMPLATE_INVOICE", None
    ):
        pending = proc._notify_customer(invoice, {"customer_phone": "08012345678"}, user.id)
    assert pending is False
    # payment link message + PDF document
    assert client.send_text.called
    assert client.send_document.called


# ── Inventory price resolution ───────────────────────────────────


async def test_resolve_prices_no_products_starts_pending_session():
    proc, client = _make_processor()
    with patch("app.services.inventory.product_service.ProductService") as PS:
        PS.return_value.list_products.return_value = ([], 0)
        result = proc._resolve_prices_from_inventory(
            1,
            [{"description": "wig", "quantity": 5}],
            "234801",
            data={"customer_name": "Joy"},
        )
    assert result is None
    assert "234801" in iip._pending_prices
    iip.clear_pending_price_session("234801")


async def test_resolve_prices_matches_inventory():
    proc, client = _make_processor()
    product = SimpleNamespace(name="Wig", selling_price=Decimal("5000"), id=9)
    with patch("app.services.inventory.product_service.ProductService") as PS:
        PS.return_value.list_products.return_value = ([product], 1)
        result = proc._resolve_prices_from_inventory(
            1,
            [{"description": "wig", "quantity": 2}],
            "234801",
            data={"customer_name": "Joy"},
        )
    assert result is not None
    assert result[0]["unit_price"] == Decimal("5000")
    assert result[0]["product_id"] == 9


async def test_resolve_prices_partial_match_prompts():
    proc, client = _make_processor()
    product = SimpleNamespace(name="Wig", selling_price=Decimal("5000"), id=9)
    with patch("app.services.inventory.product_service.ProductService") as PS:
        PS.return_value.list_products.return_value = ([product], 1)
        result = proc._resolve_prices_from_inventory(
            1,
            [
                {"description": "wig", "quantity": 2},
                {"description": "unicorn", "quantity": 1},
            ],
            "234801",
            data={"customer_name": "Joy"},
        )
    assert result is None
    assert "not all" in _last_text(client)


# ── handle_customer_optin ────────────────────────────────────────


def _seed_customer_with_invoice(db, *, pending=True, phone="+2348099998888"):
    cust = Customer(name="Joy", phone=phone)
    db.add(cust)
    db.commit()
    db.refresh(cust)
    inv = Invoice(
        invoice_id="INV-optin000000",
        issuer_id=999,
        customer_id=cust.id,
        amount=Decimal("5000"),
        status="pending",
        whatsapp_delivery_pending=pending,
        pdf_url="https://x/i.pdf",
    )
    db.add(inv)
    db.commit()
    return cust, inv


def test_optin_no_customer_returns_false(db_session):
    proc, _ = _make_processor(db=db_session)
    proc._resolve_issuer_id = Mock(return_value=None)
    assert proc.handle_customer_optin("+2348011112222") is False


def test_optin_sends_pdf(db_session):
    _seed_customer_with_invoice(db_session)
    proc, client = _make_processor(db=db_session)
    proc._resolve_issuer_id = Mock(return_value=None)
    handled = proc.handle_customer_optin("+2348099998888")
    assert handled is True
    assert client.send_document.called


def test_optin_no_pending_invoice_plain_customer(db_session):
    _seed_customer_with_invoice(db_session, pending=False)
    proc, client = _make_processor(db=db_session)
    proc._resolve_issuer_id = Mock(return_value=None)
    handled = proc.handle_customer_optin("+2348099998888")
    assert handled is True
    assert "No pending invoices" in _last_text(client)


# ── handle_customer_paid ─────────────────────────────────────────


def test_paid_no_customer(db_session):
    proc, client = _make_processor(db=db_session)
    assert proc.handle_customer_paid("+2348011112222") is True
    assert "couldn't find any invoices" in _last_text(client)


def test_paid_confirms_transfer(db_session):
    _seed_customer_with_invoice(db_session)
    proc, client = _make_processor(db=db_session)
    with patch("app.services.invoice_service.build_invoice_service") as build:
        build.return_value.confirm_transfer = Mock()
        handled = proc.handle_customer_paid("+2348099998888")
    assert handled is True
    assert "Thank you" in _last_text(client)


def test_paid_no_pending_invoice(db_session):
    cust = Customer(name="Joy", phone="+2348099998888")
    db_session.add(cust)
    db_session.commit()
    proc, client = _make_processor(db=db_session)
    handled = proc.handle_customer_paid("+2348099998888")
    assert handled is True
    assert "don't have any pending invoices" in _last_text(client)


# ── build items / payment link helpers ───────────────────────────


def test_build_items_text():
    proc, _ = _make_processor()
    invoice = _fake_invoice(
        lines=[
            SimpleNamespace(description="wig", quantity=2),
            SimpleNamespace(description="shoe", quantity=1),
        ]
    )
    text = proc._build_items_text(invoice)
    assert "2x wig" in text and "shoe" in text


def test_build_items_text_no_lines():
    proc, _ = _make_processor()
    invoice = _fake_invoice(lines=[])
    assert "Service" in proc._build_items_text(invoice)


# ── _notify_customer with template configured (full template path) ──


def test_notify_customer_template_configured(db_session):
    user = User(
        phone="+2348012345678",
        name="Biz",
        phone_verified=True,
        business_name="Biz Co",
        bank_name="GTB",
        account_number="123",
        account_name="Biz",
        wallet_balance_kobo=10_000_000,
    )
    db_session.add(user)
    db_session.commit()
    proc, client = _make_processor(db=db_session)
    invoice = _fake_invoice(pdf_url="https://x/i.pdf")
    with patch.object(iip.settings, "WHATSAPP_TEMPLATE_INVOICE_PAYMENT", "inv_pay"):
        pending = proc._notify_customer(
            invoice,
            {"customer_phone": "08012345678"},
            user.id,
        )
    # invoice_with_payment carries the PDF in a document header, so it's
    # delivered inline — nothing pending a reply.
    assert pending is False
    assert client.send_template.called


def test_notify_customer_basic_template(db_session):
    user = User(phone="+2348012345678", name="Biz", phone_verified=True, wallet_balance_kobo=10_000_000)
    db_session.add(user)
    db_session.commit()
    proc, client = _make_processor(db=db_session)
    invoice = _fake_invoice()
    with patch.object(iip.settings, "WHATSAPP_TEMPLATE_INVOICE_PAYMENT", None), patch.object(
        iip.settings, "WHATSAPP_TEMPLATE_INVOICE", "basic_tmpl"
    ):
        pending = proc._notify_customer(
            invoice,
            {"customer_phone": "08012345678"},
            user.id,
        )
    assert pending is True
    assert client.send_template.called


# ── _start_pending_price_session + handle_price_reply ────────────


def test_start_pending_price_session_prompts():
    proc, client = _make_processor()
    iip._pending_prices.clear()
    proc._start_pending_price_session(
        "234801",
        1,
        [{"description": "wig", "quantity": 5}, {"description": "shoe", "quantity": 2}],
        {"customer_name": "Tonye", "customer_phone": "08012345678"},
    )
    session = iip._pending_prices.get("234801")
    assert session is not None
    assert len(session.lines) == 2
    assert "Tonye" in _last_text(client)
    iip.clear_pending_price_session("234801")


async def test_handle_price_reply_creates_invoice():
    proc, client = _make_processor()
    iip._pending_prices["234801"] = iip.PendingPriceSession(
        user_id=42,
        lines=[{"description": "wig", "quantity": 5}, {"description": "shoe", "quantity": 10}],
        data={"customer_name": "Tonye", "customer_phone": "08012345678"},
    )
    with patch.object(proc, "_enforce_quota", return_value=True), patch.object(
        proc, "_create_invoice", new_callable=AsyncMock
    ) as mock_create, patch.object(iip, "build_invoice_service"):
        handled = await proc.handle_price_reply("234801", "5000, 3000")
    assert handled is True
    assert "234801" not in iip._pending_prices
    data = mock_create.call_args[0][3]
    assert data["amount"] == Decimal("55000")


async def test_handle_price_reply_bad_input_reprompts():
    proc, client = _make_processor()
    iip._pending_prices["234801"] = iip.PendingPriceSession(
        user_id=42,
        lines=[{"description": "wig", "quantity": 5}],
        data={"customer_name": "Tonye"},
    )
    handled = await proc.handle_price_reply("234801", "hello world")
    assert handled is True
    assert "234801" in iip._pending_prices  # kept for retry
    assert "couldn't read" in _last_text(client).lower()
    iip.clear_pending_price_session("234801")


async def test_handle_price_reply_no_session():
    proc, _ = _make_processor()
    iip._pending_prices.clear()
    assert await proc.handle_price_reply("999", "5000") is False


# ── _parse_price_reply ───────────────────────────────────────────


def test_parse_price_reply_variants():
    lines2 = [{"description": "wig", "quantity": 1}, {"description": "shoe", "quantity": 1}]
    assert InvoiceIntentProcessor._parse_price_reply("5000, 3000", lines2) == [5000.0, 3000.0]
    assert InvoiceIntentProcessor._parse_price_reply("5,000 3,000", lines2) == [5000.0, 3000.0]
    # named mapping when more numbers than items
    named = InvoiceIntentProcessor._parse_price_reply("5000 wig, 3000 shoe", lines2)
    assert named == [5000.0, 3000.0]
    # single item
    assert InvoiceIntentProcessor._parse_price_reply("5000", [{"description": "wig", "quantity": 1}]) == [5000.0]
    # unparseable
    assert InvoiceIntentProcessor._parse_price_reply("abc", lines2) is None


# ── _resolve_prices no-catalog + no data (blocking message) ──────


def test_resolve_prices_no_products_no_data_blocks():
    proc, client = _make_processor()
    with patch("app.services.inventory.product_service.ProductService") as PS:
        PS.return_value.list_products.return_value = ([], 0)
        result = proc._resolve_prices_from_inventory(
            1,
            [{"description": "wig", "quantity": 5}],
            "234801",
            data=None,
        )
    assert result is None
    assert "no prices" in _last_text(client)


def test_resolve_prices_no_match_no_data_blocks():
    proc, client = _make_processor()
    product = SimpleNamespace(name="Wig", selling_price=Decimal("5000"), id=9)
    with patch("app.services.inventory.product_service.ProductService") as PS:
        PS.return_value.list_products.return_value = ([product], 1)
        result = proc._resolve_prices_from_inventory(
            1,
            [{"description": "unicorn", "quantity": 5}],
            "234801",
            data=None,
        )
    assert result is None
    assert "couldn't find" in _last_text(client).lower()


# ── _load_issuer ─────────────────────────────────────────────────


def test_load_issuer(db_session):
    user = User(phone="+2348012345678", name="Biz", phone_verified=True, wallet_balance_kobo=10_000_000)
    db_session.add(user)
    db_session.commit()
    proc, _ = _make_processor(db=db_session)
    assert proc._load_issuer(user.id).id == user.id
    assert proc._load_issuer(999999) is None


# ── _resolve_issuer_id ───────────────────────────────────────────


def test_resolve_issuer_id_verified(db_session):
    user = User(phone="+2348012345678", name="Biz", phone_verified=True, wallet_balance_kobo=10_000_000)
    db_session.add(user)
    db_session.commit()
    proc, _ = _make_processor(db=db_session)
    assert proc._resolve_issuer_id("+2348012345678") == user.id


def test_resolve_issuer_id_auto_verifies(db_session):
    user = User(phone="+2348088887777", name="Biz", phone_verified=False, wallet_balance_kobo=10_000_000)
    db_session.add(user)
    db_session.commit()
    proc, _ = _make_processor(db=db_session)
    assert proc._resolve_issuer_id("+2348088887777") == user.id
    db_session.refresh(user)
    assert user.phone_verified is True


def test_resolve_issuer_id_none_inputs(db_session):
    proc, _ = _make_processor(db=db_session)
    assert proc._resolve_issuer_id(None) is None
    assert proc._resolve_issuer_id("+2348000000000") is None
