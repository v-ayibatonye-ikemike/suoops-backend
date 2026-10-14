from __future__ import annotations

from decimal import Decimal
from unittest.mock import AsyncMock, Mock

import pytest

from app.bot import onboarding_flow as onboarding
from app.bot.invoice_intent_processor import clear_pending_price_session, get_pending_price_session
from app.bot.nlp_service import NLPService
from app.bot.product_invoice_flow import CartItem, CartSession, get_cart, save_cart
from app.bot.whatsapp_adapter import WhatsAppHandler
from app.models.models import User


@pytest.fixture
def bot(db_session, monkeypatch):
    monkeypatch.setattr(onboarding, "_redis", lambda: None)
    onboarding._sessions.clear()
    user = User(name="Ada", phone="+2348012345678", phone_verified=True)
    db_session.add(user)
    db_session.commit()
    clear_pending_price_session(user.phone)
    handler = WhatsAppHandler(client=Mock(), nlp=NLPService(), db=db_session)
    handler.nlp.parse_text = Mock(wraps=handler.nlp.parse_text)
    monkeypatch.setattr(handler.invoice_processor, "_resolve_issuer_id", lambda sender: user.id)
    handler.invoice_processor._create_invoice = AsyncMock()
    handler.invoice_processor.handle = AsyncMock()
    handler.expense_processor.handle = AsyncMock(return_value=False)
    yield handler, user
    onboarding._sessions.clear()
    clear_pending_price_session(user.phone)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,method",
    [
        ("Please show me my balance?", "_send_quick_status"),
        ("How much have I collected today?", "_send_quick_status"),
        ("Who hasn't paid me?", "_send_owed_list"),
        ("Can you show me my unpaid invoices please?", "_send_owed_list"),
        ("STATSU", "_send_quick_status"),
        ("reprot", "_send_analytics"),
        ("Show me my orders, please.", "_send_order_status"),
        ("Please send my store link", "_send_storefront_link"),
        ("wallet balance", "_send_wallet"),
        ("balnce", "_send_quick_status"),
        ("Can you help me?", "_send_help_guide"),
    ],
)
async def test_natural_commands_do_not_become_draft_values(bot, monkeypatch, text, method):
    handler, user = bot
    draft = onboarding.start_onboarding(user.phone, user.id)
    respond = Mock()
    monkeypatch.setattr(handler, method, respond)
    await handler._handle_text_message(user.phone, {"text": text})
    respond.assert_called_once_with(user.phone, user.id)
    assert onboarding.get_onboarding_session(user.phone).customer_name == ""
    assert draft.step == "customer_name"
    handler.invoice_processor._create_invoice.assert_not_awaited()
    handler.nlp.parse_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["continue", "Where was I?", "what next", "help"])
async def test_resume_and_help_show_current_step_without_advancing(bot, text):
    handler, user = bot
    onboarding.start_guided_invoice(handler.client, user.phone, user.id, customer_name="Joy")
    handler.client.reset_mock()
    await handler._handle_text_message(user.phone, {"text": text})
    message = handler.client.send_text.call_args.args[1]
    assert "Joy" in message
    assert "amount" in message.lower() or "how much" in message.lower()
    assert onboarding.get_onboarding_session(user.phone).step == "amount"
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["Thanks!", "How do I get paid?", "I need support", "Why is this not working?"])
async def test_questions_and_smalltalk_cannot_replace_customer_name(bot, text):
    handler, user = bot
    onboarding.start_onboarding(user.phone, user.id)
    await handler._handle_text_message(user.phone, {"text": text})
    assert onboarding.get_onboarding_session(user.phone).customer_name == ""
    assert onboarding.get_onboarding_session(user.phone).step == "customer_name"
    handler.client.send_text.assert_called()
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancel_clears_only_drafts_and_never_submits_customer(bot):
    handler, user = bot
    save_cart(user.phone, CartSession(user_id=user.id, step="awaiting_customer"))
    await handler._handle_text_message(user.phone, {"text": "cancel draft"})
    assert get_cart(user.phone) is None
    assert "existing invoices" in handler.client.send_text.call_args.args[1]
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_product_search_works_while_cart_is_waiting_for_customer(bot, monkeypatch):
    handler, user = bot
    save_cart(user.phone, CartSession(user_id=user.id, step="awaiting_customer"))
    search = Mock()
    monkeypatch.setattr(handler.product_flow, "handle_search", search)
    await handler._handle_text_message(user.phone, {"text": "search wig"})
    search.assert_called_once_with(user.phone, user.id, "wig")
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_without_draft_is_honest(bot):
    handler, user = bot
    await handler._handle_text_message(user.phone, {"text": "continue"})
    assert "no active draft" in handler.client.send_text.call_args.args[1].lower()
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_starting_another_flow_does_not_replace_existing_draft(bot):
    handler, user = bot
    onboarding.start_guided_invoice(handler.client, user.phone, user.id, customer_name="Joy")
    await handler._handle_text_message(user.phone, {"text": "invoice"})
    assert onboarding.get_onboarding_session(user.phone).customer_name == "Joy"
    assert onboarding.get_onboarding_session(user.phone).step == "amount"
    assert "cancel" in handler.client.send_text.call_args.args[1].lower()


@pytest.mark.asyncio
async def test_customer_name_is_not_fuzzy_corrected_or_mistaken_for_thanks(bot):
    handler, user = bot
    onboarding.start_onboarding(user.phone, user.id)
    await handler._handle_text_message(user.phone, {"text": "ThankGod"})
    assert onboarding.get_onboarding_session(user.phone).customer_name == "ThankGod"


@pytest.mark.asyncio
async def test_short_help_preserves_access_to_full_guide(bot):
    handler, user = bot
    await handler._handle_text_message(user.phone, {"text": "help"})
    message = handler.client.send_text.call_args.args[1]
    assert len(message) < 1200
    assert "full guide" in message
    assert "continue" in message


@pytest.mark.asyncio
async def test_negated_request_does_not_start_invoice(bot, monkeypatch):
    handler, user = bot
    monkeypatch.setattr(handler.support_handler, "try_handle", lambda sender, text: False)
    await handler._handle_text_message(user.phone, {"text": "I don't want to create an invoice"})
    assert onboarding.get_onboarding_session(user.phone) is None
    handler.invoice_processor.handle.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_guide_and_copilot_still_work_during_a_draft(bot, monkeypatch):
    handler, user = bot
    onboarding.start_onboarding(user.phone, user.id)
    full_guide = Mock()
    copilot = AsyncMock()
    monkeypatch.setattr(handler, "_send_full_help_guide", full_guide)
    monkeypatch.setattr(handler, "_send_copilot_response", copilot)
    await handler._handle_text_message(user.phone, {"text": "full guide"})
    await handler._handle_text_message(user.phone, {"text": "what should I do today"})
    full_guide.assert_called_once_with(user.phone, user.id)
    copilot.assert_awaited_once()
    assert onboarding.get_onboarding_session(user.phone).step == "customer_name"


@pytest.mark.asyncio
async def test_review_confirmation_emoji_keeps_existing_behavior(bot, monkeypatch):
    handler, user = bot
    onboarding.start_guided_invoice(
        handler.client, user.phone, user.id, customer_name="Joy", customer_phone="08012345678",
        amount=5000, description="wig",
    )
    finalize = AsyncMock()
    monkeypatch.setattr(handler, "_finalize_onboarded_invoice", finalize)
    await handler._handle_text_message(user.phone, {"text": "✅"})
    finalize.assert_awaited_once()


@pytest.mark.asyncio
async def test_stop_nudges_does_not_discard_work(bot, monkeypatch):
    handler, user = bot
    consent = Mock()
    monkeypatch.setattr("app.bot.whatsapp_adapter.set_consent", consent)
    onboarding.start_guided_invoice(handler.client, user.phone, user.id, customer_name="Joy")
    await handler._handle_text_message(user.phone, {"text": "STOP"})
    assert consent.call_args.args[2] is False
    assert onboarding.get_onboarding_session(user.phone).customer_name == "Joy"


@pytest.mark.asyncio
async def test_invoice_command_is_not_collected_as_feedback(bot, monkeypatch):
    handler, user = bot
    monkeypatch.setattr("app.workers.tasks.feedback_tasks.is_feedback_pending", lambda sender: True)
    feedback = Mock(return_value=True)
    monkeypatch.setattr(handler, "_save_feedback", feedback)
    text = "Invoice Joy 5000 Never Back Down book"
    await handler._handle_text_message(user.phone, {"text": text})
    handler.nlp.parse_text.assert_called_once_with(text, is_speech=False, caller_currency="NGN")
    handler.invoice_processor.handle.assert_awaited_once()
    feedback.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["1 paud", "don't mark paid", "mark paid", "cancel invoice"])
async def test_ambiguous_financial_actions_never_autocorrect_into_mutations(bot, monkeypatch, text):
    handler, user = bot
    pay = Mock()
    cancel = Mock()
    monkeypatch.setattr(handler, "_owed_action_mark_paid", pay)
    monkeypatch.setattr(handler, "_owed_action_cancel", cancel)
    await handler._handle_text_message(user.phone, {"text": text})
    pay.assert_not_called()
    cancel.assert_not_called()
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_product_price_question_is_not_answered_as_suoops_pricing(bot):
    handler, user = bot
    onboarding.start_onboarding(user.phone, user.id)
    await handler._handle_text_message(user.phone, {"text": "How much is a wig?"})
    assert "SuoOps Pricing" not in handler.client.send_text.call_args.args[1]
    assert onboarding.get_onboarding_session(user.phone).customer_name == ""


@pytest.mark.asyncio
async def test_empty_product_selection_gets_actionable_feedback(bot):
    handler, user = bot
    save_cart(user.phone, CartSession(user_id=user.id, step="awaiting_items"))
    await handler._handle_text_message(user.phone, {"text": "2"})
    assert "product name and quantity" in handler.client.send_text.call_args.args[1]
    assert get_cart(user.phone).step == "awaiting_items"


@pytest.mark.asyncio
async def test_draft_from_another_account_is_not_resumed_or_advanced(bot):
    handler, user = bot
    onboarding.start_onboarding(user.phone, user.id + 100)
    await handler._handle_text_message(user.phone, {"text": "Joy"})
    assert "different business account" in handler.client.send_text.call_args.args[1]
    assert onboarding.get_onboarding_session(user.phone).step == "customer_name"


@pytest.mark.asyncio
async def test_resume_pending_prices_reuses_prompt_without_parsing_or_saving(bot):
    handler, user = bot
    handler.invoice_processor._start_pending_price_session(
        user.phone, user.id, [{"description": "wig", "quantity": 2}], {"customer_name": "Joy"},
    )
    handler.client.reset_mock()
    await handler._handle_text_message(user.phone, {"text": "resume"})
    message = handler.client.send_text.call_args.args[1]
    assert "Joy" in message
    assert "price per item" in message
    assert get_pending_price_session(user.phone) is not None
    handler.invoice_processor._create_invoice.assert_not_awaited()
    handler.nlp.parse_text.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("step", ["customer_name", "amount", "phone", "review"])
async def test_resume_preserves_every_guided_step(bot, step):
    handler, user = bot
    draft = onboarding.start_onboarding(user.phone, user.id)
    draft.customer_name = "Joy"
    draft.amount = 5000
    draft.step = step
    await handler._handle_text_message(user.phone, {"text": "continue"})
    assert onboarding.get_onboarding_session(user.phone).step == step
    assert onboarding.get_onboarding_session(user.phone).customer_name == "Joy"
    if step == "review":
        handler.client.send_interactive_buttons.assert_called_once()
    else:
        handler.client.send_text.assert_called_once()
    handler.invoice_processor._create_invoice.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_cart_displays_total_and_next_step_without_consuming(bot):
    handler, user = bot
    save_cart(user.phone, CartSession(
        user_id=user.id, step="awaiting_customer",
        items=[CartItem(product_id=1, product_name="Wig", quantity=2, unit_price=Decimal("5000"))],
    ))
    await handler._handle_text_message(user.phone, {"text": "continue"})
    message = handler.client.send_text.call_args.args[1]
    assert "10,000" in message
    assert "customer" in message
    assert get_cart(user.phone).step == "awaiting_customer"
    handler.invoice_processor._create_invoice.assert_not_awaited()
