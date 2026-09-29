"""The seller's invoice list hides unpaid (abandoned) storefront orders but still
shows paid storefront orders and normal pending invoices."""

from __future__ import annotations

from decimal import Decimal

from app.models import models
from app.services.invoice_service import build_invoice_service


def _seed(db):
    seller = models.User(name="S", phone="+2349000123456", business_name="S Biz")
    db.add(seller)
    db.commit()
    db.refresh(seller)

    cust = models.Customer(name="C", phone="+2348120001234")
    db.add(cust)
    db.commit()
    db.refresh(cust)

    def mk(inv_id: str, channel: str | None, status: str):
        db.add(
            models.Invoice(
                invoice_id=inv_id,
                issuer_id=seller.id,
                customer_id=cust.id,
                amount=Decimal("1000"),
                status=status,
                invoice_type="revenue",
                channel=channel,
            )
        )

    mk("INV-NORMAL-PEND", None, "pending")
    mk("INV-SF-PEND", "storefront", "pending")
    mk("INV-SF-PAID", "storefront", "paid")
    db.commit()
    return seller


def test_list_hides_unpaid_storefront(db_session):
    seller = _seed(db_session)
    svc = build_invoice_service(db_session)
    invoices, total = svc.list_invoices(seller.id, limit=100)
    ids = {i.invoice_id for i in invoices}

    assert "INV-SF-PEND" not in ids  # abandoned storefront order is hidden
    assert "INV-SF-PAID" in ids  # paid storefront order shows
    assert "INV-NORMAL-PEND" in ids  # a normal pending invoice still shows
    # The total count also excludes the abandoned order.
    assert total == 2
