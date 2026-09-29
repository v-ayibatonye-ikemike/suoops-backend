"""Report-a-problem is gated by the buyer's RELEASE CODE (not just a phone), so
a third party who knows a phone number can't dispute someone else's order or get
the seller flagged."""

from __future__ import annotations

from decimal import Decimal

from fastapi.testclient import TestClient

from app.api.main import app
from app.db.session import get_db
from app.models import models


def _make_held_order(db, *, slug, phone, cust_phone, invoice_id, code="654321"):
    seller = models.User(
        name="Report Seller",
        phone=phone,
        storefront_slug=slug,
        storefront_enabled=True,
        store_status="active",
    )
    db.add(seller)
    db.commit()
    db.refresh(seller)

    customer = models.Customer(name="Report Buyer", phone=cust_phone)
    db.add(customer)
    db.commit()
    db.refresh(customer)

    inv = models.Invoice(
        invoice_id=invoice_id,
        issuer_id=seller.id,
        customer_id=customer.id,
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
        status="held",
        same_state=True,
        gross_kobo=500000,
        fee_kobo=15000,
        payout_kobo=485000,
        confirmation_code=code,
    )
    db.add(esc)
    db.commit()
    db.refresh(esc)
    return seller, inv, esc


def test_wrong_code_cannot_dispute_or_flag_seller():
    client = TestClient(app)
    db = next(get_db())
    seller, inv, esc = _make_held_order(
        db,
        slug="report-shop-a",
        phone="+2349000333444",
        cust_phone="+2348123999888",
        invoice_id="INV-REPORT-A",
        code="654321",
    )

    resp = client.post(
        "/public/store/report-shop-a/report-problem",
        json={"code": "000000", "reason": "never delivered"},
    )
    assert resp.status_code == 404, resp.text

    db.refresh(esc)
    db.refresh(seller)
    assert esc.status == "held"  # not disputed
    assert seller.flagged_for_review is not True  # seller not flagged by a guess


def test_correct_code_disputes_but_does_not_flag_seller():
    client = TestClient(app)
    db = next(get_db())
    seller, inv, esc = _make_held_order(
        db,
        slug="report-shop-b",
        phone="+2349000555666",
        cust_phone="+2348123777666",
        invoice_id="INV-REPORT-B",
        code="777111",
    )

    resp = client.post(
        "/public/store/report-shop-b/report-problem",
        json={"code": "777111", "reason": "wrong item delivered"},
    )
    assert resp.status_code == 200, resp.text

    db.refresh(esc)
    db.refresh(seller)
    assert esc.status == "disputed"
    assert esc.dispute_reason == "wrong item delivered"
    # A single verified dispute freezes the order but does NOT nuke the seller's
    # account-wide review flag (the disputed order already blocks trust + queues).
    assert seller.flagged_for_review is not True
