"""Batched seller payout (``release_seller_batch``) money-safety tests.

The batch path consolidates a seller's several due held orders into ONE provider
transfer. These tests assert it keeps every guarantee of the per-order
``release_escrow`` state machine: one summed transfer, idempotent reconciliation
of in-flight transfers (never double-pay), the T+1 settle gate, and clean retry
after a confirmed failure.
"""

import datetime as dt
from decimal import Decimal

from app.services import escrow_service as es


def _fake_provider(transfer_status="successful"):
    """A PayoutProvider whose transfer() reports ``transfer_status`` and whose
    per-reference status is script-controllable via ``status_map``."""
    from app.services.payouts.base import PayoutProvider, PayoutResult

    class FakeProvider(PayoutProvider):
        name = "fake"

        def __init__(self):
            self.sent = []  # list of (reference, amount_kobo)
            self.status_map = {}

        def transfer(self, db, *, seller, amount_kobo, reference, reason):
            self.sent.append((reference, amount_kobo))
            self.status_map.setdefault(reference, transfer_status)
            return PayoutResult(ok=True, reference=reference, provider=self.name, status=transfer_status)

        def transfer_status(self, reference):
            return self.status_map.get(reference, "unknown")

    return FakeProvider()


_SEQ = [0]


def _seller_with_orders(db, *, count, gross_kobo=300000, settle_at="past", status="held"):
    from app.models import models

    _SEQ[0] += 1
    uniq = _SEQ[0]
    seller = models.User(
        name="Batch Seller",
        phone=f"+23480100{uniq:05d}",
        account_number="0123456789",
        bank_name="GTBank",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)

    now = dt.datetime.now(dt.timezone.utc)
    if settle_at == "past":
        settle = now - dt.timedelta(hours=1)
    elif settle_at == "future":
        settle = now + dt.timedelta(hours=6)
    else:
        settle = None

    escrows = []
    for i in range(count):
        customer = models.Customer(name=f"Buyer {i}", phone=f"+2348{uniq:04d}0{i:04d}")
        db.add(customer)
        db.commit()
        db.refresh(customer)
        inv = models.Invoice(
            invoice_id=f"INV-BATCH-{seller.id}-{i}",
            issuer_id=seller.id,
            customer_id=customer.id,
            amount=Decimal("3000"),
            status="paid",
            invoice_type="revenue",
            channel="storefront",
        )
        db.add(inv)
        db.commit()
        db.refresh(inv)
        esc = models.StorefrontOrderEscrow(
            invoice_id=inv.id,
            seller_id=seller.id,
            status=status,
            gross_kobo=gross_kobo,
            fee_kobo=int(gross_kobo * 0.03),
            payout_kobo=int(gross_kobo * 0.97),
            settle_at=settle,
            charge_reference=None,
        )
        db.add(esc)
        db.commit()
        db.refresh(esc)
        escrows.append(esc)
    return seller, escrows


def test_batch_pays_one_summed_transfer_and_releases_all(monkeypatch):
    """Several settled held orders → ONE transfer of the summed payout; all released."""
    import app.services.payouts as payouts
    from app.db.session import SessionLocal

    fake = _fake_provider(transfer_status="successful")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)

    s = SessionLocal()
    try:
        seller, escrows = _seller_with_orders(s, count=3, gross_kobo=300000)
        payout_each = int(300000 * 0.97)

        released = es.release_seller_batch(s, escrows, provider_name="fake", reason="window elapsed")

        assert released == 3
        assert len(fake.sent) == 1  # exactly ONE transfer, not three
        ref, amount = fake.sent[0]
        assert amount == payout_each * 3  # summed
        assert ref.startswith(f"ESCROWBATCH-{seller.id}-")
        for e in escrows:
            s.refresh(e)
            assert e.status == "released"
            assert e.transfer_reference == ref
            assert e.transfer_provider == "fake"
    finally:
        s.rollback()
        s.close()


def test_batch_waits_then_finalizes_without_resending(monkeypatch):
    """A queued (pending) batch stays held; a later 'successful' releases all with
    NO second transfer."""
    import app.services.payouts as payouts
    from app.db.session import SessionLocal

    fake = _fake_provider(transfer_status="pending")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)

    s = SessionLocal()
    try:
        _seller, escrows = _seller_with_orders(s, count=2)

        # Run 1: queued → in flight → nothing released, one transfer sent.
        assert es.release_seller_batch(s, escrows, provider_name="fake") == 0
        assert len(fake.sent) == 1
        ref = fake.sent[0][0]
        for e in escrows:
            s.refresh(e)
            assert e.status == "held"
            assert e.transfer_reference == ref

        # Run 2: provider confirms success → both released, no re-send.
        fake.status_map[ref] = "successful"
        assert es.release_seller_batch(s, escrows, provider_name="fake") == 2
        assert len(fake.sent) == 1  # never re-sent
        for e in escrows:
            s.refresh(e)
            assert e.status == "released"
    finally:
        s.rollback()
        s.close()


def test_batch_does_not_resend_on_unknown_status(monkeypatch):
    """An indeterminate ('unknown') in-flight batch never triggers a re-send."""
    import app.services.payouts as payouts
    from app.db.session import SessionLocal

    fake = _fake_provider(transfer_status="pending")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)

    s = SessionLocal()
    try:
        _seller, escrows = _seller_with_orders(s, count=2)

        assert es.release_seller_batch(s, escrows, provider_name="fake") == 0
        ref = fake.sent[0][0]

        fake.status_map[ref] = "unknown"  # can't determine → must WAIT
        assert es.release_seller_batch(s, escrows, provider_name="fake") == 0
        assert len(fake.sent) == 1  # still only one transfer
        for e in escrows:
            s.refresh(e)
            assert e.status == "held"
    finally:
        s.rollback()
        s.close()


def test_batch_excludes_orders_not_yet_settled(monkeypatch):
    """The T+1 settle gate keeps unsettled orders out of the batch sum."""
    import app.services.payouts as payouts
    from app.db.session import SessionLocal

    fake = _fake_provider(transfer_status="successful")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)

    s = SessionLocal()
    try:
        _seller, escrows = _seller_with_orders(s, count=3, settle_at="past")
        # Push the third order's settlement into the future — it must be excluded.
        escrows[2].settle_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=6)
        s.commit()

        payout_each = int(300000 * 0.97)
        released = es.release_seller_batch(s, escrows, provider_name="fake")

        assert released == 2  # only the two settled ones
        assert len(fake.sent) == 1
        assert fake.sent[0][1] == payout_each * 2  # unsettled amount excluded
        s.refresh(escrows[2])
        assert escrows[2].status == "held"  # left for a later run
    finally:
        s.rollback()
        s.close()


def test_batch_retries_after_confirmed_failure(monkeypatch):
    """A confirmed-failed batch is retried with a FRESH (unburned) reference."""
    import app.services.payouts as payouts
    from app.db.session import SessionLocal

    fake = _fake_provider(transfer_status="pending")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)

    s = SessionLocal()
    try:
        _seller, escrows = _seller_with_orders(s, count=2)

        assert es.release_seller_batch(s, escrows, provider_name="fake") == 0
        first_ref = fake.sent[0][0]

        # First batch failed → next run burns it and sends a fresh reference.
        fake.status_map[first_ref] = "failed"
        released = es.release_seller_batch(s, escrows, provider_name="fake")

        assert released == 0  # new transfer is pending again
        assert len(fake.sent) == 2
        second_ref = fake.sent[1][0]
        assert second_ref != first_ref
        for e in escrows:
            s.refresh(e)
            assert e.transfer_reference == second_ref  # re-stamped with fresh ref
            assert e.status == "held"
    finally:
        s.rollback()
        s.close()


def test_batch_empty_and_non_eligible_are_noops(monkeypatch):
    """No escrows, or only non-'held' ones, release nothing and send no transfer."""
    import app.services.payouts as payouts
    from app.db.session import SessionLocal

    fake = _fake_provider(transfer_status="successful")
    monkeypatch.setattr(payouts, "get_payout_provider_named", lambda name: fake)

    s = SessionLocal()
    try:
        assert es.release_seller_batch(s, [], provider_name="fake") == 0

        _seller, released_orders = _seller_with_orders(s, count=2, status="released")
        assert es.release_seller_batch(s, released_orders, provider_name="fake") == 0
        assert fake.sent == []
    finally:
        s.rollback()
        s.close()
