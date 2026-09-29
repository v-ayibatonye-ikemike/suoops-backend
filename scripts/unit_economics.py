"""Read-only: live unit economics (CAC, payback, LTV:CAC) from real DB data.

Pulls the acquisition + revenue denominators the CAC math needs:
  - Monthly signup run-rate (last 6 months + average)
  - Active sellers (created a revenue invoice in the last 30 days)
  - Platform commission earned in the last 30 days (same 3% logic as the app,
    honouring METRICS_EXCLUDED_EMAILS and the METRICS_MAX_INVOICE_NAIRA ceiling)
  - ARPU (commission per active seller / month)
  - Monthly churn -> estimated lifetime -> LTV
Then combines them with your COST inputs (payroll / marketing / infra) to print
fully-loaded CAC, marketing-only CAC, payback period, LTV:CAC and monthly burn.

Read-only (SELECT only). Run on the Render shell:
    PYTHONPATH=. python scripts/unit_economics.py \
        --payroll-ngn 150000 --marketing-usd 200 --infra-usd 222 --fx 1600

Override the acquisition denominator if you know it:
    ... --new-users 50
"""

from __future__ import annotations

import argparse
import datetime as dt

from sqlalchemy import func

from app.core.config import settings
from app.db.session import SessionLocal
from app.models import models
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
        return f"₦{v/1_000:.1f}k"
    return f"₦{v:,.0f}"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--payroll-ngn", type=float, default=150_000)
    ap.add_argument("--marketing-usd", type=float, default=200)
    ap.add_argument("--infra-usd", type=float, default=222)
    ap.add_argument("--fx", type=float, default=1600, help="NGN per USD")
    ap.add_argument("--new-users", type=float, default=0, help="override new-users/month (else uses 6-mo average)")
    args = ap.parse_args()

    Invoice = models.Invoice
    User = models.User
    now = dt.datetime.now(dt.timezone.utc)
    d30 = now - dt.timedelta(days=30)
    d60 = now - dt.timedelta(days=60)
    ceiling = settings.METRICS_MAX_INVOICE_NAIRA or 0

    with SessionLocal() as db:
        excl = _excluded_ids(db)

        def not_excl(q, col):
            return q.filter(col.notin_(excl)) if excl else q

        def cap(q, col):
            return q.filter(col <= ceiling) if ceiling and ceiling > 0 else q

        # ── Signups per month (last 6 months) ──
        print("=" * 64)
        print("ACQUISITION — new signups per month (last 6 months)")
        print("-" * 64)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        starts = []
        cur = month_start
        for _ in range(6):
            starts.append(cur)
            cur = (cur - dt.timedelta(days=1)).replace(day=1)
        starts.reverse()
        total_signups_window = 0
        months_counted = 0
        for i, ms in enumerate(starts):
            me = (ms + dt.timedelta(days=32)).replace(day=1)
            n = db.query(func.count(User.id)).filter(User.created_at >= ms, User.created_at < me).scalar() or 0
            tag = " (partial, current month)" if i == len(starts) - 1 else ""
            print(f"  {ms.strftime('%Y-%m')}: {n:>4}{tag}")
            if not tag:  # only full months feed the average
                total_signups_window += n
                months_counted += 1
        avg_signups = (total_signups_window / months_counted) if months_counted else 0
        total_users = db.query(func.count(User.id)).scalar() or 0
        print(f"\n  Avg new users/month (full months) : {avg_signups:.1f}")
        print(f"  Total registered users            : {total_users}")

        new_users = args.new_users if args.new_users > 0 else avg_signups

        # ── Active sellers (revenue invoice in last 30d) ──
        active_now = {
            r[0]
            for r in not_excl(
                db.query(func.distinct(Invoice.issuer_id)).filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.created_at >= d30,
                ),
                Invoice.issuer_id,
            ).all()
        }
        active_prev = {
            r[0]
            for r in not_excl(
                db.query(func.distinct(Invoice.issuer_id)).filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.created_at >= d60,
                    Invoice.created_at < d30,
                ),
                Invoice.issuer_id,
            ).all()
        }
        active = len(active_now)
        churned = len(active_prev - active_now)
        churn_rate = (churned / len(active_prev)) if active_prev else 0

        # ── Commission last 30 days (same logic as app) ──
        manual = cap(
            not_excl(
                db.query(Invoice.amount).filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.created_at >= d30,
                    ((Invoice.channel != "storefront") | (Invoice.channel.is_(None))),
                ),
                Invoice.issuer_id,
            ),
            Invoice.amount,
        ).all()
        online = cap(
            not_excl(
                db.query(Invoice.amount).filter(
                    Invoice.invoice_type == "revenue",
                    Invoice.channel == "storefront",
                    Invoice.status == "paid",
                    Invoice.paid_at >= d30,
                ),
                Invoice.issuer_id,
            ),
            Invoice.amount,
        ).all()
        commission_30d = (
            sum(platform_fee_kobo(a) for (a,) in manual) + sum(platform_fee_kobo(a) for (a,) in online)
        ) / 100
        arpu = (commission_30d / active) if active else 0

        print("\n" + "=" * 64)
        print("REVENUE — last 30 days")
        print("-" * 64)
        print(f"  Active sellers (invoiced in 30d)  : {active}")
        print(f"  Commission earned (30d)           : {_naira(commission_30d)}")
        print(f"  ARPU (commission / active / mo)   : {_naira(arpu)}")
        print(f"  Monthly churn (rolling 30d)       : {churn_rate*100:.1f}%")

        # LTV = ARPU / churn (geometric lifetime); guard tiny churn
        if churn_rate > 0:
            lifetime_months = 1 / churn_rate
            ltv = arpu / churn_rate
        else:
            lifetime_months = float("inf")
            ltv = float("inf")

        # ── Costs ──
        payroll = args.payroll_ngn
        marketing = args.marketing_usd * args.fx
        infra = args.infra_usd * args.fx
        total_cost = payroll + marketing + infra

        print("\n" + "=" * 64)
        print(f"COSTS — monthly (FX ₦{args.fx:.0f}/$)")
        print("-" * 64)
        print(f"  Payroll                           : {_naira(payroll)}")
        print(f"  Marketing (${args.marketing_usd:.0f})            : {_naira(marketing)}")
        print(f"  Infra (${args.infra_usd:.0f})               : {_naira(infra)}")
        print(f"  TOTAL                             : {_naira(total_cost)}")

        # ── CAC / payback / LTV:CAC ──
        cac_full = (total_cost / new_users) if new_users else 0
        cac_mkt = (marketing / new_users) if new_users else 0
        payback_full = (cac_full / arpu) if arpu else float("inf")
        payback_mkt = (cac_mkt / arpu) if arpu else float("inf")
        ltv_cac = (ltv / cac_full) if (cac_full and ltv != float("inf")) else float("inf")
        burn = commission_30d - total_cost

        def months(x):
            return "∞" if x == float("inf") else f"{x:.1f} mo"

        print("\n" + "=" * 64)
        print(
            f"UNIT ECONOMICS  (new users/mo = {new_users:.1f}"
            f"{' [override]' if args.new_users > 0 else ' [6-mo avg]'})"
        )
        print("-" * 64)
        print(f"  Fully-loaded CAC                  : {_naira(cac_full)}")
        print(f"  Marketing-only CAC                : {_naira(cac_mkt)}")
        print(f"  Est. customer lifetime            : {months(lifetime_months)}")
        print(f"  Est. LTV (ARPU / churn)           : " f"{'∞' if ltv==float('inf') else _naira(ltv)}")
        print(f"  Payback (fully-loaded)            : {months(payback_full)}")
        print(f"  Payback (marketing-only)          : {months(payback_mkt)}")
        print(f"  LTV : CAC (fully-loaded)          : " f"{'∞' if ltv_cac==float('inf') else f'{ltv_cac:.2f}x'}")
        print(f"  Monthly net (commission - cost)   : {_naira(burn)}" f"  ({'BURN' if burn < 0 else 'PROFIT'})")
        print("=" * 64)
        print("Rule of thumb: LTV:CAC >= 3x is healthy; payback <= 12 mo is good.")


if __name__ == "__main__":
    main()
