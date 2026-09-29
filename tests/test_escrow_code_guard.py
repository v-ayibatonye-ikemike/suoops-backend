"""Delivery-code brute-force guard: per-store failed-attempt lockout."""

from __future__ import annotations

import app.services.escrow_code_guard as guard
from app.core.config import settings


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, int] = {}

    def get(self, k):
        v = self.store.get(k)
        return None if v is None else str(v)

    def incr(self, k):
        self.store[k] = int(self.store.get(k, 0)) + 1
        return self.store[k]

    def expire(self, k, seconds):  # noqa: ARG002
        return True

    def delete(self, k):
        self.store.pop(k, None)


def test_store_locks_after_failure_threshold(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("app.db.redis_client.get_redis_client", lambda: fake)
    seller_id = 4242

    assert guard.is_code_locked(seller_id) is False
    # One below the threshold → still open.
    for _ in range(settings.ESCROW_CODE_MAX_FAILURES - 1):
        guard.register_code_failure(seller_id)
    assert guard.is_code_locked(seller_id) is False
    # Hitting the threshold locks the store's code entry.
    guard.register_code_failure(seller_id)
    assert guard.is_code_locked(seller_id) is True


def test_success_clears_failures(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr("app.db.redis_client.get_redis_client", lambda: fake)
    seller_id = 4343
    for _ in range(settings.ESCROW_CODE_MAX_FAILURES):
        guard.register_code_failure(seller_id)
    assert guard.is_code_locked(seller_id) is True
    guard.clear_code_failures(seller_id)  # a valid code resets suspicion
    assert guard.is_code_locked(seller_id) is False


def test_fails_open_when_redis_down(monkeypatch):
    def _boom():
        raise RuntimeError("redis down")

    monkeypatch.setattr("app.db.redis_client.get_redis_client", _boom)
    # Never lock out or crash when the cache is unavailable (per-IP limit still applies).
    assert guard.is_code_locked(999) is False
    guard.register_code_failure(999)  # must not raise
    guard.clear_code_failures(999)  # must not raise
