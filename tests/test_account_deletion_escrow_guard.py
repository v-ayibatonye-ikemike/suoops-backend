"""Account deletion is blocked while the seller still has buyer money in escrow
(held/disputed), so protected funds can't be orphaned."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.models import models
from app.services.account_deletion_service import (
    AccountDeletionBlockedError,
    AccountDeletionService,
)


def _seller_with_escrow(db, *, status: str, code: str):
    seller = models.User(
        name="Del Seller",
        phone="+234900" + code[:6],
        storefront_slug=f"del-shop-{code}",
        storefront_enabled=True,
        store_status="active",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)

    cust = models.Customer(name="Buyer", phone="+234812" + code[:6])
    db.add(cust)
    db.commit()
    db.refresh(cust)

    inv = models.Invoice(
        invoice_id=f"INV-DEL-{code}",
        issuer_id=seller.id,
        customer_id=cust.id,
        amount=Decimal("5000"),
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
        same_state=True,
        gross_kobo=500000,
        fee_kobo=15000,
        payout_kobo=485000,
        confirmation_code=code,
    )
    db.add(esc)
    db.commit()
    return seller, esc


def test_delete_blocked_with_held_escrow(db_session):
    seller, _ = _seller_with_escrow(db_session, status="held", code="112233")
    with pytest.raises(AccountDeletionBlockedError):
        AccountDeletionService(db_session).delete_account(seller.id)


def test_delete_blocked_with_disputed_escrow(db_session):
    seller, _ = _seller_with_escrow(db_session, status="disputed", code="445566")
    with pytest.raises(AccountDeletionBlockedError):
        AccountDeletionService(db_session).delete_account(seller.id)


def test_delete_not_blocked_when_escrow_released(db_session):
    seller, esc = _seller_with_escrow(db_session, status="released", code="778899")
    # A released order no longer holds buyer funds → the escrow guard must not fire.
    try:
        AccountDeletionService(db_session).delete_account(seller.id)
    except AccountDeletionBlockedError:  # pragma: no cover
        pytest.fail("Deletion should not be blocked for a released order")
