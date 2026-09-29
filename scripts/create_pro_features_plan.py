#!/usr/bin/env python3
"""Create the recurring **Pro Features** monthly plan on Paystack.

Run this ONCE with the Paystack secret key available in the environment
(``PAYSTACK_SECRET``). It creates a ₦1,500/month plan and prints the
``plan_code``. Creating a plan does NOT charge anyone — it only defines the
recurring price that customers can later subscribe to.

After running, copy the printed code into your production environment:

    PAYSTACK_PRO_FEATURES_PLAN_CODE=PLN_xxxxxxxxxxxx

Usage (use the LIVE secret key for the real plan):

    PAYSTACK_SECRET=sk_live_xxxxx python scripts/create_pro_features_plan.py

For testing first, use your test key (sk_test_...).
"""

import os
import sys

import httpx

PLAN_NAME = "Pro Features Monthly"
AMOUNT_KOBO = 1500 * 100  # ₦1,500 expressed in kobo
INTERVAL = "monthly"
DESCRIPTION = (
    "Recurring monthly access to SuoOps Pro features "
    "(custom branding, tax reports, inventory, team, voice). No invoices included."
)


def main() -> int:
    secret = os.getenv("PAYSTACK_SECRET")
    if not secret:
        print("ERROR: PAYSTACK_SECRET is not set in the environment.", file=sys.stderr)
        print("Run: PAYSTACK_SECRET=sk_live_xxx python scripts/create_pro_features_plan.py", file=sys.stderr)
        return 1

    mode = "LIVE" if secret.startswith("sk_live") else "TEST"
    print(f"Creating '{PLAN_NAME}' (₦{AMOUNT_KOBO // 100:,}/{INTERVAL}) in {mode} mode...")

    try:
        resp = httpx.post(
            "https://api.paystack.co/plan",
            headers={
                "Authorization": f"Bearer {secret}",
                "Content-Type": "application/json",
            },
            json={
                "name": PLAN_NAME,
                "amount": AMOUNT_KOBO,
                "interval": INTERVAL,
                "description": DESCRIPTION,
            },
            timeout=20.0,
        )
    except httpx.HTTPError as exc:
        print(f"ERROR: request to Paystack failed: {exc}", file=sys.stderr)
        return 1

    try:
        data = resp.json()
    except ValueError:
        print(f"ERROR: non-JSON response ({resp.status_code}): {resp.text[:200]}", file=sys.stderr)
        return 1

    if resp.status_code not in (200, 201) or not data.get("status"):
        print(f"ERROR creating plan ({resp.status_code}): {data.get('message', data)}", file=sys.stderr)
        return 1

    plan = data["data"]
    print()
    print("✅ Plan created successfully.")
    print(f"   name:       {plan.get('name')}")
    print(f"   amount:     ₦{plan.get('amount', 0) // 100:,} / {plan.get('interval')}")
    print(f"   plan_code:  {plan.get('plan_code')}")
    print()
    print("Set this in your production environment, then redeploy:")
    print(f"   PAYSTACK_PRO_FEATURES_PLAN_CODE={plan.get('plan_code')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
