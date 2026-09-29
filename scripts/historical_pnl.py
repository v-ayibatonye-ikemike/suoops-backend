"""Read-only: your ACTUAL month-by-month P&L (real history, not a projection).

This is the "Actuals" your real financials come from: real commission earned
(from the DB) minus your real monthly costs (passed in, since infra/payroll/
marketing aren't in the DB). Use it to build the model's Actuals tab and to
re-anchor the projection to where the business really is.

Per calendar month it reports:
  - New signups        (users created that month)
  - Active sellers     (distinct businesses that created a revenue invoice)
  - Commission earned  (real 3% — same logic + exclusions + ceiling as the app)
  - Costs / Operating result / Cumulative   (using your cost inputs)

Read-only (SELECT only). Run on the Render shell:
    PYTHONPATH=. python scripts/historical_pnl.py \
        --months 12 --payroll-ngn 150000 --marketing-usd 200 --infra-usd 222 \
        --fx 1600 --exclude founder@example.com
"""

from __future__ import annotations

import argparse
import datetime as dt

from sqlalchemy import func, or_

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import models
from app.models.payment_models import PaymentStatus, PaymentTransaction
from app.utils.feature_gate import platform_fee_kobo


def _excluded_ids(db) -> list[int]:
    raw = settings.METRICS_EXCLUDED_EMAILS
    if not raw:
        return []
    emails = {e.strip().lower() for e in raw.split(",") if e.strip()}
    if not emails:
        return []
    return [uid for (uid,) in db.query(models.User.id).filter(func.lower(models.User.email).in_(emails)).all()]


def _naira(v) -> str:
    v = float(v or 0)
    if abs(v) >= 1_000_000:
        return f"₦{v/1_000_000:.2f}M"
    if abs(v) >= 1_000:
        return f"₦{v/1_000:.0f}k"
    return f"₦{v:,.0f}"


def _month_starts(n: int) -> list[dt.datetime]:
    now = dt.datetime.now(dt.timezone.utc)
    cur = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    out = []
    for _ in range(n):
        out.append(cur)
        cur = (cur - dt.timedelta(days=1)).replace(day=1)
    return list(reversed(out))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=12)
    ap.add_argument("--payroll-ngn", type=float, default=150_000)
    ap.add_argument("--marketing-usd", type=float, default=200)
    ap.add_argument("--infra-usd", type=float, default=222)
    ap.add_argument("--fx", type=float, default=1600)
    ap.add_argument("--exclude", type=str, default="")
    args = ap.parse_args()

    if args.exclude:
        settings.METRICS_EXCLUDED_EMAILS = args.exclude  # type: ignore[attr-defined]

    monthly_cost = args.payroll_ngn + args.marketing_usd * args.fx + args.infra_usd * args.fx
    ceiling = settings.METRICS_MAX_INVOICE_NAIRA or 0

    Invoice = models.Invoice
    User = models.User

    with SessionLocal() as db:
        excl = _excluded_ids(db)

        def not_excl(q):
            return q.filter(Invoice.issuer_id.notin_(excl)) if excl else q

        def cap(q):
            return q.filter(Invoice.amount <= ceiling) if ceiling and ceiling > 0 else q

        print("=" * 90)
        print("SuoOps — ACTUAL monthly P&L (real history)")
        print(
            f"Monthly cost applied: {_naira(monthly_cost)}  "
            f"(payroll {_naira(args.payroll_ngn)} + mktg ${args.marketing_usd:.0f} "
            f"+ infra ${args.infra_usd:.0f} @ ₦{args.fx:.0f}/$)"
        )
        print("Commission(3%) = THEORETICAL fee owed (new model).  Cash in = REAL money")
        print("collected: Pro subs + invoice packs + wallet top-ups + online splits (both")
        print("the pre-July subscription model AND the new commission model).")
        print("=" * 96)
        hdr = (
            f"{'Month':<9}{'Signups':>8}{'Active':>7}{'Commission':>13}"
            f"{'Cash in':>12}{'Cost':>11}{'Op(cash)':>13}{'Cum(cash)':>13}"
        )
        print(hdr)
        print("-" * 96)

        cum = 0.0
        tot_comm = 0.0
        tot_cash = 0.0
        for ms in _month_starts(args.months):
            me = (ms + dt.timedelta(days=32)).replace(day=1)
            label = ms.strftime("%Y-%m")

            signups = db.query(func.count(User.id)).filter(User.created_at >= ms, User.created_at < me).scalar() or 0

            active = (
                not_excl(
                    db.query(func.count(func.distinct(Invoice.issuer_id))).filter(
                        Invoice.invoice_type == "revenue",
                        Invoice.created_at >= ms,
                        Invoice.created_at < me,
                    )
                ).scalar()
                or 0
            )

            manual = cap(
                not_excl(
                    db.query(Invoice.amount).filter(
                        Invoice.invoice_type == "revenue",
                        Invoice.created_at >= ms,
                        Invoice.created_at < me,
                        or_(Invoice.channel != "storefront", Invoice.channel.is_(None)),
                    )
                )
            ).all()
            online = cap(
                not_excl(
                    db.query(Invoice.amount).filter(
                        Invoice.invoice_type == "revenue",
                        Invoice.channel == "storefront",
                        Invoice.status == "paid",
                        Invoice.paid_at >= ms,
                        Invoice.paid_at < me,
                    )
                )
            ).all()
            manual_comm = sum(platform_fee_kobo(a) for (a,) in manual) / 100
            online_comm = sum(platform_fee_kobo(a) for (a,) in online) / 100
            commission = manual_comm + online_comm  # theoretical 3% owed

            # REAL cash collected this month = ALL successful payments to SuoOps
            # (Pro subscriptions SUB-*, invoice packs + wallet top-ups INVPACK-*)
            # + the online 3% splits settled via Paystack. This captures BOTH the
            # old model (subscriptions/packs, pre-July 2026) and the new commission
            # model. Manual wallet debits aren't extra cash (drawn from the top-up
            # pool already counted), so we don't add them separately.
            payments_kobo = (
                db.query(func.coalesce(func.sum(PaymentTransaction.amount), 0))
                .filter(
                    PaymentTransaction.status == PaymentStatus.SUCCESS,
                    PaymentTransaction.created_at >= ms,
                    PaymentTransaction.created_at < me,
                )
                .scalar()
                or 0
            )
            cash_in = payments_kobo / 100 + online_comm

            # Only charge costs for months the business was actually operating.
            operating = commission > 0 or cash_in > 0 or signups > 0 or active > 0
            cost = monthly_cost if operating else 0
            op = cash_in - cost  # CASH-basis operating result (the real one)
            cum += op
            tot_comm += commission
            tot_cash += cash_in

            print(
                f"{label:<9}{signups:>8}{active:>7}{_naira(commission):>13}"
                f"{_naira(cash_in):>12}{_naira(cost):>11}{_naira(op):>13}{_naira(cum):>13}"
            )

        print("-" * 96)
        print(f"{'TOTAL':<9}{'':>8}{'':>7}{_naira(tot_comm):>13}" f"{_naira(tot_cash):>12}{'':>11}{_naira(cum):>13}")
        print("=" * 96)
        print(f"Theoretical commission (3% owed): {_naira(tot_comm)}")
        print(f"REAL cash collected (bank)      : {_naira(tot_cash)}")
        gap = tot_comm - tot_cash
        print(
            f"Gap (owed but not cash)         : {_naira(gap)}" f"  ({gap/tot_comm*100:.0f}% of theoretical)"
            if tot_comm
            else ""
        )
        print("-" * 96)
        print("Cash in = ALL successful payments (Pro subs + packs + top-ups) + online")
        print("splits = what actually hit the bank (should reconcile to your UBA balance).")
        print("Costs are your CURRENT run-rate applied flat across operating months.")
        print("Op(cash) uses REAL cash, so Cum(cash) ≈ your true net burn to date.")


if __name__ == "__main__":
    main()
