"""Controlled Flutterwave v3 payout smoke test.

Proves the Flutterwave payout rail end-to-end WITHOUT touching escrow:
  1. Reads the wallet (payout) balance.
  2. Resolves the destination bank code from the live bank list.
  3. Sends a small transfer from the wallet to a bank account.
  4. Polls the transfer status so we can see whether it completes automatically
     or gets held for a PIN/OTP/IP-whitelist reason.

The secret is read from the environment — never hardcode or paste it. Run with:

    export FLUTTERWAVE_SECRET='FLWSECK-...'         # live v3 secret
    # optional overrides (defaults transfer to SUOOPS LTD's own UBA account):
    export FW_TEST_ACCOUNT='1028958294'
    export FW_TEST_BANK='United Bank for Africa'
    export FW_TEST_AMOUNT='100'                      # Naira
    python scripts/fw_test_transfer.py

Nothing here writes to the database. It only moves the small test amount you
choose, to the account you choose (default: your own settlement account).
"""

from __future__ import annotations

import os
import sys
import time
import uuid

import httpx

BASE = os.environ.get("FLUTTERWAVE_BASE", "https://api.flutterwave.com").rstrip("/")
SECRET = os.environ.get("FLUTTERWAVE_SECRET")
ACCOUNT = os.environ.get("FW_TEST_ACCOUNT", "1028958294")
BANK_NAME = os.environ.get("FW_TEST_BANK", "United Bank for Africa")
AMOUNT_NAIRA = float(os.environ.get("FW_TEST_AMOUNT", "100"))


def _headers() -> dict[str, str]:
    if not SECRET:
        sys.exit("FLUTTERWAVE_SECRET is not set — export it first (do not hardcode).")
    return {"Authorization": f"Bearer {SECRET}", "Content-Type": "application/json"}


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def check_balance(client: httpx.Client) -> None:
    resp = client.get(f"{BASE}/v3/balances/NGN", headers=_headers())
    data = resp.json()
    if data.get("status") != "success":
        print(f"! Could not read balance: {data.get('message')} (raw: {data})")
        return
    bal = data.get("data", {})
    print(f"  NGN wallet: available={bal.get('available_balance')} ledger={bal.get('ledger_balance')}")


def resolve_bank_code(client: httpx.Client) -> str:
    resp = client.get(f"{BASE}/v3/banks/NG", headers=_headers())
    data = resp.json()
    if data.get("status") != "success":
        sys.exit(f"Could not load bank list: {data.get('message')}")
    lookup = {_norm(b["name"]): str(b["code"]) for b in data.get("data", []) if b.get("name") and b.get("code")}
    code = lookup.get(_norm(BANK_NAME))
    if not code:
        sys.exit(f"Bank not found: {BANK_NAME!r}")
    print(f"  Resolved {BANK_NAME!r} -> code {code}")
    return code


def initiate_transfer(client: httpx.Client, bank_code: str, reference: str) -> dict:
    payload = {
        "account_bank": bank_code,
        "account_number": ACCOUNT,
        "amount": round(AMOUNT_NAIRA, 2),
        "narration": "SuoOps FW payout smoke test",
        "currency": "NGN",
        "debit_currency": "NGN",
        "reference": reference,
    }
    resp = client.post(f"{BASE}/v3/transfers", headers=_headers(), json=payload)
    data = resp.json()
    print(f"  HTTP {resp.status_code}")
    print(f"  status={data.get('status')} message={data.get('message')!r}")
    return data


def poll_status(client: httpx.Client, transfer_id: int | str) -> None:
    for attempt in range(1, 7):
        time.sleep(3)
        resp = client.get(f"{BASE}/v3/transfers/{transfer_id}", headers=_headers())
        data = resp.json()
        tx = data.get("data", {}) or {}
        state = tx.get("status")
        print(f"  [{attempt}] transfer {transfer_id} status={state} complete_message={tx.get('complete_message')!r}")
        if str(state).upper() in {"SUCCESSFUL", "FAILED"}:
            return
    print("  (still not final after polling — check the dashboard)")


def main() -> None:
    reference = f"FWTEST-{uuid.uuid4().hex[:10].upper()}"
    print(f"Flutterwave v3 payout smoke test  base={BASE}")
    print(f"Sending ₦{AMOUNT_NAIRA} to {ACCOUNT} ({BANK_NAME})  ref={reference}\n")

    with httpx.Client(timeout=30) as client:
        print("1) Wallet balance:")
        check_balance(client)
        print("2) Resolve bank code:")
        bank_code = resolve_bank_code(client)
        print("3) Initiate transfer:")
        data = initiate_transfer(client, bank_code, reference)
        tx = data.get("data") or {}
        transfer_id = tx.get("id")
        if data.get("status") == "success" and transfer_id:
            print("4) Poll status:")
            poll_status(client, transfer_id)
        else:
            print("   Transfer was not accepted — full response:")
            print(f"   {data}")
            print("\n   If message mentions OTP/PIN/authorization -> transfer 2FA is blocking the API.")
            print("   If message mentions IP -> whitelist your Render egress IPs in the FW dashboard.")


if __name__ == "__main__":
    main()
