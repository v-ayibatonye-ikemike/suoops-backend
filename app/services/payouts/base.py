"""Pluggable seller-payout providers for storefront escrow.

Only the *payout* (moving held funds to the seller) varies by provider —
collections and refunds always go through the original collector (Paystack),
since a refund must reverse the exact charge. Select the active provider with
``settings.ESCROW_PAYOUT_PROVIDER``.
"""

from __future__ import annotations

import abc
import dataclasses
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

if TYPE_CHECKING:  # pragma: no cover
    from app.models import models


class PayoutError(Exception):
    """A payout (transfer) could not be completed — caller should retry later."""


@dataclasses.dataclass
class PayoutResult:
    """Outcome of a transfer attempt.

    ``ok`` reflects only that the provider ACCEPTED the request — for both rails a
    transfer is disbursed asynchronously, so ``ok`` does NOT mean the money
    landed. ``status`` carries the normalized disbursement state when known
    (``successful`` | ``pending`` | ``failed`` | ``unknown``); money has moved
    only on ``successful``.
    """

    ok: bool
    reference: str
    provider: str
    message: str | None = None
    status: str | None = None
    raw: dict | None = None


class PayoutProvider(abc.ABC):
    """Moves money from the platform balance to a seller's bank account."""

    name: str = "base"

    @abc.abstractmethod
    def transfer(
        self,
        db: Session,
        *,
        seller: models.User,
        amount_kobo: int,
        reference: str,
        reason: str,
    ) -> PayoutResult:
        """Pay ``amount_kobo`` (minor units) to the seller's payout account.

        Must raise :class:`PayoutError` on a network/transport failure (so the
        caller retries) and return a :class:`PayoutResult` with ``ok`` reflecting
        that the provider ACCEPTED the request (queued), not that it disbursed.
        Implementations should be idempotent on ``reference`` (providers reject
        duplicates).
        """

    @abc.abstractmethod
    def transfer_status(self, reference: str) -> str:
        """Normalized disbursement status for ``reference``.

        Returns ``successful`` | ``pending`` | ``failed`` | ``unknown``. Money has
        moved ONLY on ``successful``. On a transport error or an indeterminate
        response, return ``unknown`` (never guess ``failed``) so callers do not
        re-send an in-flight transfer.
        """

    def transfer_exists(self, reference: str) -> bool:
        """True only when the transfer actually DISBURSED (status successful)."""
        return self.transfer_status(reference) == "successful"
