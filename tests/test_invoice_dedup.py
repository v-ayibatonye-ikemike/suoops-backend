"""Regression: invoice creation is idempotent against accidental double-submits."""

from __future__ import annotations

from app.models import models
from app.services.invoice_service import build_invoice_service


def _data(**over):
    d = dict(
        invoice_type="revenue",
        amount=30000,
        currency="NGN",
        customer_name="Alt Bank",
        customer_phone="+2348133157122",
        lines=[{"description": "Vendor-Space", "quantity": 1, "unit_price": 30000}],
    )
    d.update(over)
    return d


def test_identical_create_is_deduped(db_session):
    user = models.User(name="Biz", email="dedup@x.com", wallet_balance_kobo=500000)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    svc = build_invoice_service(db_session)
    i1 = svc.create_invoice(user.id, _data(), consume_balance=True)
    i2 = svc.create_invoice(user.id, _data(), consume_balance=True)

    # Second identical submit reuses the first invoice — no duplicate row.
    assert i1.invoice_id == i2.invoice_id
    count = db_session.query(models.Invoice).filter(models.Invoice.issuer_id == user.id).count()
    assert count == 1


def test_different_invoice_not_deduped(db_session):
    user = models.User(name="Biz", email="nodedup@x.com", wallet_balance_kobo=500000)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    svc = build_invoice_service(db_session)
    i1 = svc.create_invoice(user.id, _data(), consume_balance=True)
    # Same amount + customer but a different item — genuinely a new invoice.
    i3 = svc.create_invoice(
        user.id,
        _data(lines=[{"description": "Different Item", "quantity": 1, "unit_price": 30000}]),
        consume_balance=True,
    )
    assert i1.invoice_id != i3.invoice_id
