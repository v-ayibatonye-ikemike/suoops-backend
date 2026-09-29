"""Distance-aware delivery/dispute window for cross-state storefront orders.

The flat 3-day cross-state window doesn't reflect Nigerian geography: Lagos→Abuja
is far quicker than Kaduna→Rivers. We scale the window by how far apart the
seller's and buyer's states are, using the six geopolitical zones arranged on a
rough south↔north / west↔east grid, then adding working days for the distance.

Pure functions only (no IO) so they're cheap and easy to test.
"""

from __future__ import annotations

from app.core.config import settings


def _norm(value: str | None) -> str | None:
    """Normalize a state name: lowercase, drop a trailing 'state', keep a-z0-9."""
    if not value:
        return None
    v = "".join(ch for ch in value.lower() if ch.isalnum() or ch == " ").strip()
    if v.endswith(" state"):
        v = v[: -len(" state")]
    return "".join(ch for ch in v if ch.isalnum()) or None


# Six geopolitical zones.
_NC, _NE, _NW, _SE, _SS, _SW = "NC", "NE", "NW", "SE", "SS", "SW"

_STATE_ZONE: dict[str, str] = {}
for _zone, _states in {
    _NC: ["benue", "kogi", "kwara", "nasarawa", "niger", "plateau", "fct", "abuja", "federalcapitalterritory"],
    _NE: ["adamawa", "bauchi", "borno", "gombe", "taraba", "yobe"],
    _NW: ["jigawa", "kaduna", "kano", "katsina", "kebbi", "sokoto", "zamfara"],
    _SE: ["abia", "anambra", "ebonyi", "enugu", "imo"],
    _SS: ["akwaibom", "bayelsa", "crossriver", "delta", "edo", "rivers"],
    _SW: ["ekiti", "lagos", "ogun", "ondo", "osun", "oyo"],
}.items():
    for _s in _states:
        _STATE_ZONE[_s] = _zone

# Rough grid position per zone: row = south(0)…north(2), col = west(0)…east(2).
_ZONE_POS: dict[str, tuple[int, int]] = {
    _SW: (0, 0),
    _SS: (0, 1),
    _SE: (0, 2),
    _NC: (1, 1),
    _NW: (2, 0),
    _NE: (2, 2),
}


def zone_for_state(state: str | None) -> str | None:
    """Geopolitical-zone code for a state name, or None if unknown."""
    key = _norm(state)
    return _STATE_ZONE.get(key) if key else None


# A few state names that refer to the same place (so same-state matching works
# regardless of how the seller typed it or how the geocoder labelled it).
_STATE_ALIASES: dict[str, str] = {
    "abuja": "fct",
    "federalcapitalterritory": "fct",
    "fctabuja": "fct",
    "abujafct": "fct",
}


def canonical_state(state: str | None) -> str | None:
    """Normalized, alias-resolved state key (e.g. 'Abuja' and 'FCT' → 'fct')."""
    key = _norm(state)
    if not key:
        return None
    return _STATE_ALIASES.get(key, key)


def same_state(a: str | None, b: str | None) -> bool:
    """True when two state names refer to the same Nigerian state."""
    ca, cb = canonical_state(a), canonical_state(b)
    return bool(ca and cb and ca == cb)


def cross_state_delivery_days(seller_state: str | None, buyer_state: str | None) -> int:
    """Working-day dispute/delivery window for a cross-state order, scaled by how
    far apart the two states' zones are.

    Base is ``ESCROW_CROSS_STATE_HOLD_DAYS`` (neighbouring/near zones); each extra
    step of zone distance adds a working day, capped at
    ``ESCROW_MAX_CROSS_STATE_HOLD_DAYS``. Unknown states fall back to the base.
    """
    base = settings.ESCROW_CROSS_STATE_HOLD_DAYS
    cap = settings.ESCROW_MAX_CROSS_STATE_HOLD_DAYS
    zs, zb = zone_for_state(seller_state), zone_for_state(buyer_state)
    if not zs or not zb:
        return base
    (r1, c1), (r2, c2) = _ZONE_POS[zs], _ZONE_POS[zb]
    dist = abs(r1 - r2) + abs(c1 - c2)  # Manhattan distance on the zone grid
    extra = max(0, dist - 1)
    return min(cap, base + extra)
