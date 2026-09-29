"""Flutterwave payout provider (alternative rail).

Off unless ``settings.ESCROW_PAYOUT_PROVIDER == "flutterwave"`` and
``FLUTTERWAVE_SECRET`` is set. Flutterwave has no persistent "recipient" — each
transfer carries the destination bank + account directly. Amounts are in the
major unit (Naira), so we convert from kobo.

NOTE: needs a live sandbox run before enabling in production — behaviour is
built from Flutterwave's public v3 Transfers docs but not exercised against a
real account here.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings

from .base import PayoutError, PayoutProvider, PayoutResult

if TYPE_CHECKING:  # pragma: no cover
    from app.models import models

logger = logging.getLogger(__name__)

# Cached Flutterwave NG bank list (normalized name -> code).
_fw_bank_cache: dict[str, str] = {}
_fw_bank_cache_at: float = 0.0
_BANK_CACHE_TTL = 24 * 60 * 60  # 24h

# Short-lived {reference -> normalized status} map of RECENT transfers. FW v3 has
# no get-by-reference, so we page through recent transfers once and reuse the map
# for a burst of status checks (e.g. reconciling a whole business's held orders)
# instead of re-scanning per order. Successful is terminal, so a brief cache is
# safe; a not-yet-cached success just confirms on the next reconcile pass.
_fw_transfer_cache: dict[str, str] = {}
_fw_transfer_cache_at: float = 0.0
_TRANSFER_CACHE_TTL = 20  # seconds
_TRANSFER_MAX_PAGES = 40  # scan deep enough to find same-day transfers


def _normalize_bank_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


# Nigerian neobanks/fintechs are often listed under their licensed MFB name, so
# the exact name a seller saved (e.g. "Kuda Bank") won't match a provider's list
# entry (e.g. "Kuda Microfinance Bank"). These aliases bridge the common cases.
_BANK_ALIASES: dict[str, list[str]] = {
    "kudabank": ["kudamicrofinancebank", "kuda"],
    "kuda": ["kudamicrofinancebank", "kudabank"],
    "opay": ["opaydigitalservices", "opaydigitalservicesltd", "paycomopay"],
    "opaydigitalservices": ["opay", "paycomopay"],
    "palmpay": ["palmpaylimited", "palmpay"],
    "moniepoint": ["moniepointmfb", "moniepointmicrofinancebank"],
    "moniepointmfb": ["moniepointmicrofinancebank", "moniepoint"],
    "paycom": ["opay", "opaydigitalservices"],
}

# Filler tokens dropped to compare bank "cores" (e.g. "Kuda MFB" ≈ "Kuda Bank").
_BANK_FILLER = (
    "microfinancebank",
    "microfinance",
    "digitalservices",
    "mfb",
    "bank",
    "plc",
    "limited",
    "ltd",
    "nigeria",
)


def _bank_core(normalized: str) -> str:
    core = normalized
    for token in _BANK_FILLER:
        core = core.replace(token, "")
    return core


def _match_bank_code(target: str, mapping: dict[str, str]) -> str | None:
    """Resolve a bank name to its code, tolerant of provider naming differences.

    Order: exact normalized → known alias → unambiguous 'core' match (filler
    tokens stripped) → unambiguous prefix match. Ambiguous matches are refused
    (return None) so we never pay the wrong bank.
    """
    key = _normalize_bank_name(target)
    if key in mapping:
        return mapping[key]
    for alias in _BANK_ALIASES.get(key, []):
        if alias in mapping:
            return mapping[alias]
    tcore = _bank_core(key)
    if tcore:
        core_hits = {code for name, code in mapping.items() if _bank_core(name) == tcore}
        if len(core_hits) == 1:
            return next(iter(core_hits))
        prefix_hits = {
            code
            for name, code in mapping.items()
            if name.startswith(key) or key.startswith(name) or _bank_core(name).startswith(tcore)
        }
        if len(prefix_hits) == 1:
            return next(iter(prefix_hits))
    return None


def _normalize_transfer_status(raw_status: str | None) -> str:
    """Map a Flutterwave transfer state to our normalized status."""
    s = (raw_status or "").strip().upper()
    if s == "SUCCESSFUL":
        return "successful"
    if s in {"FAILED", "CANCELLED", "CANCELED"}:
        return "failed"
    if s in {"NEW", "PENDING", "INITIATED", "PROCESSING", "QUEUED"}:
        return "pending"
    return "unknown"


class FlutterwavePayoutProvider(PayoutProvider):
    """Pays sellers via the Flutterwave v3 Transfers API."""

    name = "flutterwave"

    def _base(self) -> str:
        return settings.FLUTTERWAVE_BASE.rstrip("/")

    def _http_client(self, timeout: int) -> httpx.Client:
        """httpx client for Flutterwave calls, routed through the static-IP proxy
        when configured. FW only bypasses transfer 2FA from a WHITELISTED IP, so
        on dynamic Render IPs the proxy (a cheap static-IP egress) is what makes
        payouts work. No proxy set → direct connection (unchanged behaviour)."""
        proxy = settings.FLUTTERWAVE_TRANSFER_PROXY
        if proxy:
            return httpx.Client(timeout=timeout, proxy=proxy)
        return httpx.Client(timeout=timeout)

    def _headers(self) -> dict[str, str]:
        if not settings.FLUTTERWAVE_SECRET:
            raise PayoutError("FLUTTERWAVE_SECRET is not configured")
        return {
            "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET}",
            "Content-Type": "application/json",
        }

    def _resolve_bank_code(self, bank_name: str) -> str:
        global _fw_bank_cache, _fw_bank_cache_at

        now = time.time()
        if not _fw_bank_cache or (now - _fw_bank_cache_at) > _BANK_CACHE_TTL:
            with self._http_client(20) as client:
                resp = client.get(f"{self._base()}/v3/banks/NG", headers=self._headers())
            data = resp.json()
            if data.get("status") != "success":
                raise PayoutError(f"Could not load bank list: {data.get('message')}")
            _fw_bank_cache = {
                _normalize_bank_name(b["name"]): str(b["code"])
                for b in data.get("data", [])
                if b.get("name") and b.get("code")
            }
            _fw_bank_cache_at = now

        code = _match_bank_code(bank_name, _fw_bank_cache)
        if not code:
            raise PayoutError(
                f"Unknown bank: {bank_name!r} — couldn't match it to a Flutterwave "
                "bank. Ask the seller to re-select their bank from the list."
            )
        return code

    def _available_balance_naira(self) -> float | None:
        """Available NGN payout-wallet balance in Naira, or None if unreadable."""
        try:
            with self._http_client(15) as client:
                resp = client.get(f"{self._base()}/v3/balances/NGN", headers=self._headers())
            data = resp.json()
        except Exception:  # noqa: BLE001 — transport error → skip the guard
            return None
        if data.get("status") != "success":
            return None
        bal = data.get("data") or {}
        if not isinstance(bal, dict):
            return None
        avail = bal.get("available_balance")
        try:
            return float(avail)
        except (TypeError, ValueError):
            return None

    def transfer(
        self,
        db: Session,
        *,
        seller: models.User,
        amount_kobo: int,
        reference: str,
        reason: str,
    ) -> PayoutResult:
        account_number = seller.payout_account_number or seller.account_number
        bank_name = seller.payout_bank_name or seller.bank_name
        if not (account_number and bank_name):
            raise PayoutError("Seller has no bank details set for payouts")

        bank_code = self._resolve_bank_code(bank_name)
        amount_naira = round(amount_kobo / 100, 2)

        # Pre-transfer float guard: FW transfers debit the payout balance. Fail
        # fast with a clear error (so the escrow stays 'held' to retry) instead of
        # queuing an under-funded transfer. Skipped when the balance is unreadable.
        available = self._available_balance_naira()
        if available is not None and available < amount_naira:
            raise PayoutError(
                f"Flutterwave payout balance too low: have ₦{available:.2f}, " f"need ₦{amount_naira:.2f}"
            )

        try:
            with self._http_client(20) as client:
                resp = client.post(
                    f"{self._base()}/v3/transfers",
                    headers=self._headers(),
                    json={
                        "account_bank": bank_code,
                        "account_number": account_number,
                        "amount": amount_naira,  # Naira (major unit)
                        "narration": reason,
                        "currency": "NGN",
                        "debit_currency": "NGN",
                        "reference": reference,
                    },
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — network/timeout → retry later
            raise PayoutError(f"Transfer request failed: {exc}") from exc

        return PayoutResult(
            ok=data.get("status") == "success",
            reference=reference,
            provider=self.name,
            message=data.get("message"),
            status=_normalize_transfer_status((data.get("data") or {}).get("status")),
            raw=data,
        )

    def transfer_status(self, reference: str) -> str:
        """Normalized disbursement status. FW v3 has no get-by-reference, so we
        build a {reference -> status} map from recent transfers (paged deeply and
        briefly cached) and look this reference up in it. Returns ``unknown`` only
        when the reference truly isn't among recent transfers."""
        return self._recent_transfer_map().get(reference, "unknown")

    def _recent_transfer_map(self) -> dict[str, str]:
        global _fw_transfer_cache, _fw_transfer_cache_at

        now = time.time()
        if _fw_transfer_cache and (now - _fw_transfer_cache_at) < _TRANSFER_CACHE_TTL:
            return _fw_transfer_cache

        mapping: dict[str, str] = {}
        try:
            with self._http_client(20) as client:
                for page in range(1, _TRANSFER_MAX_PAGES + 1):
                    resp = client.get(
                        f"{self._base()}/v3/transfers",
                        headers=self._headers(),
                        params={"page": page},
                    )
                    data = resp.json()
                    rows = data.get("data") or []
                    if not rows:
                        break
                    for t in rows:
                        ref = t.get("reference")
                        # First (most recent) status for a reference wins.
                        if ref and ref not in mapping:
                            mapping[ref] = _normalize_transfer_status(t.get("status"))
        except Exception:  # noqa: BLE001 — transport error → keep last-known map
            return _fw_transfer_cache or {}

        _fw_transfer_cache = mapping
        _fw_transfer_cache_at = now
        return mapping
