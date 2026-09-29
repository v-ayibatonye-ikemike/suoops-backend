"""Money movement and destructive account actions require a super admin — a
lower-privilege support admin (is_super_admin=False) is blocked with 403."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.api.main import app
from app.api.routes_admin_auth import get_current_admin
from app.models.admin_models import AdminUser


def _mk_admin(db, *, email: str, is_super_admin: bool) -> AdminUser:
    a = AdminUser(
        email=email,
        name="A",
        hashed_password="unusable",
        is_active=True,
        is_super_admin=is_super_admin,
        can_manage_tickets=True,
        can_view_users=True,
        can_view_analytics=True,
        can_invite_admins=False,
    )
    db.add(a)
    db.commit()
    db.refresh(a)
    return a


def test_non_super_admin_blocked_from_money_action(db_session):
    client = TestClient(app)
    admin = _mk_admin(db_session, email="peon-gate@suoops.com", is_super_admin=False)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(
            "/admin/users/999999/credit-wallet",
            json={"amount_naira": 1000, "reason": "test reason"},
        )
        assert r.status_code == 403, r.text
    finally:
        app.dependency_overrides.pop(get_current_admin, None)


def test_super_admin_passes_the_gate(db_session):
    client = TestClient(app)
    admin = _mk_admin(db_session, email="super-gate@suoops.com", is_super_admin=True)
    app.dependency_overrides[get_current_admin] = lambda: admin
    try:
        r = client.post(
            "/admin/users/999999/credit-wallet",
            json={"amount_naira": 1000, "reason": "test reason"},
        )
        # Past the gate → it's a 404 for the missing user, not a 403.
        assert r.status_code == 404, r.text
    finally:
        app.dependency_overrides.pop(get_current_admin, None)
