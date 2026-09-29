#!/usr/bin/env python3
"""
Slack notification helper.
Usage:
  python scripts/notify/slack_notify.py "Message text here"
Environment:
  SLACK_WEBHOOK_URL (required to send; if missing script exits 0 and prints a note)
Exit codes:
  0 success or skipped, 1 failure in HTTP send.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request


def main():
    webhook = os.getenv("SLACK_WEBHOOK_URL")
    if not webhook:
        print("[slack_notify] SLACK_WEBHOOK_URL not set – skipping send.")
        return 0

    if len(sys.argv) < 2:
        print("[slack_notify] Missing message argument.", file=sys.stderr)
        return 1

    text = sys.argv[1]
    # Basic formatting – can be extended later for richer attachments.
    payload = {"text": text}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode("utf-8")
            print(f"[slack_notify] Sent: status={resp.status} body={body[:120]}")
            return 0
    except urllib.error.HTTPError as e:
        print(f"[slack_notify] HTTPError: {e.code} {e.reason}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001
        print(f"[slack_notify] Error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
