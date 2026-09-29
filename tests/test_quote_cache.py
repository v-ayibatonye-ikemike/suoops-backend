"""Delivery-quote cache/cap helpers are fail-open and sane by default."""

from app.services.shipping import quote_cache


def test_get_cached_returns_none_when_absent():
    # Nothing cached (or no Redis) → None, never raises.
    assert quote_cache.get_cached("teststore", 6.5, 3.3, "1:2") is None


def test_store_quota_ok_defaults_true():
    # Under cap (or no Redis) → allowed, never raises.
    assert quote_cache.store_quota_ok("teststore-quota") is True


def test_set_cached_never_raises():
    # Must be safe even without Redis configured.
    quote_cache.set_cached("teststore", 6.5, 3.3, "1:2", {"enabled": True, "options": []})
