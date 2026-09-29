"""Flutterwave collector — collects to the FW payout balance so funds are held
there (v3 Standard payments). Off unless ``ESCROW_COLLECTOR_PROVIDER`` is
``flutterwave`` and ``FLUTTERWAVE_SECRET`` is set.
"""

from __future__ import annotations

import logging

import httpx

from app.core.config import settings

from .base import ChargeInit, ChargeStatus, CollectionError, CollectionProvider

logger = logging.getLogger(__name__)


def _normalize_status(raw_status: str | None) -> str:
    s = (raw_status or "").strip().lower()
    if s == "successful":
        return "successful"
    if s in {"failed", "cancelled", "canceled"}:
        return "failed"
    if s in {"pending", "new", "initiated", "processing"}:
        return "pending"
    return "unknown"


class FlutterwaveCollectionProvider(CollectionProvider):
    """Collects the held amount to the Flutterwave NGN payout balance."""

    name = "flutterwave"

    def _base(self) -> str:
        return settings.FLUTTERWAVE_BASE.rstrip("/")

    def _headers(self) -> dict[str, str]:
        if not settings.FLUTTERWAVE_SECRET:
            raise CollectionError("FLUTTERWAVE_SECRET is not configured")
        return {
            "Authorization": f"Bearer {settings.FLUTTERWAVE_SECRET}",
            "Content-Type": "application/json",
        }

    def initialize_hold_charge(
        self,
        *,
        amount_kobo: int,
        reference: str,
        customer_email: str,
        customer_phone: str | None,
        customer_name: str | None,
        callback_url: str,
        narration: str,
        metadata: dict,
    ) -> ChargeInit:
        payload = {
            "tx_ref": reference,
            "amount": round(amount_kobo / 100, 2),  # Naira (major unit)
            "currency": "NGN",
            "redirect_url": callback_url,
            "customer": {
                "email": customer_email,
                "phonenumber": customer_phone or "",
                "name": customer_name or customer_email,
            },
            "customizations": {"title": "SuoOps", "description": narration},
            "meta": metadata,
        }
        # Buyer-protection holds are collected by BANK TRANSFER ONLY — NIP transfers
        # are irreversible, so a fraudster can't pay with a stolen card then charge
        # back. (payment_options "banktransfer" = pay to a temporary virtual account.)
        if settings.ESCROW_HOLD_BANK_TRANSFER_ONLY:
            payload["payment_options"] = "banktransfer"
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.post(f"{self._base()}/v3/payments", headers=self._headers(), json=payload)
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — network/timeout
            raise CollectionError(f"Charge init failed: {exc}") from exc
        if data.get("status") != "success":
            raise CollectionError(data.get("message", "Charge initialization failed"))
        link = (data.get("data") or {}).get("link")
        if not link:
            raise CollectionError("Flutterwave did not return a payment link")
        return ChargeInit(authorization_url=link, reference=reference, provider=self.name, raw=data)

    def verify_charge(self, reference: str) -> ChargeStatus:
        """Authoritative status via verify-by-reference (our tx_ref)."""
        try:
            with httpx.Client(timeout=15) as client:
                resp = client.get(
                    f"{self._base()}/v3/transactions/verify_by_reference",
                    headers=self._headers(),
                    params={"tx_ref": reference},
                )
            data = resp.json()
        except Exception:  # noqa: BLE001 — transport error → indeterminate
            return ChargeStatus(status="unknown")
        if data.get("status") != "success":
            return ChargeStatus(status="unknown", raw=data)
        d = data.get("data") or {}
        amount = d.get("amount")
        return ChargeStatus(
            status=_normalize_status(d.get("status")),
            # FW reports Naira (major unit) — normalize to kobo.
            amount_kobo=int(round(float(amount) * 100)) if amount is not None else None,
            currency=d.get("currency"),
            provider_tx_id=str(d.get("id")) if d.get("id") is not None else None,
            raw=data,
        )

    def refund(self, *, reference: str, amount_kobo: int, note: str) -> dict:
        # FW refunds are by transaction id — resolve it from our tx_ref first.
        status = self.verify_charge(reference)
        if not status.provider_tx_id:
            raise CollectionError(f"No Flutterwave transaction found for {reference!r}")
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(
                    f"{self._base()}/v3/transactions/{status.provider_tx_id}/refund",
                    headers=self._headers(),
                    json={"amount": round(amount_kobo / 100, 2)},  # Naira
                )
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 — network/timeout
            raise CollectionError(f"Refund request failed: {exc}") from exc
        if data.get("status") != "success":
            raise CollectionError(f"Refund failed: {data.get('message')}")
        return data
