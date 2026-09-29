#!/usr/bin/env python3
"""Audit log health check.

Exits:
 0 - healthy
 1 - stale (last modified > 26h)
 2 - oversize (> 50MB)
 3 - missing/unreadable
 4 - both stale and oversize

Usage: python scripts/audit/health_check_audit_log.py [path]
Default path: audit.log in project root.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone

MAX_AGE = timedelta(hours=26)
MAX_SIZE_BYTES = 50 * 1024 * 1024  # 50MB


def check(path: str) -> int:
    if not os.path.exists(path):
        print(f"❌ audit log missing: {path}")
        return 3
    try:
        stat = os.stat(path)
    except OSError as exc:
        print(f"❌ cannot stat audit log: {exc}")
        return 3

    now = datetime.now(timezone.utc)
    mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    age = now - mtime
    size = stat.st_size

    stale = age > MAX_AGE
    oversize = size > MAX_SIZE_BYTES

    if stale and oversize:
        print(f"⚠️ audit log stale (>26h) and oversize (>50MB): age={age}, size={size} bytes")
        return 4
    if stale:
        print(f"⚠️ audit log stale (>26h): age={age}")
        return 1
    if oversize:
        print(f"⚠️ audit log oversize (>50MB): size={size} bytes")
        return 2
    print(f"✅ audit log healthy: age={age}, size={size} bytes")
    return 0


def main(argv: list[str]) -> int:
    path = argv[1] if len(argv) > 1 else "audit.log"
    return check(path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
