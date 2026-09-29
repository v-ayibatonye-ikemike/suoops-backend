"""Read-only: break down Payment Volume (GMV) so you can see what's inflating it.

GMV on the admin dashboard = SUM(amount) of REVENUE invoices with status="paid".
This script shows:
  - Total GMV (all users)
  - Top contributors (by paid revenue), with email/business name so you can spot
    test/internal accounts
  - The single largest paid invoices (test data is usually one giant invoice)
  - GMV recomputed WITHOUT the emails you pass via --exclude

Run against PROD (READ-ONLY — only SELECTs):
    PYTHONPATH=. DATABASE_URL=<prod-url> ./.venv/bin/python scripts/gmv_breakdown.py \
        --exclude founder@example.com,qa@example.com

Optional flags:
    --top 30                 how many top contributors to list (default 20)
    --big 20                 how many largest single invoices to list (default 15)
    --exclude a@b.com,c@d.com emails to remove from the recomputed GMV
"""

from __future__ import annotations

import argparse

from sqlalchemy import func

from app.db.session import SessionLocal
from app.models import models


def _naira(kobo_or_naira) -> str:
    """Amounts are stored in Naira (Numeric). Format with thousands + M/B suffix."""
    v = float(kobo_or_naira or 0)
    if abs(v) >= 1_000_000_000:
        return f"₦{v/1_000_000_000:.2f}B  (₦{v:,.2f})"
    if abs(v) >= 1_000_000:
        return f"₦{v/1_000_000:.2f}M  (₦{v:,.2f})"
    return f"₦{v:,.2f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--big", type=int, default=15)
    ap.add_argument("--exclude", type=str, default="")
    args = ap.parse_args()

    excluded_emails = {e.strip().lower() for e in args.exclude.split(",") if e.strip()}

    Invoice = models.Invoice
    User = models.User

    paid_revenue = [
        Invoice.invoice_type == "revenue",
        Invoice.status == "paid",
    ]

    with SessionLocal() as db:
        # ── Total GMV ──
        total_gmv = db.query(func.coalesce(func.sum(Invoice.amount), 0)).filter(*paid_revenue).scalar()
        total_paid_count = db.query(func.count(Invoice.id)).filter(*paid_revenue).scalar()

        print("=" * 72)
        print(f"TOTAL GMV (paid revenue invoices): {_naira(total_gmv)}")
        print(f"Paid revenue invoice count       : {total_paid_count:,}")
        print("=" * 72)

        # ── Top contributors ──
        rows = (
            db.query(
                Invoice.issuer_id,
                func.coalesce(func.sum(Invoice.amount), 0).label("gmv"),
                func.count(Invoice.id).label("cnt"),
                func.max(Invoice.amount).label("biggest"),
            )
            .filter(*paid_revenue)
            .group_by(Invoice.issuer_id)
            .order_by(func.sum(Invoice.amount).desc())
            .limit(args.top)
            .all()
        )
        user_map = {u.id: u for u in db.query(User).filter(User.id.in_([r.issuer_id for r in rows])).all()}

        print(f"\nTOP {args.top} CONTRIBUTORS TO GMV")
        print("-" * 72)
        running = 0.0
        for r in rows:
            u = user_map.get(r.issuer_id)
            email = (u.email if u else None) or "—"
            biz = (u.business_name if u else None) or (u.name if u else None) or "—"
            flag = "  <-- EXCLUDED" if email.lower() in excluded_emails else ""
            pct = (float(r.gmv) / float(total_gmv) * 100) if total_gmv else 0
            running += float(r.gmv)
            print(
                f"#{r.issuer_id:<5} {biz[:26]:<26} {email[:28]:<28} "
                f"{_naira(r.gmv):<22} {pct:5.1f}%  n={r.cnt}  max={_naira(r.biggest)}{flag}"
            )
        print("-" * 72)
        print(
            f"Top {args.top} together = {_naira(running)} "
            f"({(running/float(total_gmv)*100) if total_gmv else 0:.1f}% of GMV)"
        )

        # ── Largest single paid invoices (test data is usually one giant row) ──
        big = (
            db.query(Invoice.id, Invoice.issuer_id, Invoice.amount, Invoice.created_at)
            .filter(*paid_revenue)
            .order_by(Invoice.amount.desc())
            .limit(args.big)
            .all()
        )
        big_user_map = {u.id: u for u in db.query(User).filter(User.id.in_([b.issuer_id for b in big])).all()}
        print(f"\n{args.big} LARGEST SINGLE PAID INVOICES")
        print("-" * 72)
        for b in big:
            u = big_user_map.get(b.issuer_id)
            biz = (u.business_name if u else None) or (u.name if u else None) or "—"
            email = (u.email if u else None) or "—"
            when = b.created_at.date().isoformat() if b.created_at else "—"
            print(f"inv#{b.id:<7} {_naira(b.amount):<22} {when}  {biz[:24]:<24} {email}")

        # ── GMV excluding the emails you passed ──
        if excluded_emails:
            excluded_ids = [
                uid for (uid,) in db.query(User.id).filter(func.lower(User.email).in_(excluded_emails)).all()
            ]
            gmv_excl_q = db.query(func.coalesce(func.sum(Invoice.amount), 0)).filter(*paid_revenue)
            if excluded_ids:
                gmv_excl_q = gmv_excl_q.filter(Invoice.issuer_id.notin_(excluded_ids))
            gmv_excl = gmv_excl_q.scalar()
            removed = float(total_gmv) - float(gmv_excl)
            print("\n" + "=" * 72)
            print(f"Excluded emails : {', '.join(sorted(excluded_emails))}")
            print(f"Matched user IDs: {excluded_ids or 'NONE (emails not found!)'}")
            print(f"GMV excluding them: {_naira(gmv_excl)}")
            print(
                f"Removed by exclusion: {_naira(removed)} "
                f"({(removed/float(total_gmv)*100) if total_gmv else 0:.1f}% of GMV)"
            )
            print("=" * 72)
        else:
            print("\n(Pass --exclude a@b.com to see GMV without your test accounts.)")


if __name__ == "__main__":
    main()
