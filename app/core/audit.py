"""Audit logging utilities.

Writes structured JSON lines to a dedicated audit log file and standard logger.
Each event should describe a security- or compliance-relevant action.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from app.core.config import settings

_AUDIT_LOG_PATH = settings.AUDIT_LOG_FILE
_logger = logging.getLogger("audit")


def log_audit_event(action: str, user_id: int | None = None, status: str = "success", **metadata: Any) -> None:
    """Record an audit event.

    Parameters:
        action: A machine-readable action key (e.g. 'admin.users.count', 'user.login').
        user_id: The acting user's ID (if available).
        status: 'success' | 'failure' | 'denied'.
        **metadata: Additional context fields (ids, counts, etc.).
    """
    event = {
        "ts": int(time.time()),
        "action": action,
        "user_id": user_id,
        "status": status,
        **metadata,
    }
    line = json.dumps(event, separators=(",", ":"))
    # Append to file (best effort)
    try:
        os.makedirs(os.path.dirname(_AUDIT_LOG_PATH), exist_ok=True)
        with open(_AUDIT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        _logger.debug("Failed to write audit event to file: %s", event)
    # Durable, queryable copy in Postgres (survives redeploys). Best-effort and
    # never allowed to break the caller. Skipped in tests to keep the suite's
    # shared in-memory DB session clean — covered directly via _persist_audit_db.
    if settings.AUDIT_LOG_TO_DB and getattr(settings, "ENV", None) != "test":
        _persist_audit_db(event)
    # Emit via logger — downgrade expected noise to DEBUG
    if status == "failure" and metadata.get("error") == "missing_refresh_token":
        _logger.debug(line)
    else:
        _logger.info(line)


def _persist_audit_db(event: dict[str, Any]) -> None:
    """Insert one audit event into the durable ``audit_log`` table with a hash
    chain (``entry_hash = sha256(prev_hash + event)``). Best-effort: any failure
    is swallowed so audit logging never breaks a request. Uses its own session so
    the row commits independently of the caller's transaction."""
    try:
        from app.db.session import SessionLocal
        from app.models.models import AuditLog

        details = {k: v for k, v in event.items() if k not in ("action", "user_id", "status", "ts")} or None
        action = str(event.get("action"))[:120]
        user_id = event.get("user_id")
        status = str(event.get("status") or "success")[:20]
        with SessionLocal() as db:
            prev = (db.query(AuditLog.entry_hash).order_by(AuditLog.id.desc()).limit(1).scalar()) or ""
            entry_hash = hash_entry(prev, action, user_id, status, details)
            db.add(
                AuditLog(
                    action=action,
                    user_id=user_id,
                    status=status,
                    details=details,
                    prev_hash=prev or None,
                    entry_hash=entry_hash,
                )
            )
            db.commit()
    except Exception:  # noqa: BLE001 — audit persistence must never break a request
        _logger.debug("Failed to persist audit event to DB: %s", event.get("action"))


def hash_entry(
    prev_hash: str | None,
    action: str,
    user_id: int | None,
    status: str,
    details: Any,
) -> str:
    """Deterministic chain hash of an audit row from its STORED columns only, so a
    verifier can recompute it from the DB later. ``entry_hash = sha256(prev_hash +
    canonical(action, user_id, status, details))``."""
    import hashlib

    canonical = json.dumps(
        {"action": action, "user_id": user_id, "status": status, "details": details},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(((prev_hash or "") + canonical).encode("utf-8")).hexdigest()


def log_denied(action: str, user_id: int | None = None, reason: str | None = None, **extra: Any) -> None:
    log_audit_event(action, user_id=user_id, status="denied", reason=reason, **extra)


def log_failure(action: str, user_id: int | None = None, error: str | None = None, **extra: Any) -> None:
    log_audit_event(action, user_id=user_id, status="failure", error=error, **extra)
