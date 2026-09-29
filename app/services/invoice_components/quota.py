"""Quota/plan helpers extracted from InvoiceService.

NEW BILLING MODEL:
- Invoice balance based (not monthly limits)
- 100 invoices = ₦2,500 per pack
- All plans can purchase packs
- Balance is decremented on revenue invoice creation
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from app.core.exceptions import InvoiceBalanceExhaustedError, UserNotFoundError
from app.models import models
from app.utils.feature_gate import (
    MANUAL_INVOICE_MIN_FEE_KOBO,
    WALLET_TOPUP_TIERS,
    platform_fee_kobo,
)

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class InvoiceQuotaMixin:
    """Provides invoice wallet utilities for invoice flows."""

    db: Session

    def _wallet_kobo(self, user) -> int:
        """Prepaid wallet balance in kobo (the active billing field)."""
        return int(getattr(user, "wallet_balance_kobo", 0) or 0)

    def check_invoice_quota(self, issuer_id: int) -> dict[str, object]:
        """Check the user's wallet and return quota info."""
        user = self.db.query(models.User).filter(models.User.id == issuer_id).one_or_none()
        if not user:
            raise UserNotFoundError()

        wallet = self._wallet_kobo(user)
        can_create = wallet >= MANUAL_INVOICE_MIN_FEE_KOBO
        naira = wallet / 100

        if not can_create:
            message = (
                "Your invoice wallet is empty. Top up to keep creating invoices, "
                "or share your storefront link so customers order and pay online."
            )
        elif wallet < 50000:  # under ₦500
            message = f"⚠️ Wallet low: ₦{naira:,.0f} left. Top up soon."
        else:
            message = f"Wallet balance: ₦{naira:,.0f}"

        return {
            "can_create": can_create,
            "plan": user.plan.value,
            "wallet_balance_kobo": wallet,
            "wallet_balance_naira": naira,
            # Legacy key: rough "invoices left" at the minimum fee.
            "invoice_balance": wallet // MANUAL_INVOICE_MIN_FEE_KOBO,
            "topup_from": WALLET_TOPUP_TIERS[0],
            "message": message,
        }

    def enforce_quota(self, issuer_id: int, invoice_type: str, amount=None) -> None:
        """Raise if the wallet can't cover this revenue invoice's fee."""
        if invoice_type != "revenue":
            return  # Expense invoices don't consume the wallet

        fee = platform_fee_kobo(amount, channel="manual")
        # Lock the user row to serialise concurrent invoice creation against the
        # wallet balance (race condition).
        user = self.db.query(models.User).with_for_update().filter(models.User.id == issuer_id).one_or_none()
        if not user:
            raise UserNotFoundError()
        if self._wallet_kobo(user) < fee:
            raise InvoiceBalanceExhaustedError(
                balance=self._wallet_kobo(user),
                pack_price=WALLET_TOPUP_TIERS[0],
            )

    def deduct_invoice_balance(self, issuer_id: int, amount=None) -> None:
        """Charge the manual-invoice fee from the wallet after creation."""
        fee = platform_fee_kobo(amount, channel="manual")
        # Lock the user row so concurrent deductions serialise and cannot both
        # read the same balance and overdraw the wallet (race condition).
        user = self.db.query(models.User).with_for_update().filter(models.User.id == issuer_id).one_or_none()
        if user and self._wallet_kobo(user) >= fee:
            user.wallet_balance_kobo = self._wallet_kobo(user) - fee
            self.db.commit()
            logger.info(
                "Charged ₦%.2f from user %s wallet (remaining ₦%.2f)",
                fee / 100,
                issuer_id,
                self._wallet_kobo(user) / 100,
            )

            # Sync low balance status to Brevo (best-effort, fire-and-forget)
            try:
                from app.services.brevo_service import sync_low_balance_status
                from app.utils.async_utils import run_async

                run_async(sync_low_balance_status(user))
            except Exception as e:
                logger.debug("Brevo low balance sync skipped: %s", e)
