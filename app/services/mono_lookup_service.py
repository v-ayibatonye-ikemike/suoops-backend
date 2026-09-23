"""Mono Lookup — BVN/CAC/TIN identity & registry verification.

Charged per-call directly to the requesting business's SuoOps wallet (never
absorbed by SuoOps in bulk) — see `_charge_wallet_kobo` below. This upgrades
tax/registration fields on `TaxProfile` from self-declared to independently
verified, which the Business Snapshot (`analytics_service`) surfaces as a
stronger trust signal.

IMPORTANT — endpoint paths not independently verified: Mono's API reference
(docs.mono.co) is a client-rendered doc site that could not be scraped while
building this integration, and there is no public server-side SDK listing
exact Lookup routes (only Connect-widget frontend SDKs exist in Mono's public
GitHub org). The auth header (`mono-sec-key`) is a well-established Mono
convention used across their whole API, so that part is solid. The specific
path constants below (`_TIN_LOOKUP_PATH`, `_CAC_LOOKUP_PATH`) are this
integration's best-effort guess at Mono's REST conventions and MUST be
confirmed against the API reference in the Mono dashboard (Documentation
link after logging into app.mono.co) before this is enabled with a real
MONO_SECRET_KEY in production.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.exceptions import (
    ConfigurationError,
    InvalidCACError,
    InvalidTINError,
    LookupBalanceExhaustedError,
    ServiceUnavailableError,
)
from app.models import models
from app.models.tax_models import TaxProfile

logger = logging.getLogger(__name__)

# Mono's published Lookup pricing (naira), converted to kobo. Charged at cost
# — no SuoOps markup — since this is opt-in and paid from the business's own
# wallet, not absorbed platform-wide.
LOOKUP_COST_KOBO = {
    "tin": 5_000,   # ₦50 — generic lookup rate (Mono doesn't list a TIN-specific rate)
    "cac": 1_000,   # ₦10
    "bvn": 1_500,   # ₦15
}

# See module docstring — confirm against Mono's dashboard reference docs.
_TIN_LOOKUP_PATH = "/v2/lookup/tin"
_CAC_LOOKUP_PATH = "/v2/lookup/cac"


def _charge_wallet_kobo(db: Session, user_id: int, fee_kobo: int, reason: str) -> None:
    """Deduct `fee_kobo` from the user's prepaid wallet, or raise if it can't cover it.

    Mirrors the row-locking pattern in InvoiceQuotaMixin.deduct_invoice_balance
    so concurrent charges against the same wallet can't race past the balance
    check (same reasoning: lock the row, check, deduct, commit).
    """
    user = (
        db.query(models.User)
        .with_for_update()
        .filter(models.User.id == user_id)
        .one_or_none()
    )
    if not user:
        raise ValueError("User not found")
    balance = int(getattr(user, "wallet_balance_kobo", 0) or 0)
    if balance < fee_kobo:
        raise LookupBalanceExhaustedError(balance_kobo=balance, required_kobo=fee_kobo)
    user.wallet_balance_kobo = balance - fee_kobo
    db.commit()
    logger.info(
        "Charged ₦%.2f from user %s wallet for %s (remaining ₦%.2f)",
        fee_kobo / 100, user_id, reason, user.wallet_balance_kobo / 100,
    )


class MonoLookupClient:
    """Thin HTTP wrapper around Mono's Lookup API."""

    def _headers(self) -> dict[str, str]:
        if not settings.MONO_SECRET_KEY:
            raise ConfigurationError("MONO_SECRET_KEY")
        return {
            "mono-sec-key": settings.MONO_SECRET_KEY,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        try:
            with httpx.Client(timeout=20) as client:
                resp = client.post(
                    f"{settings.MONO_BASE_URL.rstrip('/')}{path}",
                    headers=self._headers(),
                    json=payload,
                )
            return {"status_code": resp.status_code, "data": resp.json()}
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("Mono", reason=str(exc)) from exc

    def verify_tin(self, tin: str) -> dict:
        """Returns Mono's raw response dict for a TIN lookup."""
        result = self._post(_TIN_LOOKUP_PATH, {"tin": tin})
        if result["status_code"] >= 400:
            reason = (result["data"] or {}).get("message")
            raise InvalidTINError(tin=tin, reason=reason)
        return result["data"]

    def verify_cac(self, rc_number: str) -> dict:
        """Returns Mono's raw response dict for a CAC/RC number lookup."""
        result = self._post(_CAC_LOOKUP_PATH, {"rc_number": rc_number})
        if result["status_code"] >= 400:
            reason = (result["data"] or {}).get("message")
            raise InvalidCACError(rc_number=rc_number, reason=reason)
        return result["data"]


def verify_business_tin(db: Session, user_id: int, client: MonoLookupClient | None = None) -> TaxProfile:
    """Verify the business's TIN via Mono and charge the wallet on success.

    Idempotent: if the TIN is already verified, returns immediately without
    calling Mono or charging again. The wallet is only charged AFTER Mono
    confirms the TIN is valid — a failed/invalid lookup costs nothing.
    """
    profile = db.query(TaxProfile).filter(TaxProfile.user_id == user_id).one_or_none()
    if not profile or not profile.tin:
        raise ValueError("Set your TIN in Tax settings before verifying it.")
    if profile.tin_verified:
        return profile

    client = client or MonoLookupClient()
    profile.verification_attempts = (profile.verification_attempts or 0) + 1
    db.commit()

    client.verify_tin(profile.tin)  # raises InvalidTINError on failure — no charge

    _charge_wallet_kobo(db, user_id, LOOKUP_COST_KOBO["tin"], reason="TIN verification")
    profile.mark_verified(tin=True)
    profile.last_verification_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(profile)
    return profile


def verify_business_cac(
    db: Session, user_id: int, rc_number: str, client: MonoLookupClient | None = None
) -> TaxProfile:
    """Verify the business's CAC/RC registration via Mono and charge the wallet.

    Same success-only charging + idempotency rules as `verify_business_tin`.
    """
    profile = db.query(TaxProfile).filter(TaxProfile.user_id == user_id).one_or_none()
    if not profile:
        raise ValueError("Tax profile not found.")
    if profile.cac_verified and profile.rc_number == rc_number:
        return profile

    client = client or MonoLookupClient()
    profile.verification_attempts = (profile.verification_attempts or 0) + 1
    db.commit()

    data = client.verify_cac(rc_number)  # raises InvalidCACError on failure — no charge
    registered_name = (data or {}).get("company_name") or (data or {}).get("name")

    _charge_wallet_kobo(db, user_id, LOOKUP_COST_KOBO["cac"], reason="CAC verification")
    profile.rc_number = rc_number
    profile.cac_registered_name = registered_name
    profile.mark_verified(cac=True)
    db.commit()
    db.refresh(profile)
    return profile
