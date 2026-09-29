"""Delivery-fee recovery on refund: delivered → seller absorbs (wallet debit);
not delivered → cancel the courier booking to reclaim the fee."""

import datetime as dt
from types import SimpleNamespace

from app.models import models
from app.services.escrow_service import _settle_refunded_delivery_fee


def _seller(db, phone: str):
    u = models.User(
        phone=phone,
        name="S",
        business_name="S Biz",
        wallet_balance_kobo=200_000,
    )
    db.add(u)
    db.commit()
    db.refresh(u)
    return u


def test_delivered_refund_debits_seller_wallet(db_session):
    seller = _seller(db_session, "+2348160000021")
    escrow = SimpleNamespace(
        id=1,
        seller_id=seller.id,
        delivery_fee_kobo=150_000,
        courier_delivered_at=dt.datetime.now(dt.timezone.utc),
        shipbubble_order_id="SB-1",
    )
    _settle_refunded_delivery_fee(db_session, escrow)
    db_session.refresh(seller)
    assert seller.wallet_balance_kobo == 50_000  # 200,000 − 150,000 absorbed


def test_undelivered_refund_cancels_shipment(db_session, monkeypatch):
    seller = _seller(db_session, "+2348160000022")
    called: dict = {}
    monkeypatch.setattr(
        "app.services.shipping.shipbubble.cancel_shipment",
        lambda oid: (called.setdefault("oid", oid), True)[1],
    )
    escrow = SimpleNamespace(
        id=2,
        seller_id=seller.id,
        delivery_fee_kobo=150_000,
        courier_delivered_at=None,
        shipbubble_order_id="SB-2",
    )
    _settle_refunded_delivery_fee(db_session, escrow)
    db_session.refresh(seller)
    assert seller.wallet_balance_kobo == 200_000  # unchanged — reclaimed from courier
    assert called["oid"] == "SB-2"


def test_no_delivery_fee_is_noop(db_session):
    seller = _seller(db_session, "+2348160000023")
    escrow = SimpleNamespace(
        id=3,
        seller_id=seller.id,
        delivery_fee_kobo=0,
        courier_delivered_at=None,
        shipbubble_order_id=None,
    )
    _settle_refunded_delivery_fee(db_session, escrow)
    db_session.refresh(seller)
    assert seller.wallet_balance_kobo == 200_000
