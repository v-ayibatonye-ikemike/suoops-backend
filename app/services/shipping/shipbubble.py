"""Shipbubble courier integration (feature-flagged, OFF by default).

Shipbubble is a Nigerian shipping-API aggregator: you fund a Naira wallet and,
when a shipment is booked, the courier fee is drawn from that wallet. The API
itself is free — you pay per shipment. In SuoOps the BUYER pays delivery at
checkout, so SuoOps carries no shipping cost.

Flow (per Shipbubble docs):
  1. validate_address()  → POST /shipping/address/validate  → address_code
  2. fetch_rates()       → POST /shipping/fetch_rates       → request_token + couriers[]
  3. create_shipment()   → POST /shipping/labels            → order_id + tracking_url

Everything here is best-effort and fail-soft: if the flag is off, the key is
missing, or the API errors, callers get ``None``/``[]`` and the manual dispatch
flow continues unaffected. Nothing activates until ``SHIPBUBBLE_ENABLED`` is set
and an API key + funded wallet exist.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import re
from typing import Any

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


def clean_name(name: str | None, pad: str = "Customer") -> str:
    """Shipbubble's address validation demands a clean person name ('John Doe')
    with NO numbers or symbols and at least two words. Business names ("Family's
    Saloon", "…Ltd", digits) and single-word names get rejected, which kills the
    quote. So we strip to letters/spaces and pad to two words."""
    cleaned = re.sub(r"[^A-Za-z ]+", "", name or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if not cleaned:
        return f"Suoops {pad}"
    if len(cleaned.split()) < 2:
        return f"{cleaned} {pad}"[:60]
    return cleaned[:60]


def enabled() -> bool:
    """True only when the integration is switched on AND a key is configured."""
    return bool(settings.SHIPBUBBLE_ENABLED and settings.SHIPBUBBLE_API_KEY)


# ── Delivery status (buyer-facing) ──────────────────────────────────────────
# Normalized lifecycle codes in display order, mapped to friendly labels.
DELIVERY_STATUS_LABELS: dict[str, str] = {
    "booked": "Booked — awaiting courier pickup",
    "pickup_enroute": "Rider on the way to pick up",
    "picked_up": "Picked up by courier",
    "in_transit": "In transit",
    "out_for_delivery": "Out for delivery",
    "delivered": "Delivered",
    "cancelled": "Delivery cancelled",
    "returned": "Returned to sender",
}

# Raw Shipbubble status strings → our normalized code. Keys are lowercased with
# spaces/hyphens collapsed to underscores.
_RAW_STATUS_MAP: dict[str, str] = {
    "pending": "pickup_enroute",
    "confirmed": "pickup_enroute",
    "accepted": "pickup_enroute",
    "picked_up": "picked_up",
    "pickup": "picked_up",
    "collected": "picked_up",
    "in_transit": "in_transit",
    "intransit": "in_transit",
    "shipped": "in_transit",
    "out_for_delivery": "out_for_delivery",
    "outfordelivery": "out_for_delivery",
    "dispatched": "out_for_delivery",
    "completed": "delivered",
    "delivered": "delivered",
    "fulfilled": "delivered",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "returned": "returned",
    "return": "returned",
}


def normalize_status(raw: str | None) -> str | None:
    """Map a raw Shipbubble status to our normalized code (or None if unknown)."""
    if not raw:
        return None
    key = re.sub(r"[\s\-]+", "_", str(raw).strip().lower())
    return _RAW_STATUS_MAP.get(key)


def status_label(code: str | None) -> str | None:
    """Friendly, buyer-facing label for a normalized status code."""
    return DELIVERY_STATUS_LABELS.get(code or "")


@dataclasses.dataclass
class DeliveryOption:
    """One courier rate, normalized for the checkout UI."""

    courier_id: str
    service_code: str
    name: str
    image: str | None
    amount: float  # what to charge the buyer (rate_card_amount)
    wallet_total: float  # what Shipbubble deducts from the wallet (total)
    currency: str
    delivery_eta: str | None
    delivery_eta_time: str | None
    service_type: str | None
    dropoff_station: dict[str, Any] | None

    def as_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=settings.SHIPBUBBLE_BASE_URL,
        headers={
            "Authorization": f"Bearer {settings.SHIPBUBBLE_API_KEY}",
            "Content-Type": "application/json",
        },
        timeout=20,
    )


def _post(path: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    """POST helper — returns the ``data`` object on success, else None. Never raises."""
    if not enabled():
        return None
    try:
        with _client() as client:
            resp = client.post(path, json=payload)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001 — shipping must never break the request
        logger.warning("Shipbubble %s failed: %s", path, exc)
        return None
    if isinstance(body, dict) and body.get("status") == "success":
        return body.get("data") or {}
    logger.warning("Shipbubble %s non-success: %s", path, (body or {}).get("message"))
    return None


def _get(path: str) -> Any | None:
    """GET helper — returns the ``data`` payload on success, else None. Never raises."""
    if not enabled():
        return None
    try:
        with _client() as client:
            resp = client.get(path)
        body = resp.json()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Shipbubble GET %s failed: %s", path, exc)
        return None
    if isinstance(body, dict) and body.get("status") == "success":
        return body.get("data")
    return None


# Cache the account's package categories so we resolve a default id once, not per
# quote. Category ids are account-specific, so we can't hardcode one.
_CATEGORY_CACHE: dict[str, Any] = {"id": None, "fetched": False}
# Prefer a broad, commonly-accepted category when picking a default.
_CATEGORY_PREFERENCE = (
    "others",
    "general",
    "merchandise",
    "package",
    "accessories",
    "fashion",
)


def default_category_id() -> int | None:
    """The package ``category_id`` to use for rate quotes. Uses the configured id
    if set, otherwise fetches the account's categories once and picks a sensible
    general-purpose one (cached)."""
    if settings.SHIPBUBBLE_DEFAULT_CATEGORY_ID:
        return settings.SHIPBUBBLE_DEFAULT_CATEGORY_ID
    if _CATEGORY_CACHE["fetched"]:
        return _CATEGORY_CACHE["id"]
    _CATEGORY_CACHE["fetched"] = True
    cats = _get("/shipping/labels/categories") or []
    chosen: int | None = None
    for pref in _CATEGORY_PREFERENCE:
        for c in cats:
            if pref in str(c.get("category", "")).lower():
                chosen = int(c["category_id"])
                break
        if chosen:
            break
    if chosen is None and cats:  # fall back to the first available category
        try:
            chosen = int(cats[0]["category_id"])
        except Exception:  # noqa: BLE001
            chosen = None
    _CATEGORY_CACHE["id"] = chosen
    return chosen


def validate_address(
    *,
    name: str,
    email: str,
    phone: str,
    address: str,
    latitude: float | None = None,
    longitude: float | None = None,
) -> int | None:
    """Validate an address and return its Shipbubble ``address_code`` (or None)."""
    payload: dict[str, Any] = {
        "name": name,
        "email": email,
        "phone": phone,
        "address": address,
    }
    if latitude is not None and longitude is not None:
        payload["latitude"] = latitude
        payload["longitude"] = longitude
    data = _post("/shipping/address/validate", payload)
    return int(data["address_code"]) if data and data.get("address_code") else None


def fetch_rates(
    *,
    sender_address_code: int,
    receiver_address_code: int,
    package_items: list[dict[str, Any]],
    package_dimension: dict[str, float] | None = None,
    category_id: int | None = None,
    pickup_date: str | None = None,
) -> dict[str, Any] | None:
    """Fetch courier rates. Returns ``{"request_token": str, "options": [DeliveryOption…]}``
    or None. The ``request_token`` is required to later create the shipment."""
    payload: dict[str, Any] = {
        "sender_address_code": sender_address_code,
        # NB: Shipbubble spells the field "reciever_address_code".
        "reciever_address_code": receiver_address_code,
        "pickup_date": pickup_date or dt.date.today().isoformat(),
        "category_id": category_id or default_category_id(),
        "package_items": package_items,
        "package_dimension": package_dimension or {"length": 20, "width": 20, "height": 10},
    }
    data = _post("/shipping/fetch_rates", payload)
    if not data:
        return None
    options = [_to_option(c) for c in data.get("couriers", [])]
    return {
        "request_token": data.get("request_token"),
        "options": [o for o in options if o is not None],
    }


def create_shipment(*, request_token: str, courier_id: str, service_code: str) -> dict[str, Any] | None:
    """Book the chosen courier. Returns ``{"order_id", "tracking_url", "courier",
    "delivery_eta"}`` or None. The shipping fee is drawn from the Shipbubble wallet."""
    data = _post(
        "/shipping/labels",
        {
            "request_token": request_token,
            "service_code": service_code,
            "courier_id": courier_id,
        },
    )
    if not data:
        return None
    return {
        "order_id": data.get("order_id"),
        "tracking_url": data.get("tracking_url"),
        "courier": (data.get("courier") or {}).get("name"),
        "status": data.get("status"),
    }


def cancel_shipment(order_id: str) -> bool:
    """Cancel a booked shipment (e.g. on a refund before delivery) to reclaim the
    delivery fee to the wallet. Returns True on success, False otherwise. Never
    raises — a failed cancel just means we couldn't reclaim the fee."""
    if not order_id:
        return False
    data = _post("/shipping/labels/cancel", {"order_id": order_id})
    return data is not None


def within_hours(option: DeliveryOption, max_hours: int = 24) -> bool:
    """True if a courier option's ETA is within ``max_hours``.

    Same-day and N-hour services qualify; any multi-'working day' ETA does not.
    Used to show only fast couriers on same-state storefront deliveries, e.g.
    'Same day delivery' / 'Within 6 hrs' / '24 Hours after pickup' → True, while
    'Within 1 - 2 working days' → False.
    """
    text = " ".join(s.lower() for s in (option.delivery_eta, option.delivery_eta_time) if s)
    if not text:
        return False
    if "same day" in text or "same-day" in text:
        return True
    if re.search(r"\bday", text):  # any "working day(s)" → beyond the 24h window
        return False
    hours = [int(n) for n in re.findall(r"(\d+)\s*(?:hr|hrs|hour|hours)\b", text)]
    return bool(hours) and max(hours) <= max_hours


def _to_option(c: dict[str, Any]) -> DeliveryOption | None:
    try:
        return DeliveryOption(
            courier_id=str(c["courier_id"]),
            service_code=str(c["service_code"]),
            name=str(c.get("courier_name") or "Courier"),
            image=c.get("courier_image"),
            amount=float(c.get("rate_card_amount") or c.get("total") or 0),
            wallet_total=float(c.get("total") or 0),
            currency=str(c.get("currency") or "NGN"),
            delivery_eta=c.get("delivery_eta"),
            delivery_eta_time=c.get("delivery_eta_time"),
            service_type=c.get("service_type"),
            dropoff_station=c.get("dropoff_station"),
        )
    except Exception:  # noqa: BLE001 — skip a malformed courier entry
        return None
