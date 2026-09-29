"""Lightweight, behavioural anti-fraud layer for signup.

This module deliberately avoids external KYC (BVN/NIN) providers. It relies only
on signals we can capture ourselves at signup time — IP address, a client-side
device fingerprint, the email/phone shape and simple velocity/duplicate checks —
to (a) hard-block the most abusive signups and (b) flag riskier accounts for a
human to review in the admin "Trust & Safety" screen.

Design goals:
  * Never block a legitimate small business by mistake — hard blocks are reserved
    for unambiguous abuse (disposable email, extreme velocity from one IP/device).
  * Everything else raises the risk score and flags for review instead of blocking.
  * Cheap: a couple of indexed COUNT queries, no third-party calls.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import models

# ── Tunable thresholds ────────────────────────────────────────────────
# IMPORTANT (Nigeria): mobile carriers (MTN/Airtel/Glo) route many real users
# through a few shared public IPs (carrier-grade NAT), and cybercafés / agent-
# assisted (concierge) onboarding legitimately create several accounts from one
# IP or device. So IP is treated as a *flag only* signal — it never hard-blocks —
# and device only hard-blocks at an extreme, clearly-automated count.
IP_FLAG_THRESHOLD = 6  # ≥ this many from one IP in the window → flag for review
DEVICE_FLAG_THRESHOLD = 3  # ≥ this many accounts from one browser → flag for review
DEVICE_BLOCK_THRESHOLD = 12  # ≥ this many from one browser in the window → hard block (bot)
VELOCITY_WINDOW = dt.timedelta(hours=24)

# Score at/above which we automatically flag the account for manual review.
FLAG_SCORE = 40

# A small, high-signal disposable/temporary email domain blocklist. Kept short on
# purpose (full lists are huge and go stale); extend as abuse patterns emerge.
DISPOSABLE_EMAIL_DOMAINS: frozenset[str] = frozenset(
    {
        "mailinator.com",
        "guerrillamail.com",
        "guerrillamail.info",
        "sharklasers.com",
        "grr.la",
        "10minutemail.com",
        "10minutemail.net",
        "tempmail.com",
        "temp-mail.org",
        "throwawaymail.com",
        "yopmail.com",
        "getnada.com",
        "trashmail.com",
        "maildrop.cc",
        "dispostable.com",
        "fakeinbox.com",
        "mohmal.com",
        "emailondeck.com",
        "mailnesia.com",
        "spam4.me",
        "moakt.com",
        "tempr.email",
        "discard.email",
        "byom.de",
    }
)

_EMAIL_RE = re.compile(r"^[^@\s]+@([^@\s]+\.[^@\s]+)$")


@dataclass
class RiskAssessment:
    """Outcome of a signup risk evaluation."""

    score: int = 0
    signals: list[str] = field(default_factory=list)
    block: bool = False
    block_reason: str | None = None

    @property
    def flagged(self) -> bool:
        return self.block or self.score >= FLAG_SCORE


def email_domain(email: str | None) -> str | None:
    if not email:
        return None
    match = _EMAIL_RE.match(email.strip().lower())
    return match.group(1) if match else None


def is_disposable_email(email: str | None) -> bool:
    domain = email_domain(email)
    return bool(domain and domain in DISPOSABLE_EMAIL_DOMAINS)


def _count_recent_by(db: Session, column, value: str, window: dt.timedelta) -> int:
    """Count users created within ``window`` whose ``column`` equals ``value``."""
    since = dt.datetime.now(dt.timezone.utc) - window
    return (db.query(func.count(models.User.id)).filter(column == value, models.User.created_at >= since).scalar()) or 0


def evaluate_signup(
    db: Session,
    *,
    ip: str | None,
    device_id: str | None,
    email: str | None,
    user_agent: str | None,
) -> RiskAssessment:
    """Assess a signup attempt and return a :class:`RiskAssessment`.

    Callers should:
      * refuse the signup when ``assessment.block`` is True, and
      * persist ``score``/``signals`` and set ``flagged_for_review`` when
        ``assessment.flagged`` is True.
    """
    assessment = RiskAssessment()

    # 1) Disposable / throwaway email → hard block. These are the single clearest
    #    signal of a fake/multi-account signup and legit SMEs never use them.
    if is_disposable_email(email):
        assessment.score += 60
        assessment.signals.append("disposable_email")
        assessment.block = True
        assessment.block_reason = "disposable_email"

    # 2) IP velocity — many accounts from one IP in a short window. FLAG ONLY:
    #    shared carrier NAT / cybercafé IPs make hard-blocking on IP unsafe in NG.
    if ip:
        ip_count = _count_recent_by(db, models.User.signup_ip, ip, VELOCITY_WINDOW)
        if ip_count >= IP_FLAG_THRESHOLD:
            assessment.score += 25
            assessment.signals.append("ip_velocity")

    # 3) Device fingerprint reuse — the same browser creating multiple accounts.
    #    Only hard-block at an extreme, clearly-automated count; flag before that
    #    (agent-assisted onboarding of a few shops from one device is legitimate).
    if device_id:
        device_count = _count_recent_by(db, models.User.signup_device_id, device_id, VELOCITY_WINDOW)
        if device_count >= DEVICE_BLOCK_THRESHOLD:
            assessment.score += 45
            assessment.signals.append("device_reuse_high")
            assessment.block = True
            assessment.block_reason = assessment.block_reason or "device_reuse"
        elif device_count >= DEVICE_FLAG_THRESHOLD:
            assessment.score += 25
            assessment.signals.append("device_reuse")
    else:
        # No fingerprint at all is mildly suspicious (script / automation), but
        # never blocking on its own since old browsers can fail to produce one.
        assessment.score += 5
        assessment.signals.append("no_device_id")

    # 4) Missing / automation user-agent.
    ua = (user_agent or "").strip().lower()
    if not ua:
        assessment.score += 10
        assessment.signals.append("no_user_agent")
    elif any(bot in ua for bot in ("python-requests", "curl/", "httpclient", "bot", "spider")):
        assessment.score += 20
        assessment.signals.append("bot_user_agent")

    assessment.score = max(0, min(100, assessment.score))
    return assessment


def linked_account_ids(db: Session, user: models.User, *, limit: int = 50) -> list[int]:
    """Return ids of OTHER accounts that share this user's IP or device fingerprint.

    Powers the admin "linked accounts" view so a reviewer can see a cluster of
    accounts created by the same person/device and take action together.
    """
    filters = []
    if user.signup_ip:
        filters.append(models.User.signup_ip == user.signup_ip)
    if user.signup_device_id:
        filters.append(models.User.signup_device_id == user.signup_device_id)
    if not filters:
        return []

    from sqlalchemy import or_

    rows = db.query(models.User.id).filter(or_(*filters), models.User.id != user.id).limit(limit).all()
    return [r.id for r in rows]
