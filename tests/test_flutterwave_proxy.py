"""Flutterwave payout calls route through the static-IP proxy only when set."""

from __future__ import annotations

import httpx

from app.core.config import settings
from app.services.payouts.flutterwave import FlutterwavePayoutProvider


class _FakeClient:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _CAPTURED.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_CAPTURED: list[dict] = []


def test_proxy_used_when_configured(monkeypatch):
    _CAPTURED.clear()
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(settings, "FLUTTERWAVE_TRANSFER_PROXY", "http://10.0.0.1:3128")

    prov = FlutterwavePayoutProvider()
    with prov._http_client(15):
        pass

    assert _CAPTURED[-1].get("proxy") == "http://10.0.0.1:3128"


def test_no_proxy_when_unset(monkeypatch):
    _CAPTURED.clear()
    monkeypatch.setattr(httpx, "Client", _FakeClient)
    monkeypatch.setattr(settings, "FLUTTERWAVE_TRANSFER_PROXY", None)

    prov = FlutterwavePayoutProvider()
    with prov._http_client(20):
        pass

    assert "proxy" not in _CAPTURED[-1]
    assert _CAPTURED[-1].get("timeout") == 20
