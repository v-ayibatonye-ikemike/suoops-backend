#!/usr/bin/env python3
"""Rotate telemetry.log when size > 100MB or older than 7 days.

Creates compressed archive telemetry-YYYYmmdd-HHMMSS.jsonl.gz
Removes original after successful rotation.

Exit codes:
 0 - no rotation performed, healthy
 1 - rotated successfully
 2 - rotation attempted but failed
"""

from __future__ import annotations

import gzip
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

LOG_PATH = Path("telemetry.log")
MAX_SIZE = 100 * 1024 * 1024  # 100MB
MAX_AGE = timedelta(days=7)


def needs_rotation(stat) -> bool:
    age = datetime.now(timezone.utc) - datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
    return stat.st_size > MAX_SIZE or age > MAX_AGE


def rotate() -> int:
    if not LOG_PATH.exists():
        return 0
    stat = LOG_PATH.stat()
    if not needs_rotation(stat):
        return 0
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    archive = Path(f"telemetry-{ts}.jsonl.gz")
    try:
        with LOG_PATH.open("rb") as rf, gzip.open(archive, "wb", compresslevel=6) as wf:
            shutil.copyfileobj(rf, wf)
        LOG_PATH.unlink()
        # Recreate empty log file
        LOG_PATH.touch()
        print(f"Rotated telemetry.log -> {archive.name}")
        return 1
    except Exception as exc:  # noqa: BLE001
        print(f"Rotation failed: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(rotate())
