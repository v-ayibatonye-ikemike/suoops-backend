"""Server-side reverse geocoding (lat/lng -> Nigerian state/city) via Mapbox.

Used to derive a TRUSTED state from GPS coordinates for the escrow same/different
-state window — the client sends coordinates, the server decides the state, so a
client can't fake "same state" to shorten the hold.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

_MAPBOX_GEOCODE = "https://api.mapbox.com/geocoding/v5/mapbox.places"


def reverse_geocode(lat: float, lng: float) -> tuple[str | None, str | None]:
    """Return ``(state, city)`` for a coordinate, or ``(None, None)`` if it can't
    be resolved (e.g. no token configured). Never raises."""
    token = settings.MAPBOX_TOKEN
    if not token:
        logger.info("MAPBOX_TOKEN not set — skipping reverse geocode")
        return (None, None)
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{_MAPBOX_GEOCODE}/{lng},{lat}.json",
                # No `types`/`limit`: Mapbox reverse geocoding returns HTTP 422 for
                # a multi-type request combined with a limit. The single default
                # feature carries the full region/place hierarchy in `context`.
                params={
                    "access_token": token,
                    "country": "NG",
                },
            )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — geocoding must never break the request
        logger.warning("Reverse geocode failed: %s", exc)
        return (None, None)

    state: str | None = None
    city: str | None = None
    for feat in data.get("features", []):
        place_types = feat.get("place_type", [])
        text = (feat.get("text") or "").strip() or None
        if "region" in place_types and not state:
            state = text
        if "place" in place_types and not city:
            city = text
        # The most specific feature (address/place) lists its parent region/place
        # in `context` — pull the state/city from there.
        for ctx in feat.get("context", []):
            cid = str(ctx.get("id", ""))
            ctext = (ctx.get("text") or "").strip() or None
            if cid.startswith("region") and not state:
                state = ctext
            elif cid.startswith("place") and not city:
                city = ctext
        if state and city:
            break
    if not state:
        # Token is set but no state resolved — surface WHY (invalid/deprecated
        # token, no NG coverage, error body) so state=None is diagnosable instead
        # of silent. Mapbox puts the reason in `message` on error responses.
        msg = data.get("message") if isinstance(data, dict) else None
        logger.warning(
            "Reverse geocode resolved no state for (%.5f, %.5f): status=%s msg=%s features=%d",
            lat,
            lng,
            getattr(resp, "status_code", "?"),
            msg,
            len(data.get("features", [])),
        )
    return (state, city)


def reverse_geocode_address(lat: float, lng: float) -> str | None:
    """Return a human-readable address for a coordinate (e.g. "12 Bode Thomas St,
    Surulere, Lagos"), or ``None`` if it can't be resolved. Best-effort; never
    raises. Used to send the buyer's delivery location to the seller as readable
    text (not just a raw map pin)."""
    token = settings.MAPBOX_TOKEN
    if not token:
        return None
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.get(
                f"{_MAPBOX_GEOCODE}/{lng},{lat}.json",
                # Default single feature: its `place_name` is the full readable
                # address. (A multi-type request with a limit returns HTTP 422.)
                params={
                    "access_token": token,
                    "country": "NG",
                },
            )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — geocoding must never break the request
        logger.warning("Reverse geocode (address) failed: %s", exc)
        return None
    feats = data.get("features", [])
    if not feats:
        return None
    # Mapbox's place_name is the full readable address ("…, Surulere, Lagos, Nigeria").
    name = (feats[0].get("place_name") or "").strip()
    # Drop a trailing ", Nigeria" — the seller already knows the country.
    if name.endswith(", Nigeria"):
        name = name[: -len(", Nigeria")]
    return name or None
