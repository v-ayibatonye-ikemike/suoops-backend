from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest

from app.core.config import settings
from app.core.security import create_access_token
from app.models.ai_models import AITenantPreference
from app.models.models import Customer, Invoice, User
from app.models.schemas.web_assistant import WebAssistantQuestion
from app.models.team_models import Team, TeamMember
from app.services.ai.gateway import AIGateway, AIProviderError
from app.services.ai.web_assistant import (
    DESTINATIONS,
    NavigationSelection,
    WebAssistantService,
    _date_range,
    invoice_prefill,
)


@pytest.fixture
def merchant(db_session):
    user = User(name="Assistant Merchant", email="web-assistant@example.test", phone="+2348012345071")
    db_session.add(user)
    db_session.commit()
    return user


@pytest.fixture(autouse=True)
def no_live_ai(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", False)


def headers(user):
    return {"Authorization": f"Bearer {create_access_token(str(user.id))}"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Where do I change my bank details?", "bank_details"),
        ("Show unpaid invoices from last month", "unpaid_invoices"),
        ("What does awaiting confirmation mean?", "awaiting_confirmation"),
        ("Which products need restocking?", "inventory"),
        ("Help me set up my storefront", "storefront"),
        ("Open expenses", "expenses"),
        ("Show paid invoices", "paid_invoices"),
        ("Create an invoice for Ada", "new_invoice"),
    ],
)
async def test_common_requests_never_call_ai(db_session, merchant, message, expected):
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock()
    result = await WebAssistantService(db_session, gateway=gateway).ask(
        WebAssistantQuestion(message=message),
        actor_user_id=merchant.id,
        data_owner_id=merchant.id,
    )
    assert expected in [action.id for action in result.actions]
    assert result.ai_assisted is False
    gateway.generate_structured.assert_not_awaited()
    assert db_session.query(Invoice).count() == 0


@pytest.mark.parametrize(
    ("message", "name", "currency", "price"),
    [
        ("Create an invoice for Ada", "Ada", "NGN", None),
        ("Invoice Ada 50k for design", "Ada", "NGN", 50000),
        ("Create an invoice for Ada for NGN 15000", "Ada", "NGN", 15000),
        ("Invoice John $25.50 for design", "John", "USD", 25.5),
        ("Invoice Òlá 1,234.50 for design", "Òlá", "NGN", 1234.5),
        ("Create an invoice", None, "NGN", None),
    ],
)
def test_prefills_only_explicit_simple_details(message, name, currency, price):
    draft, notice = invoice_prefill(message)
    assert draft.customer_name == name
    assert draft.currency == currency
    assert (draft.lines[0].unit_price if draft.lines else None) == price
    assert "Nothing has been saved or sent" in notice


@pytest.mark.parametrize(
    "message",
    [
        "Invoice Ada 2 soaps at 500 each",
        "Invoice Ada -50 for design",
        "Invoice Ada 500 for design due tomorrow",
        "Invoice Ada 500 for design; 200 for printing",
        "Invoice Ada 500000000000000000000",
    ],
)
def test_ambiguous_or_unsafe_amounts_require_manual_entry(message):
    draft, notice = invoice_prefill(message)
    assert draft.customer_name is None
    assert draft.lines == []
    assert "review" in notice or "reliably extract" in notice


@pytest.mark.parametrize(
    ("today", "start", "end"),
    [
        (dt.date(2026, 1, 10), dt.date(2025, 12, 1), dt.date(2025, 12, 31)),
        (dt.date(2024, 3, 1), dt.date(2024, 2, 1), dt.date(2024, 2, 29)),
    ],
)
def test_last_month_uses_calendar_boundaries(today, start, end):
    assert _date_range("unpaid invoices from last month", today) == (start, end)


def test_context_and_ask_are_authenticated_and_drafts_are_not_saved(client, db_session, merchant):
    assert client.get("/ai/web-assistant/context").status_code == 401
    context = client.get("/ai/web-assistant/context?page=inventory", headers=headers(merchant))
    assert context.status_code == 200
    assert "stock" in context.json()["message"]
    result = client.post(
        "/ai/web-assistant/ask",
        headers=headers(merchant),
        json={"message": "Invoice Ada 50k for design", "page": "invoices"},
    )
    assert result.status_code == 200, result.text
    action = result.json()["actions"][0]
    assert action["kind"] == "invoice_draft"
    assert action["draft"]["lines"][0]["unit_price"] == 50000
    assert db_session.query(Invoice).count() == 0
    assert db_session.query(Customer).count() == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"message": "  "},
        {"message": "x" * 501},
        {"message": "help", "page": "https://example.test"},
        {"message": "help", "data_owner_id": 999},
    ],
)
def test_request_validation_rejects_invalid_or_owner_supplied_context(client, merchant, payload):
    response = client.post("/ai/web-assistant/ask", headers=headers(merchant), json=payload)
    assert response.status_code == 422


def test_member_shortcuts_respect_real_team_membership(client, db_session, merchant):
    member = User(name="Team member", email="assistant-member@example.test")
    db_session.add(member)
    db_session.flush()
    team = Team(name="Assistant team", admin_user_id=merchant.id)
    db_session.add(team)
    db_session.flush()
    db_session.add(TeamMember(team_id=team.id, user_id=member.id))
    db_session.commit()
    response = client.get("/ai/web-assistant/context?page=settings", headers=headers(member))
    assert response.status_code == 200
    keys = {action["id"] for action in response.json()["actions"]}
    assert not {key for key, value in DESTINATIONS.items() if value.owner_only} & keys
    response = client.post("/ai/web-assistant/ask", headers=headers(member), json={"message": "Change bank details"})
    assert response.status_code == 200
    assert response.json()["actions"] == []
    assert "owner or team admin" in response.json()["message"]


@pytest.mark.asyncio
async def test_ai_can_only_select_permission_filtered_actions(db_session, merchant, monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(
        return_value=NavigationSelection(
            action_ids=["https://evil.example", "bank_details", "invoices"],
        )
    )
    result = await WebAssistantService(db_session, gateway=gateway).ask(
        WebAssistantQuestion(message="Take me to the right place"),
        actor_user_id=merchant.id + 1,
        data_owner_id=merchant.id,
    )
    assert [action.id for action in result.actions] == ["invoices"]
    assert result.ai_assisted
    request = gateway.generate_structured.call_args.args[0]
    supplied = json.loads(request.messages[1].content)
    assert "bank_details" not in supplied["allowed_actions"]
    assert set(supplied) == {"request", "page", "allowed_actions"}
    assert request.feature == "web_navigation"


@pytest.mark.asyncio
async def test_ai_failure_and_opt_out_keep_navigation_available(db_session, merchant, monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(side_effect=AIProviderError("offline"))
    service = WebAssistantService(db_session, gateway=gateway)
    response = await service.ask(
        WebAssistantQuestion(message="Take me to the right place"),
        actor_user_id=merchant.id,
        data_owner_id=merchant.id,
    )
    assert response.actions
    assert "unavailable" in response.notice
    gateway.generate_structured.reset_mock()
    await service.ask(
        WebAssistantQuestion(message="Take me to the right place", allow_ai=False),
        actor_user_id=merchant.id,
        data_owner_id=merchant.id,
    )
    gateway.generate_structured.assert_not_awaited()

    db_session.add(
        AITenantPreference(
            data_owner_id=merchant.id, updated_by_user_id=merchant.id, enabled=False, feature_overrides={}
        )
    )
    db_session.commit()
    provider = MagicMock()
    provider.complete = AsyncMock()
    governed = WebAssistantService(db_session, gateway=AIGateway(db_session, provider=provider))
    response = await governed.ask(
        WebAssistantQuestion(message="Take me to the right place"),
        actor_user_id=merchant.id,
        data_owner_id=merchant.id,
    )
    assert "unavailable" in response.notice
    provider.complete.assert_not_awaited()


def test_unpaid_date_link_filters_all_records_and_excludes_other_tenants(client, db_session, merchant):
    customer = Customer(name="Invoice buyer")
    other = User(name="Another workspace", email="another-assistant@example.test")
    db_session.add_all([customer, other])
    db_session.flush()
    month = dt.date.today().replace(day=1) - dt.timedelta(days=1)
    for index, (owner, status, day) in enumerate(
        [
            (merchant.id, "pending", month),
            (merchant.id, "awaiting_confirmation", month),
            (merchant.id, "paid", month),
            (merchant.id, "cancelled", month),
            (other.id, "pending", month),
            (merchant.id, "pending", dt.date.today()),
        ]
    ):
        db_session.add(
            Invoice(
                invoice_id=f"INV-ASSIST-{index}",
                issuer_id=owner,
                customer_id=customer.id,
                amount=Decimal("100"),
                status=status,
                invoice_type="revenue",
                due_date=dt.datetime.combine(day, dt.time.min, tzinfo=dt.timezone.utc),
            )
        )
    db_session.commit()
    answer = client.post(
        "/ai/web-assistant/ask",
        headers=headers(merchant),
        json={"message": "Show unpaid invoices from last month"},
    ).json()
    query = parse_qs(urlsplit(answer["actions"][0]["href"]).query)
    response = client.get(
        "/invoices/",
        headers=headers(merchant),
        params={**{key: values[0] for key, values in query.items()}, "invoice_type": "revenue"},
    )
    assert response.status_code == 200, response.text
    assert {item["invoice_id"] for item in response.json()["items"]} == {"INV-ASSIST-0", "INV-ASSIST-1"}
    assert response.json()["total"] == 2


@pytest.mark.parametrize(
    "params",
    [
        {"start_date": "2026-02-30"},
        {"end_date": "not-a-date"},
        {"start_date": "2026-10-02", "end_date": "2026-10-01"},
    ],
)
def test_invalid_invoice_date_filters_are_not_silently_ignored(client, merchant, params):
    response = client.get("/invoices/", headers=headers(merchant), params=params)
    assert response.status_code == 422
