"""X-Forwarded-For must be read from the RIGHT (trusted proxy hops) so a client
can't spoof their IP by prepending fake entries."""

from __future__ import annotations

from starlette.requests import Request

from app.core.admin_security import get_client_ip
from app.core.config import settings


def _req(xff: str | None = None, real_ip: str | None = None, peer: str = "127.0.0.1") -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if xff is not None:
        headers.append((b"x-forwarded-for", xff.encode()))
    if real_ip is not None:
        headers.append((b"x-real-ip", real_ip.encode()))
    return Request({"type": "http", "headers": headers, "client": (peer, 5555)})


def test_single_trusted_hop_ignores_spoofed_left_entry(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1, raising=False)
    # Client prepends a fake IP; the trusted proxy appends the real peer on the
    # right. We must return the real one, not the spoofed leftmost.
    assert get_client_ip(_req(xff="203.0.113.9, 41.58.1.2")) == "41.58.1.2"


def test_single_entry_is_the_client(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1, raising=False)
    assert get_client_ip(_req(xff="41.58.1.2")) == "41.58.1.2"


def test_two_trusted_hops(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 2, raising=False)
    # client, then the first proxy's IP appended by the second proxy.
    assert get_client_ip(_req(xff="41.58.1.2, 10.0.0.1")) == "41.58.1.2"


def test_falls_back_to_peer_without_xff(monkeypatch):
    monkeypatch.setattr(settings, "TRUSTED_PROXY_HOPS", 1, raising=False)
    assert get_client_ip(_req(peer="8.8.8.8")) == "8.8.8.8"
