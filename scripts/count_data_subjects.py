"""One-off: count distinct data subjects for NDPC "major importance" registration.

The NDPC level (and fee) is driven by how many individuals' personal data you
process in 6 months:
  > 5,000  -> Ultra High Level  (MDP-UHL, ₦250,000)
  > 1,000  -> Extra High Level  (MDP-EHL, ₦100,000)
  > 200    -> Ordinary High Level (MDP-OHL, ₦10,000)   [also the SME class]

A "data subject" = any individual whose personal data you hold: your registered
users (sellers) AND the customers/buyers stored on their invoices/orders.

Run against PROD (read-only):
    PYTHONPATH=. DATABASE_URL=<prod-url> ./.venv/bin/python scripts/count_data_subjects.py
"""

from __future__ import annotations

import datetime as dt

from sqlalchemy import func

from app.db.session import SessionLocal
from app.models import models


def _norm(phone: str | None) -> str | None:
    if not phone:
        return None
    digits = "".join(ch for ch in phone if ch.isdigit())
    if not digits:
        return None
    # Normalize Nigerian numbers to a comparable tail (last 10 digits).
    return digits[-10:]


def main() -> None:
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=182)  # ~6 months

    with SessionLocal() as db:
        total_users = db.query(func.count(models.User.id)).scalar() or 0
        total_customers = db.query(func.count(models.Customer.id)).scalar() or 0

        # Distinct individuals by normalized phone (users ∪ customers).
        user_phones = {_norm(p) for (p,) in db.query(models.User.phone).all() if _norm(p)}
        cust_phones = {_norm(p) for (p,) in db.query(models.Customer.phone).all() if _norm(p)}
        distinct_individuals = len(user_phones | cust_phones)

        # Activity in the last ~6 months (a stricter "processed in 6 months" read).
        users_active_6mo = db.query(func.count(models.User.id)).filter(models.User.last_login >= cutoff).scalar() or 0
        customers_6mo = (
            db.query(func.count(func.distinct(models.Invoice.customer_id)))
            .filter(models.Invoice.created_at >= cutoff)
            .scalar()
            or 0
        )

    def level(n: int) -> str:
        if n > 5000:
            return "Ultra High Level (MDP-UHL) — ₦250,000"
        if n > 1000:
            return "Extra High Level (MDP-EHL) — ₦100,000"
        if n > 200:
            return "Ordinary High Level (MDP-OHL) — ₦10,000"
        return "Below 200 — still register (financial/fiduciary), OHL ₦10,000"

    print("=" * 60)
    print("NDPC data-subject count")
    print("=" * 60)
    print(f"Registered users (sellers):           {total_users:>8,}")
    print(f"Stored customers (buyers):            {total_customers:>8,}")
    print(f"Distinct individuals (phone-deduped): {distinct_individuals:>8,}")
    print("-" * 60)
    print(f"Users active in last 6 months:        {users_active_6mo:>8,}")
    print(f"Customers invoiced in last 6 months:  {customers_6mo:>8,}")
    print("=" * 60)
    print(f"By TOTAL held individuals   -> {level(distinct_individuals)}")
    print(f"By LAST-6-MONTHS activity   -> {level(users_active_6mo + customers_6mo)}")
    print("=" * 60)
    print("Note: pick the level for the HIGHER of the two to avoid under-declaring.")


if __name__ == "__main__":
    main()
