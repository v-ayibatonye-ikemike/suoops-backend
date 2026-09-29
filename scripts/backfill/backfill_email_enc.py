#!/usr/bin/env python3
"""Backfill encrypted email column (email_enc) for existing users.

Idempotent: skips users where email_enc already populated or plaintext email missing.
Requires ENCRYPTION_KEY to be set; exits 0 if key absent (no-op).

Usage:
  python scripts/backfill/backfill_email_enc.py
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.encryption import encrypt_value
from app.db.session import SessionLocal
from app.models.models import User


def main() -> int:
    if not settings.ENCRYPTION_KEY:
        print("ENCRYPTION_KEY not set; skipping backfill (no-op).")
        return 0
    session: Session = SessionLocal()
    try:
        users = session.query(User).filter(User.email.isnot(None)).all()
        updated = 0
        for u in users:
            if u.email and not u.email_enc:
                enc = encrypt_value(u.email)
                if enc:
                    u.email_enc = enc
                    updated += 1
        if updated:
            session.commit()
        print(f"Backfill complete. Users updated: {updated}")
        return 0
    except Exception as exc:  # noqa: BLE001
        session.rollback()
        print(f"Backfill failed: {exc}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
