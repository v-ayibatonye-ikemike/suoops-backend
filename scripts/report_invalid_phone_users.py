#!/usr/bin/env python3
"""Report users whose phone value isn't a real number (e.g. OAuth placeholders),
and optionally diagnose why one number didn't get the feature announcement.

Meta rejects sends to values like "oauth_google_x" (#131009). Run this on the
env that has the prod DB (e.g. the Render shell):

    python3 scripts/report_invalid_phone_users.py
    python3 scripts/report_invalid_phone_users.py 07065730703   # diagnose one number
"""

from __future__ import annotations

import sys

from app.db.session import SessionLocal
from app.models.models import User, UserEmailLog

ANNOUNCE_EMAIL_TYPE = "announce_online_storefront_v1"
ANNOUNCE_WA_TYPE = "wa_announce_online_storefront_v1"


def _is_valid_phone(value: str | None) -> bool:
    digits = (value or "").strip().lstrip("+")
    return digits.isdigit() and len(digits) >= 10


def _phone_candidates(raw: str) -> set[str]:
    """Build the likely stored forms of a Nigerian number."""
    d = "".join(ch for ch in raw if ch.isdigit())
    out: set[str] = {raw.strip()}
    if d.startswith("0") and len(d) == 11:
        d = "234" + d[1:]
    if d.startswith("234"):
        out.update({d, "+" + d, "0" + d[3:]})
    return out


def _report(db) -> None:
    total = db.query(User).count()
    with_email = db.query(User).filter(User.email.isnot(None)).count()

    invalid = 0
    null_phone = 0
    oauth_like = 0
    samples: list[str] = []
    for uid, phone in db.query(User.id, User.phone).all():
        if phone is None or not str(phone).strip():
            null_phone += 1
            invalid += 1
        elif not _is_valid_phone(phone):
            invalid += 1
            if str(phone).startswith("oauth_"):
                oauth_like += 1
            if len(samples) < 15:
                samples.append(f"#{uid}: {phone!r}")

    print("\n=== Invalid-phone user report ===")
    print(f"Total users:            {total}")
    print(f"Users with an email:    {with_email}")
    print(f"Invalid/placeholder phone: {invalid}")
    print(f"  ├─ null/empty:        {null_phone}")
    print(f"  └─ 'oauth_*' style:   {oauth_like}")
    print(f"Reachable on WhatsApp (valid phone): {total - invalid}")
    if samples:
        print("\nSample invalid values:")
        for s in samples:
            print(f"  {s}")


def _diagnose(db, raw_number: str) -> None:
    cands = _phone_candidates(raw_number)
    user = db.query(User).filter(User.phone.in_(list(cands))).first()
    print(f"\n=== Diagnose {raw_number!r} (tried {sorted(cands)}) ===")
    if not user:
        print(
            "No user found with that phone. → They aren't in the DB under this "
            "number (or it's stored differently), so nothing could be sent."
        )
        return

    print(f"User #{user.id}  name={user.name!r}  email={user.email!r}")
    print(f"phone stored as:        {user.phone!r}")
    print(f"valid phone for WA:     {_is_valid_phone(user.phone)}")
    online = bool(getattr(user, "paystack_subaccount_active", False))
    print(
        f"online payments active: {online}  " f"{'← SKIPPED (already on online payments, by design)' if online else ''}"
    )

    def _sent(email_type: str) -> bool:
        return (
            db.query(UserEmailLog.id)
            .filter(UserEmailLog.user_id == user.id, UserEmailLog.email_type == email_type)
            .first()
            is not None
        )

    print(f"announcement email sent: {_sent(ANNOUNCE_EMAIL_TYPE)}")
    print(f"announcement WA sent:    {_sent(ANNOUNCE_WA_TYPE)}")
    print(
        "\nInterpretation:\n"
        "  • If online payments active → intentionally skipped.\n"
        "  • If WA sent = True but they didn't receive it → Meta dropped it "
        "(131049 marketing throttle) or the number isn't on WhatsApp.\n"
        "  • If WA sent = False and phone valid and not online → left to send; "
        "re-running the task will attempt it."
    )


def main() -> int:
    db = SessionLocal()
    try:
        _report(db)
        if len(sys.argv) > 1:
            _diagnose(db, sys.argv[1])
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
