"""Paystack payout provider + collector refund (the default rail)."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from app.core.config import settings
from app.services.paystack_http import paystack_client

from .base import PayoutError, PayoutProvider, PayoutResult

if TYPE_CHECKING:  # pragma: no cover
    from app.models import models

logger = logging.getLogger(__name__)

_PAYSTACK_BASE = "https://api.paystack.co"

# Cached Paystack bank list (normalized name -> code); rarely changes.
_bank_cache: dict[str, str] = {}
_bank_cache_at: float = 0.0
_BANK_CACHE_TTL = 24 * 60 * 60  # 24h


def _headers() -> dict[str, str]:
    if not settings.PAYSTACK_SECRET:
        raise PayoutError("PAYSTACK_SECRET is not configured")
    return {
        "Authorization": f"Bearer {settings.PAYSTACK_SECRET}",
        "Content-Type": "application/json",
    }


def _normalize_bank_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _normalize_transfer_status(raw_status: str | None) -> str:
    """Map a Paystack transfer state to our normalized status."""
    s = (raw_status or "").strip().lower()
    if s == "success":
        return "successful"
    if s in {"failed", "reversed", "abandoned"}:
        return "failed"
    if s in {"pending", "otp", "processing", "received", "queued"}:
        return "pending"
    return "unknown"


def _resolve_bank_code(bank_name: str) -> str:
    global _bank_cache, _bank_cache_at

    now = time.time()
    if not _bank_cache or (now - _bank_cache_at) > _BANK_CACHE_TTL:
        with paystack_client(timeout=20) as client:
            resp = client.get(
                f"{_PAYSTACK_BASE}/bank",
                headers=_headers(),
                params={"currency": "NGN", "perPage": 100},
            )
        data = resp.json()
        if not data.get("status"):
            raise PayoutError(f"Could not load bank list: {data.get('message')}")
        _bank_cache = {_normalize_bank_name(b["name"]): b["code"] for b in data.get("data", [])}
        _bank_cache_at = now

    code = _bank_cache.get(_normalize_bank_name(bank_name))
    if not code:
        raise PayoutError(f"Unknown bank: {bank_name!r}")
    return code


class PaystackPayoutProvider(PayoutProvider):
    """Pays sellers via Paystack Transfers (``source: balance``)."""

    name = "paystack"

    def _ensure_recipient(self, db: Session, user: models.User) -> str:
        """Return the seller's Paystack Transfer Recipient code, creating it once."""
        if user.paystack_recipient_code:
            return user.paystack_recipient_code

        account_number = user.payout_account_number or user.account_number
        bank_name = user.payout_bank_name or user.bank_name
        account_name = user.payout_account_name or user.account_name or user.business_name or user.name
        if not (account_number and bank_name):
            raise PayoutError("Seller has no bank details set for payouts")

        bank_code = _resolve_bank_code(bank_name)

        with paystack_client(timeout=20) as client:
            resp = client.post(
                f"{_PAYSTACK_BASE}/transferrecipient",
                headers=_headers(),
                json={
                    "type": "nuban",
                    "name": account_name,
                    "account_number": account_number,
                    "bank_code": bank_code,
                    "currency": "NGN",
                },
            )
        data = resp.json()
        if not data.get("status"):
            raise PayoutError(f"Could not create transfer recipient: {data.get('message')}")

        code = (data.get("data") or {}).get("recipient_code")
        if not code:
            raise PayoutError("Paystack did not return a recipient code")

        user.paystack_recipient_code = code
        db.commit()
        logger.info("Created Paystack transfer recipient for seller %s", user.id)
        return code

    def transfer(
        self,
        db: Session,
        *,
        seller: models.User,
        amount_kobo: int,
        reference: str,
        reason: str,
    ) -> PayoutResult:
        recipient = self._ensure_recipient(db, seller)
        try:
            with paystack_client(timeout=20) as client:
                resp = client.post(
                    f"{_PAYSTACK_BASE}/transfer",
                    headers=_headers(),
                    json={
                        "source": "balance",
                        "amount": int(amount_kobo),
                        "recipient": recipient,
                        "reason": reason,
                        "reference": reference,
                    },
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — network/timeout → retry later
            raise PayoutError(f"Transfer request failed: {exc}") from exc

        return PayoutResult(
            ok=bool(data.get("status")),
            reference=reference,
            provider=self.name,
            message=data.get("message"),
            status=_normalize_transfer_status((data.get("data") or {}).get("status")),
            raw=data,
        )

    def transfer_status(self, reference: str) -> str:
        """Normalized disbursement status via Paystack's verify-by-reference."""
        try:
            with paystack_client(timeout=15) as client:
                resp = client.get(f"{_PAYSTACK_BASE}/transfer/verify/{reference}", headers=_headers())
            data = resp.json()
        except Exception:  # noqa: BLE001 — transport error → indeterminate
            return "unknown"
        if not data.get("status"):
            return "unknown"
        return _normalize_transfer_status((data.get("data") or {}).get("status"))


def paystack_refund(*, charge_reference: str, amount_kobo: int, note: str) -> dict:
    """Refund a Paystack charge (the collector). Raises PayoutError on failure."""
    try:
        with paystack_client(timeout=20) as client:
            resp = client.post(
                f"{_PAYSTACK_BASE}/refund",
                headers=_headers(),
                json={
                    "transaction": charge_reference,
                    "amount": int(amount_kobo),
                    "merchant_note": note,
                },
            )
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 — network/timeout → retry later
        raise PayoutError(f"Refund request failed: {exc}") from exc

    if data.get("status"):
        # Paystack accepts the refund (top-level status:true); the refund itself
        # is then 'pending'/'processing' → 'processed'. Only an explicit failure
        # means the buyer won't be paid — surface that so the caller doesn't mark
        # the order refunded when Paystack actually rejected it.
        refund_status = str((data.get("data") or {}).get("status") or "").lower()
        if refund_status in ("failed", "cancelled", "canceled", "declined", "reversed"):
            raise PayoutError(f"Refund not accepted by Paystack: {refund_status}")
        return data
    raise PayoutError(f"Refund failed: {data.get('message')}")
