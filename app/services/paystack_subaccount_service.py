"""
Paystack subaccount service.

Creates and maintains one Paystack subaccount per business so that online
invoice / marketplace payments can be settled directly to the business's bank
account via Paystack split payments, with SuoOps retaining a commission.

Flow (all automated, no manual dashboard work):
    1. Resolve the business's bank name -> Paystack bank code.
    2. Verify the account number with Paystack (/bank/resolve) -> real account name.
    3. Create (or update) a Paystack subaccount -> ACCT_xxx code.
    4. Persist the code on the User row and mark it active.

The subaccount's ``percentage_charge`` is the platform commission SuoOps keeps;
the business receives the remainder, settled by Paystack to their bank.
"""

from __future__ import annotations

import logging
import time

from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import models
from app.services.paystack_http import paystack_async_client

logger = logging.getLogger(__name__)

_PAYSTACK_BASE = "https://api.paystack.co"

# Cache the Paystack bank list (name -> code) for a while; it rarely changes.
_bank_cache: dict[str, str] = {}
_bank_cache_at: float = 0.0
_BANK_CACHE_TTL = 24 * 60 * 60  # 24h


class SubaccountError(Exception):
    """Raised when a Paystack subaccount cannot be created/verified."""


def _normalize_bank_name(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


class PaystackSubaccountService:
    def __init__(self, db: Session, secret: str | None = None, commission_percent: float | None = None):
        self.db = db
        self.secret = secret or settings.PAYSTACK_SECRET
        if not self.secret:
            raise SubaccountError("PAYSTACK_SECRET is not configured")
        self.commission_percent = (
            commission_percent if commission_percent is not None else settings.PAYSTACK_PLATFORM_COMMISSION_PERCENT
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}", "Content-Type": "application/json"}

    async def _list_banks(self) -> dict[str, str]:
        """Return a {normalized_bank_name: bank_code} map (cached)."""
        global _bank_cache, _bank_cache_at
        if _bank_cache and (time.time() - _bank_cache_at) < _BANK_CACHE_TTL:
            return _bank_cache
        async with paystack_async_client(timeout=15.0) as client:
            resp = await client.get(
                f"{_PAYSTACK_BASE}/bank",
                headers=self._headers,
                params={"currency": "NGN", "perPage": 200},
            )
        if resp.status_code != 200:
            raise SubaccountError(f"Could not fetch Paystack bank list ({resp.status_code})")
        banks = resp.json().get("data", []) or []
        mapping = {_normalize_bank_name(b["name"]): b["code"] for b in banks if b.get("name") and b.get("code")}
        _bank_cache = mapping
        _bank_cache_at = time.time()
        return mapping

    async def resolve_bank_code(self, bank_name: str) -> str:
        banks = await self._list_banks()
        key = _normalize_bank_name(bank_name)
        code = banks.get(key)
        if code:
            return code
        # Fall back to a loose contains-match (e.g. "GTBank" vs "Guaranty Trust Bank")
        for name, c in banks.items():
            if key and (key in name or name in key):
                return c
        raise SubaccountError(f"Unrecognized bank name: {bank_name!r}")

    async def resolve_account(self, account_number: str, bank_code: str) -> str:
        """Verify the account exists and return its real account name."""
        async with paystack_async_client(timeout=15.0) as client:
            resp = await client.get(
                f"{_PAYSTACK_BASE}/bank/resolve",
                headers=self._headers,
                params={"account_number": account_number, "bank_code": bank_code},
            )
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or not body.get("status"):
            raise SubaccountError(body.get("message") or f"Could not verify account {account_number}")
        name = (body.get("data") or {}).get("account_name")
        if not name:
            raise SubaccountError("Bank did not return an account name")
        return name

    async def _create_subaccount(
        self, business_name: str, bank_code: str, account_number: str, contact_email: str | None
    ) -> str:
        payload: dict[str, object] = {
            "business_name": business_name,
            "settlement_bank": bank_code,
            "account_number": account_number,
            "percentage_charge": self.commission_percent,
        }
        if contact_email:
            payload["primary_contact_email"] = contact_email
        async with paystack_async_client(timeout=15.0) as client:
            resp = await client.post(f"{_PAYSTACK_BASE}/subaccount", headers=self._headers, json=payload)
        body = resp.json() if resp.content else {}
        if resp.status_code not in (200, 201) or not body.get("status"):
            raise SubaccountError(body.get("message") or "Failed to create Paystack subaccount")
        code = (body.get("data") or {}).get("subaccount_code")
        if not code:
            raise SubaccountError("Paystack did not return a subaccount_code")
        return code

    async def _update_subaccount(self, subaccount_code: str, bank_code: str, account_number: str) -> None:
        payload = {
            "settlement_bank": bank_code,
            "account_number": account_number,
            "percentage_charge": self.commission_percent,
        }
        async with paystack_async_client(timeout=15.0) as client:
            resp = await client.put(
                f"{_PAYSTACK_BASE}/subaccount/{subaccount_code}",
                headers=self._headers,
                json=payload,
            )
        body = resp.json() if resp.content else {}
        if resp.status_code != 200 or not body.get("status"):
            raise SubaccountError(body.get("message") or "Failed to update Paystack subaccount")

    async def ensure_subaccount(self, user: models.User) -> str:
        """
        Create or update the user's Paystack subaccount from their bank details.

        Returns the subaccount code. Raises SubaccountError on any failure
        (e.g. missing/invalid bank details). Persists the code + active flag.
        """
        if not (user.bank_name and user.account_number):
            raise SubaccountError("Add your bank name and account number first")

        # Serialize concurrent enable-payments calls for the SAME user so we never
        # create two subaccounts (one would be orphaned and keep costing us
        # commission). Re-load the row under a lock; the second caller then sees
        # the code the first one committed and takes the update path instead.
        locked = self.db.query(models.User).filter(models.User.id == user.id).with_for_update().first()
        if locked is not None:
            user = locked

        bank_code = await self.resolve_bank_code(user.bank_name)
        verified_name = await self.resolve_account(user.account_number, bank_code)

        business_name = user.business_name or user.name or f"SuoOps business {user.id}"

        if user.paystack_subaccount_code:
            await self._update_subaccount(user.paystack_subaccount_code, bank_code, user.account_number)
            code = user.paystack_subaccount_code
        else:
            code = await self._create_subaccount(business_name, bank_code, user.account_number, user.email)
            user.paystack_subaccount_code = code

        # Keep the on-file verified name in sync (used on receipts / trust).
        if verified_name and not user.account_name:
            user.account_name = verified_name
        user.paystack_subaccount_active = True
        self.db.commit()
        logger.info(
            "Paystack subaccount ready for user %s (%s) -> %s",
            user.id,
            business_name,
            code,
        )
        return code
