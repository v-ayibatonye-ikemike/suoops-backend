import datetime as dt
from decimal import Decimal
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import update

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.api.routes_auth import get_current_user_id
from app.models import models
from app.services import escrow_service as escrow
from app.services.collections import CollectionError
from app.services.payouts.base import PayoutResult

UTC = dt.timezone.utc


@pytest.fixture
def order(db_session):
    seller = models.User(
        name="Escrow seller", phone="+2348109990001", storefront_slug="escrow-safety",
        storefront_enabled=True, store_status="active",
    )
    buyer = models.Customer(name="Escrow buyer", phone="+2348109990002")
    db_session.add_all([seller, buyer])
    db_session.flush()
    invoice = models.Invoice(
        invoice_id="INV-ESCROW-SAFETY", issuer_id=seller.id, customer_id=buyer.id,
        amount=Decimal("1000"), status="paid", invoice_type="revenue", channel="storefront",
    )
    db_session.add(invoice)
    db_session.flush()
    row = models.StorefrontOrderEscrow(
        invoice_id=invoice.id, seller_id=seller.id, status="held",
        gross_kobo=100000, fee_kobo=3000, payout_kobo=100000,
        confirmation_code="815923", charge_reference="INVPAY-SAFETY",
        release_due_at=dt.datetime.now(UTC) - dt.timedelta(hours=1),
        settle_at=dt.datetime.now(UTC) - dt.timedelta(hours=1),
    )
    db_session.add(row)
    db_session.commit()
    return row


@pytest.fixture
def provider(monkeypatch):
    from app.services import payouts

    provider = Mock(name="provider")
    provider.name = "paystack"
    provider.transfer_status.return_value = "unknown"
    provider.transfer_exists.return_value = False
    provider.transfer.return_value = PayoutResult(
        ok=True, reference="unused", provider="paystack", status="pending",
    )
    monkeypatch.setattr(payouts, "get_payout_provider", lambda: provider)
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: provider)
    return provider


def test_refund_cannot_overlap_unknown_payout(db_session, order, provider, monkeypatch):
    order.transfer_reference = "existing-transfer"
    order.transfer_provider = "paystack"
    db_session.commit()
    collector = Mock()
    monkeypatch.setattr("app.services.collections.get_collection_provider_named", lambda name: collector)
    with pytest.raises(escrow.EscrowError):
        escrow.refund_escrow(db_session, order)
    collector.refund.assert_not_called()
    assert order.transfer_reference == "existing-transfer"


def test_uncertain_refund_is_reserved_and_never_resubmitted(db_session, order, monkeypatch):
    collector = Mock()
    collector.refund.side_effect = CollectionError("Connection lost after submission")
    monkeypatch.setattr("app.services.collections.get_collection_provider_named", lambda name: collector)
    with pytest.raises(escrow.EscrowError):
        escrow.refund_escrow(db_session, order)
    db_session.refresh(order)
    assert order.status == "refund_pending"
    assert order.held_for_review
    with pytest.raises(escrow.EscrowError):
        escrow.refund_escrow(db_session, order)
    assert collector.refund.call_count == 1


def test_release_refreshes_stale_identity_map_under_lock(db_session, order, provider):
    db_session.execute(
        update(models.StorefrontOrderEscrow).where(models.StorefrontOrderEscrow.id == order.id)
        .values(status="disputed").execution_options(synchronize_session=False)
    )
    assert order.status == "held"
    assert escrow.release_escrow(db_session, order) is False
    provider.transfer.assert_not_called()


@pytest.mark.parametrize("batch", [False, True])
def test_old_rail_must_be_reconciled_before_changing_provider(db_session, order, monkeypatch, batch):
    old, new = Mock(), Mock()
    old.name, new.name = "paystack", "flutterwave"
    old.transfer_status.return_value = "unknown"
    new.transfer.return_value = PayoutResult(ok=True, reference="new", provider="flutterwave", status="successful")
    order.transfer_reference = "original-rail-transfer"
    order.transfer_provider = "paystack"
    db_session.commit()
    monkeypatch.setattr(escrow, "_collector_for_charge", lambda db, ref: "flutterwave")
    monkeypatch.setattr(
        "app.services.payouts.get_payout_provider_named", lambda name: old if name == "paystack" else new,
    )
    if batch:
        assert escrow.release_seller_batch(db_session, [order], provider_name="flutterwave") == 0
    else:
        assert escrow.release_escrow(db_session, order) is False
    old.transfer_status.assert_called_once_with("original-rail-transfer")
    new.transfer.assert_not_called()
    assert order.transfer_reference == "original-rail-transfer"


def test_admin_retry_never_discards_unknown_transfer(db_session, order, provider, monkeypatch):
    order.transfer_reference = "original-transfer"
    order.transfer_provider = "paystack"
    db_session.commit()
    monkeypatch.setattr("app.api.routes_admin._require_money_stepup", lambda *args: None)
    monkeypatch.setitem(app.dependency_overrides, get_current_admin, lambda: Mock(id=1, is_super_admin=True))
    response = TestClient(app).post(f"/admin/disputes/{order.id}/retry-payout")
    assert response.status_code == 200, response.text
    db_session.refresh(order)
    assert order.transfer_reference == "original-transfer"
    provider.transfer.assert_not_called()


def test_paid_expired_checkout_is_held_for_review(db_session, order, monkeypatch):
    order.status = "canceled"
    order.charge_reference = None
    db_session.commit()
    monkeypatch.setattr(escrow, "send_delivery_code", Mock())
    invoice = db_session.get(models.Invoice, order.invoice_id)
    escrow.activate_escrow_on_payment(db_session, invoice, charge_reference="late-charge")
    db_session.refresh(order)
    assert order.status == "held"
    assert order.held_for_review
    assert order.charge_reference == "late-charge"


@pytest.mark.parametrize("action,payload", [
    ("messages/list", {"code": "815923"}),
    ("report-problem", {"code": "815923", "reason": "The item never arrived"}),
    ("confirm-delivery", {"code": "815923"}),
])
def test_existing_buyer_actions_work_when_store_is_offline(db_session, order, action, payload):
    seller = db_session.get(models.User, order.seller_id)
    seller.storefront_enabled = False
    seller.store_status = "suspended"
    db_session.commit()
    response = TestClient(app).post(f"/public/store/escrow-safety/{action}", json=payload)
    assert response.status_code == 200, response.text


def test_dispute_cannot_claim_funds_are_safe_after_payout_started(db_session, order):
    order.transfer_reference = "payout-in-flight"
    db_session.commit()
    response = TestClient(app).post(
        "/public/store/escrow-safety/report-problem",
        json={"code": "815923", "reason": "The item never arrived"},
    )
    assert response.status_code == 409, response.text
    db_session.refresh(order)
    assert order.status == "held"


@pytest.mark.parametrize("batch", [False, True])
def test_failed_transfer_still_waits_for_settlement(db_session, order, provider, batch):
    order.settle_at = dt.datetime.now(UTC) + dt.timedelta(days=1)
    order.transfer_reference = "failed-original"
    order.transfer_provider = provider.name
    provider.transfer_status.return_value = "failed"
    db_session.commit()
    if batch:
        assert escrow.release_seller_batch(db_session, [order], provider_name=provider.name) == 0
    else:
        assert escrow.release_escrow(db_session, order) is False
    provider.transfer.assert_not_called()
    assert order.transfer_reference == "failed-original"


@pytest.mark.parametrize("batch", [False, True])
def test_release_rechecks_courier_delivery_gate(db_session, order, provider, batch):
    order.delivery_courier = "Courier"
    order.delivery_status = "booking_unknown"
    db_session.commit()
    if batch:
        assert escrow.release_seller_batch(db_session, [order], provider_name=provider.name) == 0
    else:
        assert escrow.release_escrow(db_session, order) is False
    provider.transfer.assert_not_called()


@pytest.mark.parametrize("status", ["held", "disputed"])
def test_reviewed_release_is_persisted_without_faking_buyer_confirmation(
    db_session, order, provider, monkeypatch, status,
):
    order.status = status
    order.held_for_review = True
    order.delivery_courier = "Courier"
    order.settle_at = dt.datetime.now(UTC) + dt.timedelta(days=1)
    db_session.commit()
    monkeypatch.setattr("app.api.routes_admin._require_money_stepup", lambda *args: None)
    monkeypatch.setitem(app.dependency_overrides, get_current_admin, lambda: Mock(id=1, is_super_admin=True))
    response = TestClient(app).post(f"/admin/disputes/{order.id}/resolve", json={"action": "release"})
    assert response.status_code == 200, response.text
    db_session.refresh(order)
    assert order.status == "held"
    assert order.release_approved_at is not None
    assert order.confirmed_at is None
    provider.transfer.assert_not_called()
    order.settle_at = dt.datetime.now(UTC) - dt.timedelta(minutes=1)
    db_session.commit()
    escrow.release_escrow(db_session, order)
    provider.transfer.assert_called_once()


def test_admin_bulk_retry_preserves_unknown_reference(db_session, order, provider, monkeypatch):
    order.transfer_reference = "original-batch"
    order.transfer_provider = provider.name
    db_session.commit()
    monkeypatch.setattr("app.api.routes_admin._require_money_stepup", lambda *args: None)
    monkeypatch.setitem(app.dependency_overrides, get_current_admin, lambda: Mock(id=1, is_super_admin=True))
    response = TestClient(app).post(f"/admin/businesses/{order.seller_id}/retry-held-payouts")
    assert response.status_code == 200, response.text
    db_session.refresh(order)
    assert order.transfer_reference == "original-batch"
    provider.transfer.assert_not_called()


def test_code_is_never_reused_for_a_terminal_order(db_session, order, monkeypatch):
    order.status = "released"
    db_session.commit()
    monkeypatch.setattr(escrow.secrets, "randbelow", lambda _: int(order.confirmation_code) - 100000)
    with pytest.raises(escrow.EscrowError, match="unique order release code"):
        escrow._unique_confirmation_code(db_session, order.seller_id)


def test_refund_pending_blocks_account_deletion(db_session, order):
    from app.services.account_deletion_service import AccountDeletionBlockedError, AccountDeletionService

    order.status = "refund_pending"
    db_session.commit()
    with pytest.raises(AccountDeletionBlockedError):
        AccountDeletionService(db_session).delete_account(order.seller_id)


def test_disputed_seller_can_add_evidence_without_releasing_money(db_session, order, monkeypatch):
    order.status = "disputed"
    db_session.commit()
    monkeypatch.setitem(app.dependency_overrides, get_current_user_id, lambda: order.seller_id)
    response = TestClient(app).post(
        "/inventory/storefront/orders/INV-ESCROW-SAFETY/mark-delivered",
        data={"note": "Delivery receipt available for review"},
    )
    assert response.status_code == 200, response.text
    db_session.refresh(order)
    assert order.status == "disputed"
    assert order.confirmed_at is None
    assert order.delivery_proof_note == "Delivery receipt available for review"


def test_paid_order_is_not_expired_by_cleanup(db_session, order, monkeypatch):
    from contextlib import contextmanager

    from app.workers.tasks.escrow_tasks import cancel_stale_pending_orders

    @contextmanager
    def session():
        yield db_session

    monkeypatch.setattr("app.workers.tasks.escrow_tasks.session_scope", session)
    order.status = "pending"
    order.created_at = dt.datetime.now(UTC) - dt.timedelta(days=30)
    db_session.commit()
    assert cancel_stale_pending_orders.run()["canceled"] == 0
    assert order.status == "pending"


@pytest.mark.parametrize("batch", [False, True])
def test_not_submitted_payout_can_retry_without_guessing_provider_outcome(
    db_session, order, provider, batch,
):
    from app.services.payouts import PayoutNotSubmitted

    provider.transfer.side_effect = [
        PayoutNotSubmitted("Balance too low; transfer not sent"),
        PayoutResult(ok=True, reference="next", provider=provider.name, status="pending"),
    ]

    def release():
        if batch:
            return escrow.release_seller_batch(db_session, [order], provider_name=provider.name)
        return escrow.release_escrow(db_session, order)

    with pytest.raises(escrow.EscrowError, match="not submitted"):
        release()
    db_session.refresh(order)
    assert order.transfer_not_submitted
    previous_reference = order.transfer_reference
    release()
    assert provider.transfer.call_count == 2
    db_session.refresh(order)
    assert not order.transfer_not_submitted
    assert order.transfer_reference != previous_reference


def test_expired_checkout_cannot_bypass_escrow_on_payment_retry(db_session, order, monkeypatch):
    from unittest.mock import AsyncMock

    invoice = db_session.get(models.Invoice, order.invoice_id)
    invoice.status = "pending"
    order.status = "canceled"
    db_session.commit()
    start = AsyncMock()
    monkeypatch.setattr("app.services.invoice_payment_service.start_invoice_payment", start)
    response = TestClient(app).post("/public/invoices/INV-ESCROW-SAFETY/pay")
    assert response.status_code == 409, response.text
    start.assert_not_called()


def test_escrow_safety_migration_round_trip():
    import importlib.util
    from pathlib import Path

    import sqlalchemy as sa

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    path = Path(__file__).parents[1] / "alembic/versions/20261014_escrow_safety.py"
    spec = importlib.util.spec_from_file_location("escrow_safety_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE storefront_order_escrow (id INTEGER PRIMARY KEY)"))
        connection.execute(sa.text("INSERT INTO storefront_order_escrow (id) VALUES (1)"))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            assert connection.execute(sa.text(
                "SELECT release_approved_at, transfer_not_submitted FROM storefront_order_escrow"
            )).one() == (None, 0)
            migration.downgrade()
        assert [c["name"] for c in sa.inspect(connection).get_columns("storefront_order_escrow")] == ["id"]
    engine.dispose()


def test_returned_courier_delivery_is_flagged_and_cannot_pay(db_session, order, provider):
    from app.api.routes_webhooks import _apply_shipbubble_update

    order.shipbubble_order_id = "RETURN-SAFETY"
    order.delivery_status = "delivered"
    order.courier_delivered_at = dt.datetime.now(UTC) - dt.timedelta(days=2)
    db_session.commit()
    _apply_shipbubble_update(db_session, "RETURN-SAFETY", "returned", {}, {})
    db_session.commit()
    assert order.held_for_review
    with pytest.raises(escrow.EscrowError, match="held for review"):
        escrow.release_escrow(db_session, order)
    provider.transfer.assert_not_called()
