"""Tests for RBAC utilities."""

from app.core.rbac import require_roles


def test_require_roles_decorator_exists():
    """Test that require_roles decorator exists and is callable."""
    assert callable(require_roles)

    # Should be able to call it with allowed roles
    decorator = require_roles(["admin", "user"])
    assert callable(decorator)
