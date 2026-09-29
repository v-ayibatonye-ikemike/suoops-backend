"""Collection provider abstraction: factory + Paystack/Flutterwave charge/verify/refund."""


class _Resp:
    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


class _FakeClient:
    """httpx.Client stand-in dispatching to handler(method, url, body, params)."""

    def __init__(self, handler):
        self._h = handler

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url, headers=None, params=None):
        return self._h("GET", url, None, params)

    def post(self, url, headers=None, json=None):
        return self._h("POST", url, json, None)


def test_factory_selects_collection_provider(monkeypatch):
    from app.core.config import settings
    from app.services.collections import get_collection_provider

    monkeypatch.setattr(settings, "ESCROW_COLLECTOR_PROVIDER", "flutterwave")
    assert get_collection_provider().name == "flutterwave"
    monkeypatch.setattr(settings, "ESCROW_COLLECTOR_PROVIDER", "paystack")
    assert get_collection_provider().name == "paystack"
    monkeypatch.setattr(settings, "ESCROW_COLLECTOR_PROVIDER", "nope")
    assert get_collection_provider().name == "paystack"  # unknown → default


def test_paystack_collection_init_and_verify(monkeypatch):
    import app.services.collections.paystack as pc
    import app.services.paystack_http as psh
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAYSTACK_SECRET", "sk_test_x")
    captured = {}

    def handler(method, url, body, params):
        if method == "POST":
            captured["url"] = url
            captured["body"] = body
            return _Resp({"status": True, "data": {"authorization_url": "https://pay/x"}})
        return _Resp({"status": True, "data": {"status": "success", "amount": 50000, "currency": "NGN", "id": 99}})

    monkeypatch.setattr(psh.httpx, "Client", lambda *a, **k: _FakeClient(handler))
    prov = pc.PaystackCollectionProvider()
    charge = prov.initialize_hold_charge(
        amount_kobo=50000,
        reference="INVPAY-1-AB",
        customer_email="c@x.com",
        customer_phone="+234",
        customer_name="C",
        callback_url="https://cb",
        narration="n",
        metadata={"invoice_id": 1},
    )
    assert charge.authorization_url == "https://pay/x"
    assert captured["url"].endswith("/transaction/initialize")
    assert captured["body"]["amount"] == 50000  # kobo
    assert "subaccount" not in captured["body"]  # held → collected to platform balance

    status = prov.verify_charge("INVPAY-1-AB")
    assert status.status == "successful"
    assert status.amount_kobo == 50000


def test_flutterwave_collection_init_verify_refund(monkeypatch):
    import app.services.collections.flutterwave as fc
    from app.core.config import settings

    monkeypatch.setattr(settings, "FLUTTERWAVE_SECRET", "FLWSECK_test")
    calls = {}

    def handler(method, url, body, params):
        if method == "POST" and url.endswith("/v3/payments"):
            calls["init"] = body
            return _Resp({"status": "success", "data": {"link": "https://flw/checkout"}})
        if method == "GET" and url.endswith("/verify_by_reference"):
            calls["verify_params"] = params
            return _Resp(
                {"status": "success", "data": {"status": "successful", "amount": 500, "currency": "NGN", "id": 12345}}
            )
        if method == "POST" and "/refund" in url:
            calls["refund_url"] = url
            calls["refund_body"] = body
            return _Resp({"status": "success", "data": {"id": 777}})
        return _Resp({"status": "error"})

    monkeypatch.setattr(fc.httpx, "Client", lambda *a, **k: _FakeClient(handler))
    prov = fc.FlutterwaveCollectionProvider()

    charge = prov.initialize_hold_charge(
        amount_kobo=50000,
        reference="INVPAY-2-CD",
        customer_email="c@x.com",
        customer_phone="+234",
        customer_name="C",
        callback_url="https://cb",
        narration="Storefront order",
        metadata={"invoice_id": 2},
    )
    assert charge.authorization_url == "https://flw/checkout"
    assert calls["init"]["tx_ref"] == "INVPAY-2-CD"
    assert calls["init"]["amount"] == 500.0  # kobo → Naira
    assert calls["init"]["currency"] == "NGN"

    status = prov.verify_charge("INVPAY-2-CD")
    assert status.status == "successful"
    assert status.amount_kobo == 50000  # ₦500 → 50000 kobo
    assert status.provider_tx_id == "12345"

    prov.refund(reference="INVPAY-2-CD", amount_kobo=50000, note="dispute")
    assert calls["refund_url"].endswith("/v3/transactions/12345/refund")
    assert calls["refund_body"]["amount"] == 500.0


def test_flutterwave_refund_raises_without_tx(monkeypatch):
    import pytest

    import app.services.collections.flutterwave as fc
    from app.core.config import settings
    from app.services.collections.base import CollectionError

    monkeypatch.setattr(settings, "FLUTTERWAVE_SECRET", "FLWSECK_test")

    def handler(method, url, body, params):
        # verify returns no transaction id
        return _Resp({"status": "success", "data": {"status": "pending"}})

    monkeypatch.setattr(fc.httpx, "Client", lambda *a, **k: _FakeClient(handler))
    with pytest.raises(CollectionError):
        fc.FlutterwaveCollectionProvider().refund(reference="INVPAY-9-Z", amount_kobo=1000, note="x")


def test_flw_webhook_failed_event_does_not_burn_dedup_key():
    """A failed charge.completed must NOT record the dedup key, so a later
    successful event on the SAME tx_ref (retry) is still processed."""
    from app.api.routes_webhooks import _handle_flutterwave_invoice_payment
    from app.db.session import SessionLocal
    from app.models.models import WebhookEvent

    ref = "INVPAY-999-FAILTEST"
    s = SessionLocal()
    try:
        res = _handle_flutterwave_invoice_payment(
            {"event": "charge.completed", "data": {"tx_ref": ref, "status": "failed"}},
            s,
            "sig",
        )
        assert res["status"] == "ignored"
        recorded = (
            s.query(WebhookEvent)
            .filter(
                WebhookEvent.provider == "flutterwave:invoice_payment",
                WebhookEvent.external_id == ref,
            )
            .count()
        )
        assert recorded == 0  # key not burned → retry can succeed
    finally:
        s.rollback()
        s.close()
