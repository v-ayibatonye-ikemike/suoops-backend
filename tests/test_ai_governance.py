from __future__ import annotations

import datetime as dt
import json
from decimal import Decimal

import pytest
from pydantic import BaseModel

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.core.config import settings
from app.core.security import create_access_token
from app.models import models
from app.models.admin_models import AdminUser
from app.models.ai_models import (
    AICopilotBriefing,
    AIFeatureControl,
    AIFeedback,
    AITenantPreference,
    AIUsageEvent,
)
from app.services.ai.gateway import AIGateway, AIUnavailableError
from app.services.ai.governance import (
    ai_access_allowed,
    governance_overview,
    record_feedback,
    update_feature_control,
    update_tenant_preferences,
)
from app.services.ai.retention import purge_expired_ai_data
from app.services.ai.types import AICompletion, AIMessage, AIRequest


class GovernanceOutput(BaseModel):
    message: str


class GovernanceProvider:
    name = "governance-test"

    def __init__(self) -> None:
        self.calls = 0

    async def complete(self, **kwargs):
        self.calls += 1
        return AICompletion(
            content=json.dumps({"message": "Allowed"}),
            provider=self.name,
            model="test-model",
            input_tokens=10,
            output_tokens=2,
        )


@pytest.fixture
def governance_user(db_session):
    user = models.User(
        name="Governed Merchant",
        email="governed@example.com",
        phone="+2348160000800",
    )
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture(autouse=True)
def enable_governance_ai(monkeypatch):
    monkeypatch.setattr(settings, "AI_ENABLED", True)
    monkeypatch.setattr(settings, "AI_MONTHLY_INCLUDED_OPERATIONS", 30)


def _request() -> AIRequest:
    return AIRequest(
        feature="daily_briefing",
        prompt_version="governance-v1",
        messages=[AIMessage(role="user", content="Summarise verified facts.")],
    )


@pytest.mark.asyncio
async def test_tenant_opt_out_blocks_provider_and_records_governance_decision(
    db_session, governance_user
):
    update_tenant_preferences(
        db_session,
        data_owner_id=governance_user.id,
        actor_user_id=governance_user.id,
        enabled=False,
        feature_overrides={},
    )
    provider = GovernanceProvider()

    with pytest.raises(AIUnavailableError):
        await AIGateway(db_session, provider=provider).generate_structured(
            _request(),
            GovernanceOutput,
            actor_user_id=governance_user.id,
            data_owner_id=governance_user.id,
        )

    assert provider.calls == 0
    event = db_session.query(AIUsageEvent).one()
    assert event.status == "blocked"
    assert event.error_code == "tenant_ai_disabled"
    assert event.counts_toward_quota is False


def test_rollout_and_allowlist_are_deterministic(db_session, governance_user):
    admin = AdminUser(
        email="rollout-admin@suoops.com",
        name="Rollout Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
    )
    db_session.add(admin)
    db_session.commit()
    update_feature_control(
        db_session,
        feature="daily_briefing",
        admin_user_id=admin.id,
        enabled=True,
        rollout_percent=0,
        allowlisted_owner_ids=[],
        reason="Canary rollout",
    )
    assert ai_access_allowed(
        db_session,
        feature="daily_briefing",
        data_owner_id=governance_user.id,
    ) == (False, "outside_rollout")

    update_feature_control(
        db_session,
        feature="daily_briefing",
        admin_user_id=admin.id,
        enabled=True,
        rollout_percent=0,
        allowlisted_owner_ids=[governance_user.id],
        reason="Canary rollout",
    )
    assert ai_access_allowed(
        db_session,
        feature="daily_briefing",
        data_owner_id=governance_user.id,
    ) == (True, None)


def test_feedback_and_governance_overview(db_session, governance_user):
    record_feedback(
        db_session,
        data_owner_id=governance_user.id,
        actor_user_id=governance_user.id,
        feature="daily_briefing",
        sentiment="negative",
        reason_code="not_useful",
        comment="The answer was too broad.",
        context_id="dashboard",
    )
    db_session.add(
        AIUsageEvent(
            operation_id="governance-event",
            data_owner_id=governance_user.id,
            actor_user_id=governance_user.id,
            feature="daily_briefing",
            provider="fake",
            model="fake-model",
            prompt_version="v1",
            status="succeeded",
            input_hash="a" * 64,
            input_tokens=100,
            output_tokens=20,
            estimated_cost_usd=Decimal("0.002"),
            duration_ms=250,
        )
    )
    db_session.commit()

    overview = governance_overview(db_session, days=30)
    briefing = next(item for item in overview["features"] if item["feature"] == "daily_briefing")
    assert briefing["operations"] == 1
    assert briefing["success_rate"] == 100
    assert briefing["negative_feedback"] == 1
    assert overview["total_cost_usd"] == 0.002


def test_ai_retention_enforces_documented_cutoffs(db_session, governance_user):
    now = dt.datetime.now(dt.timezone.utc)
    old = now - dt.timedelta(days=600)
    db_session.add_all(
        [
            AIUsageEvent(
                operation_id="old-governance-event",
                data_owner_id=governance_user.id,
                actor_user_id=governance_user.id,
                feature="daily_briefing",
                provider="fake",
                model="fake-model",
                prompt_version="v1",
                status="succeeded",
                input_hash="b" * 64,
                created_at=old,
            ),
            AIFeedback(
                data_owner_id=governance_user.id,
                actor_user_id=governance_user.id,
                feature="daily_briefing",
                sentiment="positive",
                created_at=old,
            ),
            AICopilotBriefing(
                data_owner_id=governance_user.id,
                briefing_date=old.date(),
                facts_hash="c" * 64,
                headline="Old briefing",
                summary="Expired cached narrative",
                generated_at=now - dt.timedelta(days=31),
            ),
        ]
    )
    db_session.commit()

    preview = purge_expired_ai_data(db_session, now=now, dry_run=True)
    assert preview["usage_events"] == 1
    assert preview["feedback"] == 1
    assert preview["briefings"] == 1
    deleted = purge_expired_ai_data(db_session, now=now)
    assert deleted["usage_events"] == 1
    assert db_session.query(AIUsageEvent).count() == 0


def test_governance_apis_support_tenant_control_and_admin_monitoring(
    client, db_session, governance_user
):
    token = create_access_token(str(governance_user.id))
    headers = {"Authorization": f"Bearer {token}"}

    preference = client.patch(
        "/ai/preferences",
        headers=headers,
        json={"enabled": False, "feature_overrides": {"daily_briefing": False}},
    )
    assert preference.status_code == 200, preference.text
    assert preference.json()["enabled"] is False
    assert db_session.query(AITenantPreference).one().data_owner_id == governance_user.id

    admin = AdminUser(
        email="governance-admin@suoops.com",
        name="Governance Admin",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=True,
        can_view_users=True,
    )
    db_session.add(admin)
    db_session.commit()
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        control = client.patch(
            "/admin/ai-governance/features/daily_briefing",
            json={
                "enabled": False,
                "rollout_percent": 25,
                "allowlisted_owner_ids": [governance_user.id],
                "reason": "Incident containment",
            },
        )
        overview = client.get("/admin/ai-governance?days=30")
    finally:
        app.dependency_overrides.pop(get_current_admin, None)

    assert control.status_code == 200, control.text
    assert control.json()["enabled"] is False
    assert overview.status_code == 200, overview.text
    assert overview.json()["disabled_tenants"] == 1
    assert db_session.query(AIFeatureControl).one().reason == "Incident containment"
