"""Admin security helpers: client IP extraction, IP allowlist, login audit.

These utilities back two admin-protection features:

* An optional IP allowlist that restricts every ``/admin*`` route to a set of
  configured addresses/CIDR ranges (``settings.ADMIN_IP_ALLOWLIST``).
* A persisted audit trail of admin authentication events so suspicious logins
  (e.g. from unexpected IPs) are visible in the admin panel.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from typing import Iterable

from sqlalchemy.orm import Session
from starlette.requests import Request

from app.core.config import settings
from app.models.admin_models import AdminIpAllowlistEntry, AdminLoginAudit

logger = logging.getLogger(__name__)


def get_client_ip(request: Request) -> str | None:
    """Return the originating client IP.

    Behind a reverse proxy (Render), the real client IP is the entry the trusted
    proxy APPENDS to the RIGHT of ``X-Forwarded-For``. A malicious client can
    prepend their own fake entries on the left, so we must NOT take the leftmost
    value — we count ``settings.TRUSTED_PROXY_HOPS`` from the right (default 1 for
    Render's single edge proxy). This closes X-Forwarded-For spoofing of the
    admin IP allowlist and of buyer-IP fraud signals.
    """
    hops = max(0, int(getattr(settings, "TRUSTED_PROXY_HOPS", 1)))
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded and hops > 0:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if parts:
            idx = len(parts) - hops
            # If the client sent fewer entries than expected hops, fall back to
            # the leftmost we have rather than indexing out of range.
            return parts[idx] if 0 <= idx < len(parts) else parts[0]
    # x-real-ip is set by the proxy (a single value), safe to use when present.
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip.strip()
    if request.client:
        return request.client.host
    return None


_NETWORK = "ipaddress._BaseNetwork"

# The merged allowlist (env + DB) is cached briefly so we don't hit the database
# on every admin request. CRUD operations call ``invalidate_admin_allowlist_cache``
# to apply changes immediately.
_CACHE_TTL_SECONDS = 30.0
_cache: dict = {"loaded": False, "expires": 0.0, "networks": ()}


def env_allowlist_entries() -> list[str]:
    """Return the raw entries configured via the ``ADMIN_IP_ALLOWLIST`` env var."""
    raw = settings.ADMIN_IP_ALLOWLIST or ""
    return [entry.strip() for entry in raw.split(",") if entry.strip()]


def parse_networks(entries: Iterable[str]) -> tuple:
    """Parse IP/CIDR strings into network objects, ignoring invalid ones."""
    networks: list = []
    for entry in entries:
        entry = (entry or "").strip()
        if not entry:
            continue
        try:
            # strict=False lets a bare host (e.g. "203.0.113.4") parse as a /32.
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            logger.warning("Ignoring invalid admin IP allowlist entry: %r", entry)
    return tuple(networks)


def _db_allowlist_entries(db: Session) -> list[str]:
    try:
        return [row[0] for row in db.query(AdminIpAllowlistEntry.cidr).all()]
    except Exception:  # noqa: BLE001 — table may not exist yet (pre-migration)
        logger.debug("Could not load admin IP allowlist from DB", exc_info=True)
        return []


def load_admin_networks(db: Session) -> tuple:
    """Return the merged (env + DB) allowlist networks, cached for a short TTL."""
    now = time.monotonic()
    if _cache["loaded"] and now < _cache["expires"]:
        return _cache["networks"]
    networks = parse_networks(env_allowlist_entries() + _db_allowlist_entries(db))
    _cache.update(loaded=True, expires=now + _CACHE_TTL_SECONDS, networks=networks)
    return networks


def invalidate_admin_allowlist_cache() -> None:
    """Force the next ``load_admin_networks`` call to re-read from env + DB."""
    _cache["loaded"] = False


def ip_matches_networks(ip: str | None, networks: Iterable) -> bool:
    """Return True if ``ip`` falls within any of ``networks``."""
    networks = tuple(networks)
    if not ip or not networks:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in net for net in networks)


def admin_ip_allowlist_enabled(db: Session) -> bool:
    """True when at least one valid allowlist entry exists (env or DB)."""
    return bool(load_admin_networks(db))


def is_admin_ip_allowed(ip: str | None, db: Session) -> bool:
    """Return True if ``ip`` may access admin routes.

    When no allowlist is configured (env + DB both empty/invalid), all IPs are
    allowed so the panel can never be locked out by accident.
    """
    networks = load_admin_networks(db)
    if not networks:
        return True
    return ip_matches_networks(ip, networks)


def record_admin_login_event(
    db: Session,
    *,
    request: Request | None = None,
    status: str,
    event: str = "login",
    admin_id: int | None = None,
    email: str | None = None,
    reason: str | None = None,
) -> None:
    """Persist an admin authentication event. Best-effort: never raises."""
    try:
        ip = get_client_ip(request) if request is not None else None
        user_agent = None
        if request is not None:
            user_agent = (request.headers.get("user-agent") or "")[:512] or None
        entry = AdminLoginAudit(
            admin_id=admin_id,
            email=(email or None),
            ip=ip,
            user_agent=user_agent,
            status=status,
            event=event,
            reason=reason,
        )
        db.add(entry)
        db.commit()
    except Exception:  # noqa: BLE001
        logger.debug("Failed to record admin login audit event", exc_info=True)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
