import datetime as dt
import importlib.util
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.core.config import settings
from app.core.security import create_access_token
from app.models.admin_models import AdminUser
from app.models.models import Customer, Invoice, User
from app.models.retention_models import (
    MerchantLifecycle,
    RetentionContact,
    RetentionEnrollment,
    RetentionExperiment,
)
from app.services.merchant_retention import (
    UTC,
    email_unsubscribe_token,
    pilot_protected,
    recommendation,
    record_delivery,
    record_response,
    refresh_lifecycle,
    retention_facts,
    send_retention_contact,
    set_consent,
    set_outreach_enabled,
    unsubscribe_email,
)
from app.services.retention_metrics import retention_metrics
from app.workers.celery_app import _create_celery

NOW = dt.datetime(2026, 10, 14, 9, tzinfo=UTC)


def test_retention_schedule_runs_only_monday_and_thursday_at_10_wat(monkeypatch):
    monkeypatch.setattr(settings, "ENV", "production")
    with _create_celery() as celery:
        entries = {
            name: entry for name, entry in celery.conf.beat_schedule.items()
            if entry["task"] == "retention.refresh_merchants"
        }
        assert set(entries) == {"daily-merchant-retention"}
        assert celery.conf.timezone == "UTC"
        schedule = entries["daily-merchant-retention"]["schedule"]
        assert schedule.day_of_week == {1, 4}
        monday = dt.datetime(2026, 10, 12, 9, tzinfo=UTC)
        for day in range(7):
            for minute in (-1, 0, 1):
                now = monday + dt.timedelta(days=day, minutes=minute)
                schedule.nowfun = lambda now=now: now
                due = schedule.is_due(now - dt.timedelta(minutes=1)).is_due
                assert due == (day in {0, 3} and minute == 0), now


def merchant(db, index=1, age=90):
    user = User(
        name=f"Merchant {index}",
        phone=f"+23480{index:08d}",
        email=f"merchant{index}@example.test",
        phone_verified=True,
        created_at=NOW - dt.timedelta(days=age),
    )
    db.add(user)
    db.flush()
    return user


def invoice(db, user, *, age=35, status="pending", channel="dashboard", kind="revenue", currency="NGN", paid_at=None):
    customer = Customer(name="Synthetic buyer")
    db.add(customer)
    db.flush()
    record = Invoice(
        invoice_id=f"RET-{user.id}-{customer.id}",
        issuer_id=user.id,
        customer_id=customer.id,
        amount=Decimal("185000"),
        status=status,
        channel=channel,
        invoice_type=kind,
        currency=currency,
        created_at=NOW - dt.timedelta(days=age),
        paid_at=paid_at,
        platform_fee_kobo=92500,
    )
    db.add(record)
    db.flush()
    return record


@pytest.fixture
def pilot(db_session, monkeypatch):
    monkeypatch.setattr(settings, "METRICS_EXCLUDED_EMAILS", "")
    monkeypatch.setattr(settings, "RETENTION_OUTREACH_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_UNPAID_ALERT", "unpaid_alert_test")
    monkeypatch.setattr("app.services.merchant_retention.get_retention_smtp_configs", lambda: [])
    monkeypatch.setattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "test-sender")
    monkeypatch.setattr(settings, "WHATSAPP_API_KEY", "test-key")
    admin = AdminUser(name="Pilot admin", email="pilot@suoops.com", hashed_password="unused", is_super_admin=True)
    db_session.add(admin)
    db_session.flush()
    historical = RetentionExperiment(
        id=1, created_at=NOW, created_by=admin.id, enabled=False,
        started_at=NOW, ends_at=NOW + dt.timedelta(days=30),
    )
    db_session.add(historical)
    db_session.flush()
    for index in range(1, 51):
        user = merchant(db_session, index)
        invoice(db_session, user)
        set_consent(db_session, user.id, True, "web_settings", NOW)
        db_session.add(RetentionEnrollment(
            user_id=user.id, experiment_id=1, arm="treatment" if index <= 25 else "control",
            initial_state="dormant", baseline={},
        ))
    db_session.commit()
    return historical


def test_verified_records_exclude_carts_cancelled_and_other_tenants(db_session):
    user, other = merchant(db_session), merchant(db_session, 2)
    invoice(db_session, user, age=20)
    invoice(db_session, user, age=0, channel="storefront")
    invoice(db_session, user, age=0, status="cancelled")
    invoice(db_session, other, age=0)
    facts = retention_facts(db_session, user, NOW)
    assert facts.invoices == facts.outstanding_count == 1
    assert facts.outstanding_by_currency == {"NGN": Decimal("185000")}
    assert facts.days_inactive == 20
    profile = refresh_lifecycle(db_session, facts, NOW)
    assert profile.state == "at_risk"
    action = recommendation(facts, profile, NOW)
    assert action.trigger == "reactivation_14"
    assert "185,000.00" in action.reason
    assert action.action_url == "/dashboard/collections"


def test_currency_totals_are_never_added_as_naira(db_session):
    user = merchant(db_session)
    invoice(db_session, user, currency="NGN")
    invoice(db_session, user, currency="USD")
    facts = retention_facts(db_session, user, NOW)
    action = recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW)
    assert "NGN 185,000.00 + USD 185,000.00" in action.reason


def test_recordkeeping_copy_is_grounded_and_does_not_promise_tax_compliance(db_session):
    user = merchant(db_session)
    invoice(db_session, user, status="paid", paid_at=NOW - dt.timedelta(days=35))
    facts = retention_facts(db_session, user, NOW)
    action = recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW)
    assert action.title == "Keep each sale recorded"
    assert "0 invoices in the last 30 days" in action.reason
    assert "Have you also recorded" in action.reason
    assert "tax review" in action.reason
    assert "only when received" in action.reason
    assert "2027" not in action.reason and "compliant" not in action.reason
    assert action.action_url == "/dashboard?assistant=invoice"


@pytest.fixture
def storefront_merchant(db_session, monkeypatch):
    from app.models.inventory_models import Product

    monkeypatch.setattr(settings, "SHIPBUBBLE_ENABLED", True)
    monkeypatch.setattr(settings, "SHIPBUBBLE_API_KEY", "test-key")
    monkeypatch.setattr(settings, "SHIPBUBBLE_CHECKOUT_ENABLED", True)
    user = merchant(db_session)
    user.storefront_enabled, user.store_status, user.storefront_slug = True, "active", "retention-store"
    user.storefront_city, user.storefront_state = "Ikeja", "Lagos"
    user.paystack_subaccount_active, user.paystack_subaccount_code = True, "ACCT_TEST"
    invoice(db_session, user, status="paid", paid_at=NOW - dt.timedelta(days=35))
    product = Product(
        user_id=user.id, sku="RET-PHYSICAL", name="Bag", description="Everyday bag",
        image_url="https://example.test/bag.jpg", selling_price=Decimal("1000"), quantity_in_stock=20,
    )
    db_session.add(product)
    db_session.flush()
    return user, product


def test_storefront_nudge_explains_payment_booking_confirmation_and_dropoff(db_session, storefront_merchant):
    user, _ = storefront_merchant
    facts = retention_facts(db_session, user, NOW)
    assert facts.storefront_delivery_ready
    action = recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW)
    assert action.title == "Share your storefront"
    assert action.action_url == "/dashboard/settings#storefront"
    for phrase in [
        "pays for the order and delivery", "Book courier pickup", "package photo",
        "wait for confirmation", "drop-off", "availability",
    ]:
        assert phrase in action.reason
    invoice(db_session, user)
    facts = retention_facts(db_session, user, NOW)
    assert recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW).action_url == "/dashboard/collections"
    without_debt = replace(facts, outstanding_count=0, low_stock_count=1)
    stock_action = recommendation(without_debt, refresh_lifecycle(db_session, without_debt, NOW), NOW)
    assert stock_action.action_url == "/dashboard/inventory"


def test_storefront_discovery_does_not_require_store_setup_or_courier_use(db_session, storefront_merchant):
    from app.models.models import StorefrontOrderEscrow

    user, product = storefront_merchant
    user.storefront_enabled = False
    user.paystack_subaccount_active = False
    product.image_url = None
    db_session.flush()
    assert db_session.query(StorefrontOrderEscrow).count() == 0
    facts = retention_facts(db_session, user, NOW)
    assert facts.has_physical_products and not facts.storefront_delivery_ready
    action = recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW)
    assert action.title == "Discover your SuoOps storefront"
    assert "complete any missing storefront and online-payment setup first" in action.reason
    assert "Where courier delivery is available" in action.reason
    assert "wait for confirmation" in action.reason
    assert action.action_url == "/dashboard/settings#storefront"


def test_unavailable_courier_integration_only_allows_conditional_discovery_copy(
    db_session, storefront_merchant, monkeypatch,
):
    user, _ = storefront_merchant
    monkeypatch.setattr(settings, "SHIPBUBBLE_ENABLED", False)
    facts = retention_facts(db_session, user, NOW)
    action = recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW)
    assert action.title == "Discover your SuoOps storefront"
    assert "Where courier delivery is available" in action.reason
    assert "Your storefront has physical goods available. Share its link" not in action.reason


@pytest.mark.parametrize(
    ("target", "attribute", "value"),
    [
        ("user", "storefront_enabled", False), ("user", "store_status", "paused"),
        ("user", "paystack_subaccount_active", False), ("user", "paystack_subaccount_code", None),
        ("user", "storefront_state", None), ("user", "phone_verified", False),
        ("product", "fulfilment_type", "service"), ("product", "fulfilment_type", "digital"),
        ("product", "image_url", None), ("product", "description", ""),
        ("product", "is_active", False), ("product", "quantity_in_stock", 0),
        ("settings", "SHIPBUBBLE_ENABLED", False), ("settings", "SHIPBUBBLE_API_KEY", None),
        ("settings", "SHIPBUBBLE_CHECKOUT_ENABLED", False),
    ],
)
def test_storefront_delivery_message_is_gated(db_session, storefront_merchant, monkeypatch, target, attribute, value):
    user, product = storefront_merchant
    if target == "settings":
        monkeypatch.setattr(settings, attribute, value)
    else:
        setattr(user if target == "user" else product, attribute, value)
    db_session.flush()
    assert not retention_facts(db_session, user, NOW).storefront_delivery_ready


@pytest.mark.parametrize(
    ("hours", "expected"), [(23, None), (24, "activation"), (167, "activation"), (168, "activation")]
)
def test_activation_requires_full_24_hours_and_includes_existing_unactivated_users(db_session, hours, expected):
    user = merchant(db_session, age=0)
    user.created_at = NOW - dt.timedelta(hours=hours)
    facts = retention_facts(db_session, user, NOW)
    action = recommendation(facts, refresh_lifecycle(db_session, facts, NOW), NOW)
    assert (action.trigger if action else None) == expected


@pytest.mark.parametrize(
    ("days", "state", "trigger"),
    [
        (3, "activated", "first_payment"),
        (13, "activated", "first_payment"),
        (14, "at_risk", "reactivation_14"),
        (21, "at_risk", "reactivation_21"),
        (30, "dormant", "winback_30"),
    ],
)
def test_lifecycle_and_trigger_boundaries(db_session, days, state, trigger):
    user = merchant(db_session)
    invoice(db_session, user, age=days)
    facts = retention_facts(db_session, user, NOW)
    profile = refresh_lifecycle(db_session, facts, NOW)
    assert profile.state == state
    assert recommendation(facts, profile, NOW).trigger == trigger


def test_payment_and_expense_are_activity_but_login_is_not(db_session):
    user = merchant(db_session)
    record = invoice(db_session, user)
    user.last_login = NOW
    facts = retention_facts(db_session, user, NOW)
    assert refresh_lifecycle(db_session, facts, NOW).state == "dormant"
    record.status, record.paid_at = "paid", NOW
    db_session.flush()
    facts = retention_facts(db_session, user, NOW)
    profile = refresh_lifecycle(db_session, facts, NOW)
    assert profile.state == "reactivated"
    assert recommendation(facts, profile, NOW).trigger == "reactivated"
    later = NOW + dt.timedelta(days=8)
    assert refresh_lifecycle(db_session, retention_facts(db_session, user, later), later).state == "engaged"
    invoice(db_session, user, age=0, kind="expense")
    assert retention_facts(db_session, user, NOW).invoices == 1


def test_decline_requires_baseline_and_healthy_business_is_not_nudged(db_session):
    user = merchant(db_session)
    for _ in range(6):
        invoice(db_session, user, age=20, status="paid", paid_at=NOW - dt.timedelta(days=20))
    invoice(db_session, user, age=1)
    facts = retention_facts(db_session, user, NOW)
    assert facts.declining
    assert refresh_lifecycle(db_session, facts, NOW).state == "at_risk"
    healthy = replace(facts, recent_14=4, declining=False, health_score=80)
    profile = MerchantLifecycle(user_id=user.id, state="healthy", state_since=NOW)
    assert recommendation(healthy, profile, NOW) is None


def test_phone_verification_does_not_grant_consent_and_phone_change_revokes_delivery(db_session, pilot, monkeypatch):
    treatment = db_session.query(RetentionEnrollment).filter_by(arm="treatment").first()
    user = db_session.get(User, treatment.user_id)
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    user.phone = "+23480999999999"
    db_session.commit()
    assert send_retention_contact(db_session, user.id, NOW) == "ineligible"
    fresh = merchant(db_session, 80)
    profile = refresh_lifecycle(db_session, retention_facts(db_session, fresh, NOW), NOW)
    assert not profile.whatsapp_opted_in


def test_ongoing_outreach_does_not_require_enrollment(db_session, pilot, monkeypatch):
    sender = Mock()
    sender.send_template_with_id.return_value = "wamid.unenrolled"
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: sender)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda **kwargs: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda **kwargs: None)
    user = merchant(db_session, 81)
    invoice(db_session, user)
    set_consent(db_session, user.id, True, "web_settings", NOW)
    assert not db_session.get(RetentionEnrollment, user.id)
    assert send_retention_contact(db_session, user.id, NOW) == "ineligible"
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    assert send_retention_contact(db_session, user.id, NOW) == "accepted"
    assert db_session.query(RetentionContact).one().experiment_id is None


@pytest.fixture
def email_outreach(db_session, pilot, monkeypatch):
    monkeypatch.setattr(
        "app.services.merchant_retention.get_retention_smtp_configs",
        lambda: [("smtp-relay.brevo.com", 587, "test", "test", "noreply@example.test")],
    )
    sender = Mock(return_value=True)
    monkeypatch.setattr("app.services.merchant_retention.send_retention_email", sender)
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    return sender


def test_existing_unactivated_account_receives_email_without_whatsapp_optin(db_session, email_outreach, monkeypatch):
    user = merchant(db_session, 80, age=180)
    db_session.commit()
    whatsapp = Mock()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", whatsapp)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda **kwargs: False)
    assert send_retention_contact(db_session, user.id, NOW) == "accepted"
    assert not db_session.get(MerchantLifecycle, user.id).whatsapp_opted_in
    contact = db_session.query(RetentionContact).filter_by(user_id=user.id).one()
    assert contact.channel == "email" and contact.experiment_id is None
    assert contact.delivered_at is None and contact.message_id is None
    assert "tax review" in email_outreach.call_args.args[2]
    assert (
        "Stop these emails: https://api.suoops.com/ai/retention/unsubscribe?token="
        in email_outreach.call_args.args[2]
    )
    whatsapp.assert_not_called()
    assert send_retention_contact(db_session, user.id, NOW) == "cooldown"
    email_outreach.assert_called_once()


def test_email_optout_is_persistent_without_blocking_account_access(db_session, email_outreach, client):
    user = merchant(db_session, 80)
    db_session.commit()
    token = email_unsubscribe_token(user, NOW)
    url = f"/ai/retention/unsubscribe?token={token}"
    assert client.get(url).status_code == 200
    profile = refresh_lifecycle(db_session, retention_facts(db_session, user, NOW), NOW)
    assert not profile.email_opted_out
    db_session.commit()
    assert client.post(url, data={"List-Unsubscribe": "One-Click"}).status_code == 200
    db_session.refresh(profile)
    assert profile.email_opted_out
    assert send_retention_contact(db_session, user.id, NOW) == "ineligible"
    email_outreach.assert_not_called()
    headers = {"Authorization": f"Bearer {create_access_token(str(user.id))}"}
    assert client.get("/ai/retention", headers=headers).status_code == 200
    response = client.patch("/ai/retention/consent", json={"channel": "email", "enabled": True}, headers=headers)
    assert response.status_code == 200
    db_session.refresh(profile)
    assert not profile.email_opted_out


def test_unsubscribe_token_is_bound_to_purpose_and_email(db_session, email_outreach, client):
    user = merchant(db_session, 80)
    db_session.commit()
    token = email_unsubscribe_token(user, NOW)
    assert client.get("/ai/retention", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    with pytest.raises(ValueError, match="invalid or expired"):
        unsubscribe_email(db_session, create_access_token(str(user.id)), NOW)
    user.email = "changed@example.test"
    db_session.commit()
    with pytest.raises(ValueError, match="no longer matches"):
        unsubscribe_email(db_session, token, NOW)
    assert client.post("/ai/retention/unsubscribe?token=invalid").status_code == 400


def test_suppression_pause_and_recent_legacy_messages_block_email(db_session, pilot, email_outreach):
    from app.models.models import EmailSuppression, UserEmailLog

    user = merchant(db_session, 80)
    suppression = EmailSuppression(email=user.email, reason="complaint", source="test")
    db_session.add(suppression)
    db_session.commit()
    assert send_retention_contact(db_session, user.id, NOW) == "ineligible"
    db_session.delete(suppression)
    db_session.add(UserEmailLog(user_id=user.id, email_type="activation_day1", sent_at=NOW))
    db_session.commit()
    assert send_retention_contact(db_session, user.id, NOW) == "cooldown"
    db_session.query(UserEmailLog).filter_by(user_id=user.id).delete()
    set_outreach_enabled(db_session, False, NOW, pilot.created_by)
    assert send_retention_contact(db_session, user.id, NOW) == "ineligible"
    email_outreach.assert_not_called()


def test_unknown_email_is_not_retried_or_fallen_back_to_whatsapp(db_session, email_outreach, monkeypatch):
    user = db_session.query(User).filter(User.email.like("merchant%")).first()
    email_outreach.side_effect = RuntimeError("SMTP response lost")
    whatsapp = Mock()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", whatsapp)
    with pytest.raises(RuntimeError, match="SMTP response lost"):
        send_retention_contact(db_session, user.id, NOW)
    contact = db_session.query(RetentionContact).filter_by(user_id=user.id).one()
    assert contact.status == "unknown" and contact.channel == "email"
    assert send_retention_contact(db_session, user.id, NOW + dt.timedelta(days=7)) == "duplicate"
    whatsapp.assert_not_called()
    email_outreach.assert_called_once()


def test_whatsapp_stop_does_not_switch_to_retention_email(db_session, email_outreach):
    user = db_session.query(User).filter(User.email.like("merchant%")).first()
    set_consent(db_session, user.id, False, "whatsapp", NOW)
    assert send_retention_contact(db_session, user.id, NOW) == "ineligible"
    email_outreach.assert_not_called()


def test_ongoing_outreach_requires_kill_switch_but_has_no_experiment_expiry(db_session, pilot, monkeypatch):
    monkeypatch.setattr(settings, "RETENTION_OUTREACH_ENABLED", False)
    with pytest.raises(ValueError, match="kill switch"):
        set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    monkeypatch.setattr(settings, "RETENTION_OUTREACH_ENABLED", True)
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    set_outreach_enabled(db_session, True, NOW + dt.timedelta(days=31), pilot.created_by)
    assert pilot.enabled


def test_existing_approved_template_reaches_both_former_arms_with_cooldown(db_session, pilot, monkeypatch):
    sender = Mock()
    sender.send_template_with_id.side_effect = ["wamid.control", "wamid.retention"]
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: sender)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda **kwargs: True)
    monkeypatch.setattr("app.utils.whatsapp_budget.record_whatsapp_send", lambda **kwargs: None)
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    treatment = db_session.query(RetentionEnrollment).filter_by(arm="treatment").first().user_id
    control = db_session.query(RetentionEnrollment).filter_by(arm="control").first().user_id
    assert send_retention_contact(db_session, control, NOW) == "accepted"
    assert send_retention_contact(db_session, treatment, NOW) == "accepted"
    assert send_retention_contact(db_session, treatment, NOW) == "cooldown"
    assert send_retention_contact(db_session, treatment, NOW + dt.timedelta(days=7)) == "duplicate"
    assert sender.send_template_with_id.call_count == 2
    parameters = sender.send_template_with_id.call_args.args[3][0]["parameters"]
    assert parameters[1]["text"] == "₦185,000"
    assert parameters[2]["text"] == "1"
    assert sender.send_template_with_id.call_args.args[1] == "unpaid_alert_test"
    contact = db_session.query(RetentionContact).filter_by(user_id=treatment).one()
    assert contact.status == "accepted" and contact.delivered_at is None
    record_delivery(db_session, contact.message_id, "delivered", NOW)
    record_delivery(db_session, contact.message_id, "failed", NOW)
    assert contact.status == "delivered"
    record_response(db_session, treatment, NOW + dt.timedelta(hours=1), contact.message_id)
    assert contact.responded_at


def test_unknown_delivery_is_not_retried_and_optout_stops_campaigns(db_session, pilot, monkeypatch):
    sender = Mock()
    sender.send_template_with_id.return_value = None
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: sender)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda **kwargs: True)
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    uid = db_session.query(RetentionEnrollment).filter_by(arm="treatment").first().user_id
    assert send_retention_contact(db_session, uid, NOW) == "unknown"
    assert send_retention_contact(db_session, uid, NOW + dt.timedelta(days=7)) == "duplicate"
    set_consent(db_session, uid, False, "whatsapp", NOW)
    assert send_retention_contact(db_session, uid, NOW + dt.timedelta(days=8)) == "ineligible"
    assert pilot_protected(db_session, uid, NOW + dt.timedelta(days=31))
    sender.send_template_with_id.assert_called_once()


def test_reactivation_cancels_old_winback_action(db_session, pilot):
    user = db_session.get(User, db_session.query(RetentionEnrollment).first().user_id)
    invoice(db_session, user, age=0)
    facts = retention_facts(db_session, user, NOW)
    profile = refresh_lifecycle(db_session, facts, NOW)
    assert profile.state == "reactivated"
    assert recommendation(facts, profile, NOW).trigger == "reactivated"


def test_metrics_separate_itt_and_delivered_contact_rates(db_session, pilot):
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    uid = db_session.query(RetentionEnrollment).filter_by(arm="treatment").first().user_id
    db_session.add(
        RetentionContact(
            user_id=uid,
            experiment_id=1,
            trigger="winback_30",
            dedupe_key="metric",
            state="dormant",
            status="delivered",
            message_id="wamid.metric",
            created_at=NOW,
            delivered_at=NOW,
            responded_at=NOW + dt.timedelta(hours=1),
        )
    )
    record = invoice(db_session, db_session.get(User, uid), age=0, status="paid", paid_at=NOW + dt.timedelta(hours=2))
    record.created_at = NOW + dt.timedelta(hours=1)
    db_session.commit()
    result = retention_metrics(db_session, NOW + dt.timedelta(days=1))
    arm = result["experiment"]["arms"]["treatment"]
    assert arm["reactivation_rate_itt"] == {"numerator": 1, "denominator": 25, "percent": 4.0}
    assert arm["reactivation_rate_contacted"] == {"numerator": 1, "denominator": 1, "percent": 100.0}
    assert arm["whatsapp_response_rate"]["percent"] == 100.0
    assert arm["gmv"] == 185000
    assert arm["recorded_commission"] == 925
    assert result["experiment"]["arms"]["control"]["reactivation_rate_contacted"]["percent"] is None


def test_immature_cohorts_and_zero_denominators_are_not_reported_as_zero(db_session):
    merchant(db_session, age=2)
    result = retention_metrics(db_session, NOW)
    assert result["activation_7_day"]["percent"] is None
    assert result["retention"]["30"]["denominator"] == 0
    assert result["mean_hours_to_first_invoice"] is None


def test_retention_admin_api_rejects_non_super_admin(client):
    app.dependency_overrides[get_current_admin] = lambda: AdminUser(id=123, is_super_admin=False)
    try:
        response = client.get("/admin/retention")
        assert response.status_code == 403
    finally:
        app.dependency_overrides.pop(get_current_admin, None)


def test_owner_consent_api_is_authenticated_and_does_not_send(client, db_session, monkeypatch):
    user = merchant(db_session)
    db_session.commit()
    sender = Mock()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: sender)
    assert client.patch("/ai/retention/consent", json={"enabled": True}).status_code == 401
    token = create_access_token(str(user.id))
    headers = {"Authorization": f"Bearer {token}"}
    response = client.patch("/ai/retention/consent", headers=headers, json={"enabled": True})
    assert response.status_code == 200
    assert response.json() == {"whatsapp_opted_in": True}
    snapshot = client.get("/ai/retention", headers=headers)
    assert snapshot.status_code == 200
    assert snapshot.json()["user_id"] == user.id
    sender.send_template_with_id.assert_not_called()


def test_existing_engagement_job_suppresses_control_group(db_session, pilot, monkeypatch):
    from app.workers.tasks import engagement_tasks

    uid = db_session.query(RetentionEnrollment).filter_by(arm="control").first().user_id
    sender = Mock()
    monkeypatch.setattr(engagement_tasks, "_send_smtp_email", sender)
    stats = {"skipped": 0}
    engagement_tasks._process_user(db_session, db_session.get(User, uid), NOW, stats)
    assert stats["skipped"] == 1
    sender.assert_not_called()


def test_receipts_and_replies_cannot_credit_other_merchant(db_session, pilot):
    users = db_session.query(RetentionEnrollment).limit(2).all()
    contact = RetentionContact(
        user_id=users[0].user_id,
        experiment_id=1,
        trigger="winback_30",
        dedupe_key="scope",
        state="dormant",
        status="accepted",
        message_id="wamid.scope",
        created_at=NOW,
    )
    db_session.add(contact)
    db_session.commit()
    record_response(db_session, users[1].user_id, NOW + dt.timedelta(hours=1), "wamid.scope")
    assert contact.responded_at is None
    record_response(db_session, users[0].user_id, NOW + dt.timedelta(days=8), "wamid.scope")
    assert contact.responded_at is None
    record_delivery(db_session, "wamid.other", "delivered", NOW)
    assert contact.delivered_at is None


@pytest.mark.asyncio
async def test_whatsapp_stop_revokes_consent_before_other_flows(db_session, pilot):
    from app.bot.whatsapp_adapter import WhatsAppHandler

    user = db_session.get(User, db_session.query(RetentionEnrollment).first().user_id)
    client = Mock()
    handler = WhatsAppHandler(client, Mock(), db_session)
    await handler._handle_text_message(user.phone, {"text": "STOP"})
    assert not db_session.get(MerchantLifecycle, user.id).whatsapp_opted_in
    assert "nudges stopped" in client.send_text.call_args.args[1]


def test_metrics_freeze_window_and_never_mix_usd_into_naira(db_session, pilot):
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    uid = db_session.query(RetentionEnrollment).filter_by(arm="treatment").first().user_id
    user = db_session.get(User, uid)
    usd = invoice(db_session, user, age=0, currency="USD", status="paid", paid_at=NOW + dt.timedelta(days=1))
    usd.amount = Decimal("100")
    late = invoice(db_session, user, age=0, status="paid", paid_at=NOW + dt.timedelta(days=31))
    late.created_at = NOW + dt.timedelta(days=1)
    db_session.commit()
    report = retention_metrics(db_session, NOW + dt.timedelta(days=40))
    result = report["experiment"]["arms"]["treatment"]
    assert result["gmv"] == 0
    assert result["gmv_by_currency"] == {"USD": 100}
    assert result["payments_recorded"] == 1
    assert result["invoice_to_payment_conversion"]["numerator"] == 1
    assert report["experiment"]["complete"]


def test_retention_migration_round_trip_matches_models():
    from sqlalchemy import create_engine, inspect

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    migrations = []
    for revision in ("20261014_merchant_retention", "20261014_ongoing_retention"):
        path = Path(__file__).parents[1] / f"alembic/versions/{revision}.py"
        spec = importlib.util.spec_from_file_location(revision, path)
        migration = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(migration)
        migrations.append(migration)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE user (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE admin_users (id INTEGER PRIMARY KEY)")
        with Operations.context(MigrationContext.configure(connection)):
            for migration in migrations:
                migration.upgrade()
            inspector = inspect(connection)
            for model in (MerchantLifecycle, RetentionExperiment, RetentionEnrollment, RetentionContact):
                columns = {column["name"] for column in inspector.get_columns(model.__tablename__)}
                assert columns == set(model.__table__.columns.keys())
            for migration in reversed(migrations):
                migration.downgrade()
            assert set(inspect(connection).get_table_names()) == {"user", "admin_users"}
    engine.dispose()


def test_explicit_restock_reactivates_but_automatic_stock_reversal_does_not(db_session):
    from app.models.inventory_models import Product, StockMovement, StockMovementType

    user = merchant(db_session)
    invoice(db_session, user)
    profile = refresh_lifecycle(db_session, retention_facts(db_session, user, NOW), NOW)
    assert profile.state == "dormant"
    product = Product(user_id=user.id, name="Synthetic stock", sku="RET-STOCK", selling_price=Decimal("100"))
    db_session.add(product)
    db_session.flush()
    movement = StockMovement(
        user_id=user.id,
        product_id=product.id,
        movement_type=StockMovementType.RETURN_IN,
        quantity=5,
        quantity_before=0,
        quantity_after=5,
        created_by="system",
        created_at=NOW,
    )
    db_session.add(movement)
    db_session.flush()
    assert retention_facts(db_session, user, NOW).days_inactive == 35
    movement.created_by = "user"
    movement.movement_type = StockMovementType.ADJUSTMENT
    db_session.flush()
    assert refresh_lifecycle(db_session, retention_facts(db_session, user, NOW), NOW).state == "reactivated"
    report = retention_metrics(db_session, NOW + dt.timedelta(hours=1))
    assert report["last_30_days"]["returning_active_businesses"] == 1
    assert report["last_30_days"]["invoices_created"] == 0


def test_cancelled_manual_invoice_does_not_hide_persisted_wallet_fee(db_session):
    user = merchant(db_session)
    invoice(db_session, user, age=0, status="cancelled")
    report = retention_metrics(db_session, NOW + dt.timedelta(hours=1))
    assert report["last_30_days"]["recorded_commission"] == 925
    assert report["last_30_days"]["invoices_created"] == 0
    assert report["last_30_days"]["returning_active_businesses"] == 0


@pytest.mark.asyncio
async def test_actual_delivery_webhook_uses_provider_timestamp(db_session, pilot):
    from app.bot.whatsapp_adapter import WhatsAppHandler

    uid = db_session.query(RetentionEnrollment).first().user_id
    contact = RetentionContact(
        user_id=uid,
        experiment_id=1,
        trigger="winback_30",
        dedupe_key="webhook",
        state="dormant",
        status="accepted",
        message_id="wamid.webhook",
        created_at=NOW,
    )
    db_session.add(contact)
    db_session.commit()
    delivered = NOW + dt.timedelta(seconds=10)
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": "wamid.webhook",
                                    "status": "delivered",
                                    "timestamp": str(int(delivered.timestamp())),
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }
    await WhatsAppHandler(Mock(), Mock(), db_session).handle_incoming(payload)
    assert contact.status == "delivered"
    assert contact.delivered_at.replace(tzinfo=UTC) == delivered


def test_budget_exhaustion_does_not_reserve_or_send(db_session, pilot, monkeypatch):
    sender = Mock()
    monkeypatch.setattr("app.core.whatsapp.get_whatsapp_client", lambda: sender)
    monkeypatch.setattr("app.utils.whatsapp_budget.can_send_whatsapp", lambda **kwargs: False)
    set_outreach_enabled(db_session, True, NOW, pilot.created_by)
    uid = db_session.query(RetentionEnrollment).filter_by(arm="treatment").first().user_id
    assert send_retention_contact(db_session, uid, NOW) == "budget"
    assert db_session.query(RetentionContact).count() == 0
    sender.send_template_with_id.assert_not_called()
