from __future__ import annotations

import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.core.config import settings
from app.models import models
from app.models.admin_models import AdminUser
from app.services.ai.disputes import DisputeAssistantService, DisputeNarrativeOut


@pytest.fixture
def disputed_order(db_session):
    seller = models.User(
        name="Evidence Seller",
        business_name="Evidence Store",
        email="evidence-seller@example.com",
        phone="+2348160000700",
        flagged_for_review=True,
        circumvention_attempts=1,
    )
    customer = models.Customer(name="Evidence Buyer", phone="+2348160000701")
    db_session.add_all([seller, customer])
    db_session.flush()
    paid_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    invoice = models.Invoice(
        invoice_id="INV-DISPUTE-AI",
        issuer_id=seller.id,
        customer_id=customer.id,
        amount=Decimal("15000"),
        status="paid",
        invoice_type="revenue",
        channel="storefront",
        paid_at=paid_at,
        notes="Deliver to verified Lagos destination.",
    )
    db_session.add(invoice)
    db_session.flush()
    db_session.add(
        models.InvoiceLine(
            invoice_id=invoice.id,
            description="Verified bag",
            quantity=1,
            unit_price=Decimal("15000"),
        )
    )
    disputed_at = paid_at + dt.timedelta(days=1)
    escrow = models.StorefrontOrderEscrow(
        invoice_id=invoice.id,
        seller_id=seller.id,
        status="disputed",
        gross_kobo=1_500_000,
        fee_kobo=45_000,
        payout_kobo=1_500_000,
        seller_dispatched_at=paid_at + dt.timedelta(hours=2),
        dispatch_tracking="TRACK-123",
        dispatch_proof_url="https://example.com/dispatch.png",
        courier_delivered_at=paid_at + dt.timedelta(hours=20),
        delivery_status="delivered",
        delivery_status_at=paid_at + dt.timedelta(hours=20),
        disputed_at=disputed_at,
        dispute_reason="The order was not delivered",
        held_for_review=True,
        review_reason="shared IP",
        requires_delivery=True,
        created_at=paid_at - dt.timedelta(hours=1),
    )
    db_session.add(escrow)
    db_session.flush()
    db_session.add_all(
        [
            models.OrderMessage(
                escrow_id=escrow.id,
                sender_role="buyer",
                body_raw="Where is my order?",
                body_redacted="Where is my order?",
            ),
            models.OrderMessage(
                escrow_id=escrow.id,
                sender_role="seller",
                sender_user_id=seller.id,
                body_raw="Pay me directly at 08000000000",
                body_redacted="Pay me directly at [PHONE]",
                flagged=True,
                blocked=True,
                flag_reasons="off_platform_payment",
            ),
            models.BuyerReputation(
                phone="+2348160000701",
                disputes=2,
                false_disputes=1,
                flagged=True,
            ),
        ]
    )
    db_session.commit()
    db_session.refresh(escrow)
    return seller, invoice, escrow


@pytest.mark.asyncio
async def test_dispute_assistant_reconstructs_evidence_without_deciding_money(
    db_session, disputed_order, monkeypatch
):
    _seller, invoice, escrow = disputed_order
    monkeypatch.setattr(settings, "AI_DISPUTE_ASSISTANT_ENABLED", False)

    result = await DisputeAssistantService(db_session).analyse(escrow.id, admin_user_id=99)

    assert result["invoice_id"] == invoice.invoice_id
    assert [event["event"] for event in result["timeline"]] == [
        "Order created",
        "Payment confirmed",
        "Seller marked dispatched",
        "Courier status updated",
        "Courier marked delivered",
        "Buyer reported a problem",
    ]
    assert any("conflicts with an integrated courier" in flag for flag in result["review_flags"])
    assert any("blocked" in flag for flag in result["review_flags"])
    assert "human administrator" in result["decision_notice"]
    assert "refund" not in result["neutral_summary"].lower()
    assert "release" not in result["neutral_summary"].lower()


@pytest.mark.asyncio
async def test_dispute_ai_gets_redacted_evidence_and_cannot_execute_action(
    db_session, disputed_order, monkeypatch
):
    seller, _invoice, escrow = disputed_order
    monkeypatch.setattr(settings, "AI_DISPUTE_ASSISTANT_ENABLED", True)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(
        return_value=DisputeNarrativeOut(
            neutral_summary="The buyer reports non-delivery while courier delivery evidence is recorded.",
            reviewer_questions=["Does the courier event match this order and destination?"],
        )
    )

    result = await DisputeAssistantService(db_session, gateway=gateway).analyse(
        escrow.id,
        admin_user_id=44,
    )

    assert result["ai_generated"] is True
    request = gateway.generate_structured.await_args.args[0]
    assert "+2348160000701" not in request.messages[1].content
    assert "08000000000" not in request.messages[1].content
    assert "verified Lagos destination" not in request.messages[1].content
    assert gateway.generate_structured.await_args.kwargs == {
        "actor_admin_user_id": 44,
        "data_owner_id": seller.id,
        "enforce_owner_quota": False,
    }
    assert escrow.status == "disputed"


@pytest.mark.asyncio
async def test_dispute_assistant_rejects_ai_outcome_recommendations(
    db_session, disputed_order, monkeypatch
):
    _seller, _invoice, escrow = disputed_order
    monkeypatch.setattr(settings, "AI_DISPUTE_ASSISTANT_ENABLED", True)
    gateway = MagicMock()
    gateway.generate_structured = AsyncMock(
        return_value=DisputeNarrativeOut(
            neutral_summary="The evidence means SuoOps should refund the buyer.",
            reviewer_questions=[],
        )
    )

    result = await DisputeAssistantService(db_session, gateway=gateway).analyse(
        escrow.id,
        admin_user_id=44,
    )

    assert result["ai_generated"] is False
    assert "rejected" in result["generation_notice"]
    assert "should refund" not in result["neutral_summary"].lower()


def test_admin_dispute_assistant_endpoint_is_read_only(
    client, db_session, disputed_order, monkeypatch
):
    _seller, _invoice, escrow = disputed_order
    admin = AdminUser(
        email="dispute-ai-admin@suoops.com",
        name="Dispute AI Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_view_users=True,
    )
    db_session.add(admin)
    db_session.commit()
    monkeypatch.setattr(settings, "AI_DISPUTE_ASSISTANT_ENABLED", False)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        response = client.post(f"/admin/disputes/{escrow.id}/assistant")
    finally:
        app.dependency_overrides.pop(get_current_admin, None)

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "disputed"
    db_session.refresh(escrow)
    assert escrow.status == "disputed"
    assert escrow.refunded_at is None
    assert escrow.released_at is None
