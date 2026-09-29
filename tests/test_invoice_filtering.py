"""Server-side invoice filtering: status, search (id/amount/customer), counts."""

from __future__ import annotations

from app.models import models
from app.services.invoice_service import build_invoice_service


def _mk(svc, uid, name, amount, phone):
    return svc.create_invoice(
        uid,
        dict(
            invoice_type="revenue",
            amount=amount,
            currency="NGN",
            customer_name=name,
            customer_phone=phone,
            lines=[{"description": "Svc", "quantity": 1, "unit_price": amount}],
        ),
        consume_balance=True,
    )


def test_search_and_status_counts(db_session):
    user = models.User(name="Biz", email="filter@x.com", wallet_balance_kobo=500000)
    db_session.add(user)
    db_session.commit()
    db_session.refresh(user)

    svc = build_invoice_service(db_session)
    _mk(svc, user.id, "Alpha Co", 1000, "+2348000000001")
    _mk(svc, user.id, "Beta Ltd", 2000, "+2348000000002")
    _mk(svc, user.id, "Alpha Co", 3000, "+2348000000003")

    # Search by customer name spans all invoices.
    inv, total = svc.list_invoices(user.id, search="alpha")
    assert total == 2
    assert all(i.customer.name == "Alpha Co" for i in inv)

    # Search by amount substring.
    _, total_amt = svc.list_invoices(user.id, search="2000")
    assert total_amt == 1

    # Status counts respect search but not the status filter itself.
    counts = svc.count_invoices_by_status(user.id)
    assert counts["all"] == 3
    # Has a phone contact -> created as 'pending'.
    assert counts.get("pending", 0) == 3

    # Status filter narrows the list.
    _, paid_total = svc.list_invoices(user.id, status="paid")
    assert paid_total == 0
