"""Coverage tests for app/api/routes_admin_auth.py.

Covers the passwordless OTP admin login flow, invites, admin management,
IP allowlist CRUD, and the get_current_admin dependency error paths.

External email delivery (SMTP) is mocked so nothing leaves the process.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes_admin_auth import ADMIN_OTP_PURPOSE, get_current_admin
from app.core import admin_security
from app.core.security import create_access_token
from app.models.admin_models import AdminIpAllowlistEntry, AdminUser
from app.services.otp_service import OTPService

# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_admin_allowlist_cache():
    """The admin IP allowlist is cached module-side; clear it around every test
    so a stale cache from an allowlist test can't block other admin requests."""
    admin_security.invalidate_admin_allowlist_cache()
    yield
    admin_security.invalidate_admin_allowlist_cache()


@pytest.fixture(autouse=True)
def _disable_rate_limit():
    """Disable slowapi rate limiting so repeated OTP requests don't 429."""
    from app.api.rate_limit import limiter

    prev = limiter.enabled
    limiter.enabled = False
    yield
    limiter.enabled = prev


@pytest.fixture(autouse=True)
def _no_email(monkeypatch):
    """Never actually send OTP emails during tests."""
    monkeypatch.setattr(
        "app.services.otp_service.OTPService._send_email_otp",
        lambda self, email, otp, purpose: None,
    )


@pytest.fixture
def client():
    return TestClient(app)


def make_admin(
    db,
    *,
    email="admin@suoops.com",
    name="Admin",
    is_active=True,
    is_super_admin=False,
    can_invite_admins=False,
    **extra,
):
    admin = AdminUser(
        email=email,
        name=name,
        hashed_password="unusable",
        is_active=is_active,
        is_super_admin=is_super_admin,
        can_manage_tickets=True,
        can_view_users=True,
        can_view_analytics=True,
        can_invite_admins=can_invite_admins,
        **extra,
    )
    db.add(admin)
    db.commit()
    db.refresh(admin)
    return admin


def override_admin(admin):
    app.dependency_overrides[get_current_admin] = lambda: admin


def clear_override():
    app.dependency_overrides.pop(get_current_admin, None)


def read_otp(email: str) -> str:
    raw = OTPService()._store.get(f"otp:{ADMIN_OTP_PURPOSE}:{email.lower()}")
    assert raw is not None, "OTP was not stored"
    return json.loads(raw)["code"]


# ---------------------------------------------------------------------------
# Passwordless OTP login flow
# ---------------------------------------------------------------------------


def test_login_flow_end_to_end(client, db_session):
    make_admin(db_session, email="ops@suoops.com", name="Ops", is_super_admin=True)

    r = client.post("/admin/auth/request-otp", json={"email": "ops@suoops.com"})
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    code = read_otp("ops@suoops.com")
    v = client.post("/admin/auth/verify-otp", json={"email": "ops@suoops.com", "otp": code})
    assert v.status_code == 200, v.text
    body = v.json()
    assert body["token_type"] == "bearer"
    assert body["access_token"]
    assert body["user"]["email"] == "ops@suoops.com"
    assert body["user"]["role"] == "admin"

    # Fresh token authenticates /me
    token = body["access_token"]
    me = client.get("/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200, me.text
    assert me.json()["email"] == "ops@suoops.com"
    assert me.json()["access_token"]


def test_request_otp_bad_domain_generic(client, db_session):
    r = client.post("/admin/auth/request-otp", json={"email": "hacker@gmail.com"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_request_otp_unknown_admin_generic(client, db_session):
    r = client.post("/admin/auth/request-otp", json={"email": "nobody@suoops.com"})
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_request_otp_bootstraps_default_admin(client, db_session):
    # No admins exist -> requesting for the default email bootstraps one.
    r = client.post("/admin/auth/request-otp", json={"email": "support@suoops.com"})
    assert r.status_code == 200
    created = db_session.query(AdminUser).filter(AdminUser.email == "support@suoops.com").first()
    assert created is not None
    assert created.is_super_admin is True


def test_request_otp_delivery_failure_returns_502(client, db_session, monkeypatch):
    make_admin(db_session, email="fail@suoops.com")

    def boom(self, identifier, purpose):
        raise RuntimeError("smtp down")

    monkeypatch.setattr("app.services.otp_service.OTPService.send_code", boom)
    r = client.post("/admin/auth/request-otp", json={"email": "fail@suoops.com"})
    assert r.status_code == 502


def test_verify_otp_bad_domain_403(client, db_session):
    r = client.post("/admin/auth/verify-otp", json={"email": "x@gmail.com", "otp": "123456"})
    assert r.status_code == 403


def test_verify_otp_unknown_admin_401(client, db_session):
    r = client.post("/admin/auth/verify-otp", json={"email": "ghost@suoops.com", "otp": "123456"})
    assert r.status_code == 401


def test_verify_otp_bad_code_401(client, db_session):
    make_admin(db_session, email="real@suoops.com")
    client.post("/admin/auth/request-otp", json={"email": "real@suoops.com"})
    r = client.post("/admin/auth/verify-otp", json={"email": "real@suoops.com", "otp": "000000"})
    assert r.status_code == 401


def test_verify_otp_inactive_admin_401(client, db_session):
    make_admin(db_session, email="inactive@suoops.com", is_active=False)
    r = client.post("/admin/auth/verify-otp", json={"email": "inactive@suoops.com", "otp": "123456"})
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# get_current_admin dependency error paths
# ---------------------------------------------------------------------------


def test_me_missing_token_401(client, db_session):
    r = client.get("/admin/auth/me")
    assert r.status_code == 401


def test_me_non_admin_token_403(client, db_session):
    token = create_access_token(subject="42", expires_minutes=10)  # no 'admin:' prefix
    r = client.get("/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_me_unknown_admin_token_403(client, db_session):
    token = create_access_token(subject="admin:999999", expires_minutes=10)
    r = client.get("/admin/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 403


def test_me_invalid_token_401(client, db_session):
    r = client.get("/admin/auth/me", headers={"Authorization": "Bearer not-a-jwt"})
    assert r.status_code == 401


def test_me_via_cookie(client, db_session):
    admin = make_admin(db_session, email="cookie@suoops.com")
    token = create_access_token(subject=f"admin:{admin.id}", expires_minutes=10)
    r = client.get("/admin/auth/me", cookies={"suoops.admin": token})
    assert r.status_code == 200
    assert r.json()["email"] == "cookie@suoops.com"


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------


def test_invite_success(client, db_session):
    admin = make_admin(db_session, email="boss@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/invite", json={"email": "new@suoops.com", "name": "New Admin"})
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
        assert r.json()["invite_link"]
    finally:
        clear_override()


def test_invite_without_permission_403(client, db_session):
    admin = make_admin(db_session, email="peon@suoops.com", is_super_admin=False, can_invite_admins=False)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/invite", json={"email": "new2@suoops.com", "name": "N"})
        assert r.status_code == 403
    finally:
        clear_override()


def test_invite_bad_domain_400(client, db_session):
    admin = make_admin(db_session, email="boss2@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/invite", json={"email": "new@gmail.com", "name": "N"})
        assert r.status_code == 400
    finally:
        clear_override()


def test_invite_existing_active_400(client, db_session):
    admin = make_admin(db_session, email="boss3@suoops.com", is_super_admin=True)
    make_admin(db_session, email="taken@suoops.com", is_active=True)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/invite", json={"email": "taken@suoops.com", "name": "T"})
        assert r.status_code == 400
    finally:
        clear_override()


def test_invite_reinvite_pending(client, db_session):
    admin = make_admin(db_session, email="boss4@suoops.com", is_super_admin=True)
    make_admin(db_session, email="pending@suoops.com", is_active=False)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/invite", json={"email": "pending@suoops.com", "name": "Pend"})
        assert r.status_code == 200, r.text
        assert r.json()["success"] is True
    finally:
        clear_override()


# ---------------------------------------------------------------------------
# Accept invite
# ---------------------------------------------------------------------------


def test_accept_invite_success(client, db_session, monkeypatch):
    import datetime as dt

    # SQLite stores datetimes naive; the route compares against
    # datetime.now(timezone.utc) (aware). Patch the module clock to naive UTC
    # so the comparison works under SQLite (production uses tz-aware Postgres).
    import app.api.routes_admin_auth as _auth_mod

    class _NaiveNow(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.utcnow()

    monkeypatch.setattr(_auth_mod, "datetime", _NaiveNow)

    pending = make_admin(
        db_session,
        email="acceptme@suoops.com",
        is_active=False,
        invite_token="tok-123",
        invite_expires_at=dt.datetime.utcnow() + dt.timedelta(days=3),
    )
    r = client.post("/admin/auth/accept-invite", json={"token": "tok-123"})
    assert r.status_code == 200, r.text
    assert r.json()["access_token"]
    db_session.refresh(pending)
    assert pending.is_active is True


def test_accept_invite_invalid_token_400(client, db_session):
    r = client.post("/admin/auth/accept-invite", json={"token": "nope"})
    assert r.status_code == 400


def test_accept_invite_expired_400(client, db_session, monkeypatch):
    import datetime as dt

    import app.api.routes_admin_auth as _auth_mod

    class _NaiveNow(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return dt.datetime.utcnow()

    monkeypatch.setattr(_auth_mod, "datetime", _NaiveNow)

    make_admin(
        db_session,
        email="expired@suoops.com",
        is_active=False,
        invite_token="tok-exp",
        invite_expires_at=dt.datetime.utcnow() - dt.timedelta(days=1),
    )
    r = client.post("/admin/auth/accept-invite", json={"token": "tok-exp"})
    assert r.status_code == 400


def test_accept_invite_already_active_400(client, db_session):
    make_admin(db_session, email="already@suoops.com", is_active=True, invite_token="tok-act")
    r = client.post("/admin/auth/accept-invite", json={"token": "tok-act"})
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Logout / list admins / remove admin
# ---------------------------------------------------------------------------


def test_logout(client, db_session):
    r = client.post("/admin/auth/logout")
    assert r.status_code == 200
    assert r.json()["success"] is True


def test_list_admins_success(client, db_session):
    admin = make_admin(db_session, email="lister@suoops.com", is_super_admin=True)
    make_admin(db_session, email="other@suoops.com")
    override_admin(admin)
    try:
        r = client.get("/admin/auth/admins")
        assert r.status_code == 200
        assert len(r.json()) >= 2
    finally:
        clear_override()


def test_list_admins_forbidden(client, db_session):
    admin = make_admin(db_session, email="noperm@suoops.com", is_super_admin=False, can_invite_admins=False)
    override_admin(admin)
    try:
        r = client.get("/admin/auth/admins")
        assert r.status_code == 403
    finally:
        clear_override()


def test_remove_admin_non_super_403(client, db_session):
    admin = make_admin(db_session, email="ns@suoops.com", is_super_admin=False)
    target = make_admin(db_session, email="victim@suoops.com")
    override_admin(admin)
    try:
        r = client.delete(f"/admin/auth/admins/{target.id}")
        assert r.status_code == 403
    finally:
        clear_override()


def test_remove_admin_not_found_404(client, db_session):
    admin = make_admin(db_session, email="super@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.delete("/admin/auth/admins/999999")
        assert r.status_code == 404
    finally:
        clear_override()


def test_remove_admin_self_400(client, db_session):
    admin = make_admin(db_session, email="selfsuper@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.delete(f"/admin/auth/admins/{admin.id}")
        assert r.status_code == 400
    finally:
        clear_override()


def test_remove_admin_other_super_400(client, db_session):
    admin = make_admin(db_session, email="super1@suoops.com", is_super_admin=True)
    other_super = make_admin(db_session, email="super2@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.delete(f"/admin/auth/admins/{other_super.id}")
        assert r.status_code == 400
    finally:
        clear_override()


def test_remove_admin_success(client, db_session):
    admin = make_admin(db_session, email="remover@suoops.com", is_super_admin=True)
    target = make_admin(db_session, email="removed@suoops.com", is_super_admin=False)
    override_admin(admin)
    try:
        r = client.delete(f"/admin/auth/admins/{target.id}")
        assert r.status_code == 200
        assert "removed" in r.json()["message"].lower()
    finally:
        clear_override()


# ---------------------------------------------------------------------------
# Login audit
# ---------------------------------------------------------------------------


def test_login_audit_forbidden(client, db_session):
    admin = make_admin(db_session, email="auditns@suoops.com", is_super_admin=False)
    override_admin(admin)
    try:
        r = client.get("/admin/auth/login-audit")
        assert r.status_code == 403
    finally:
        clear_override()


def test_login_audit_success(client, db_session):
    admin = make_admin(db_session, email="auditsuper@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.get("/admin/auth/login-audit?limit=10")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    finally:
        clear_override()


# ---------------------------------------------------------------------------
# IP allowlist
# ---------------------------------------------------------------------------


def test_ip_allowed_public(client, db_session):
    r = client.get("/admin/auth/ip-allowed")
    assert r.status_code == 200
    assert "allowed" in r.json()


def test_ip_allowlist_list_forbidden(client, db_session):
    admin = make_admin(db_session, email="iplistns@suoops.com", is_super_admin=False)
    override_admin(admin)
    try:
        r = client.get("/admin/auth/ip-allowlist")
        assert r.status_code == 403
    finally:
        clear_override()


def test_ip_allowlist_list_success(client, db_session):
    admin = make_admin(db_session, email="iplist@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.get("/admin/auth/ip-allowlist")
        assert r.status_code == 200
        assert isinstance(r.json(), list)
    finally:
        clear_override()


def test_ip_allowlist_add_forbidden(client, db_session):
    admin = make_admin(db_session, email="ipaddns@suoops.com", is_super_admin=False)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/ip-allowlist", json={"cidr": "203.0.113.0/24"})
        assert r.status_code == 403
    finally:
        clear_override()


def test_ip_allowlist_add_invalid_cidr_400(client, db_session):
    admin = make_admin(db_session, email="ipbad@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/ip-allowlist", json={"cidr": "not-an-ip"})
        assert r.status_code == 400
    finally:
        clear_override()


def test_ip_allowlist_add_lockout_guard_400(client, db_session):
    # Caller IP is 'testclient' (not a real IP) so adding a range that doesn't
    # cover it triggers the lock-out guard.
    admin = make_admin(db_session, email="iplock@suoops.com", is_super_admin=True)
    override_admin(admin)
    try:
        r = client.post("/admin/auth/ip-allowlist", json={"cidr": "203.0.113.0/24"})
        assert r.status_code == 400
    finally:
        clear_override()


def test_ip_allowlist_add_success(client, db_session, monkeypatch):
    admin = make_admin(db_session, email="ipadd@suoops.com", is_super_admin=True)
    override_admin(admin)
    # Patch caller-IP resolution (route + middleware) to a matching address.
    monkeypatch.setattr("app.api.routes_admin_auth.get_client_ip", lambda request: "203.0.113.5")
    monkeypatch.setattr("app.core.admin_security.get_client_ip", lambda request: "203.0.113.5")
    try:
        r = client.post("/admin/auth/ip-allowlist", json={"cidr": "203.0.113.0/24", "label": "office"})
        assert r.status_code == 200, r.text
        assert r.json()["cidr"] == "203.0.113.0/24"
    finally:
        clear_override()
        admin_security.invalidate_admin_allowlist_cache()


def test_ip_allowlist_add_duplicate_400(client, db_session, monkeypatch):
    admin = make_admin(db_session, email="ipdup@suoops.com", is_super_admin=True)
    db_session.add(AdminIpAllowlistEntry(cidr="203.0.113.0/24", label="x"))
    db_session.commit()
    override_admin(admin)
    monkeypatch.setattr("app.api.routes_admin_auth.get_client_ip", lambda request: "203.0.113.5")
    monkeypatch.setattr("app.core.admin_security.get_client_ip", lambda request: "203.0.113.5")
    try:
        r = client.post("/admin/auth/ip-allowlist", json={"cidr": "203.0.113.0/24"})
        assert r.status_code == 400
    finally:
        clear_override()
        admin_security.invalidate_admin_allowlist_cache()


def test_ip_allowlist_delete_forbidden(client, db_session):
    admin = make_admin(db_session, email="ipdelns@suoops.com", is_super_admin=False)
    override_admin(admin)
    try:
        r = client.delete("/admin/auth/ip-allowlist/1")
        assert r.status_code == 403
    finally:
        clear_override()


def test_ip_allowlist_delete_not_found_404(client, db_session, monkeypatch):
    admin = make_admin(db_session, email="ipdelnf@suoops.com", is_super_admin=True)
    override_admin(admin)
    monkeypatch.setattr("app.api.routes_admin_auth.get_client_ip", lambda request: "203.0.113.5")
    monkeypatch.setattr("app.core.admin_security.get_client_ip", lambda request: "203.0.113.5")
    try:
        r = client.delete("/admin/auth/ip-allowlist/999999")
        assert r.status_code == 404
    finally:
        clear_override()
        admin_security.invalidate_admin_allowlist_cache()


def test_ip_allowlist_delete_success(client, db_session, monkeypatch):
    admin = make_admin(db_session, email="ipdel@suoops.com", is_super_admin=True)
    entry = AdminIpAllowlistEntry(cidr="203.0.113.0/24", label="office")
    db_session.add(entry)
    db_session.commit()
    db_session.refresh(entry)
    entry_id = entry.id
    override_admin(admin)
    # Caller within the (only) range so middleware admits and removal leaves an
    # empty allowlist (fail-open) rather than locking the caller out.
    monkeypatch.setattr("app.api.routes_admin_auth.get_client_ip", lambda request: "203.0.113.5")
    monkeypatch.setattr("app.core.admin_security.get_client_ip", lambda request: "203.0.113.5")
    admin_security.invalidate_admin_allowlist_cache()
    try:
        r = client.delete(f"/admin/auth/ip-allowlist/{entry_id}")
        assert r.status_code == 200, r.text
        assert "removed" in r.json()["message"].lower()
    finally:
        clear_override()
        admin_security.invalidate_admin_allowlist_cache()
