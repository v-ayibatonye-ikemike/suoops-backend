"""Pluggable payment COLLECTORS for storefront/escrow orders.

Only the escrow-hold collection (full amount collected to the platform balance so
it can be held and released later) varies by provider — regular invoice payments
always settle through the issuer's Paystack subaccount. Select the active
collector with ``settings.ESCROW_COLLECTOR_PROVIDER``. Refunds follow the
provider that collected each order (recorded in the payment metadata).
"""

from __future__ import annotations

import abc
import dataclasses


class CollectionError(Exception):
    """A collection (charge init / verify / refund) could not be completed."""


@dataclasses.dataclass
class ChargeInit:
    """Result of initializing a hosted charge."""

    authorization_url: str
    reference: str
    provider: str
    raw: dict | None = None


@dataclasses.dataclass
class ChargeStatus:
    """Normalized outcome of a charge, from a verify call.

    ``status`` is ``successful`` | ``pending`` | ``failed`` | ``unknown``. Value
    should be given to the customer only on ``successful`` AND after confirming
    ``amount_kobo`` / ``currency`` match what was expected.
    """

    status: str
    amount_kobo: int | None = None
    currency: str | None = None
    provider_tx_id: str | None = None
    raw: dict | None = None


class CollectionProvider(abc.ABC):
    """Collects a held (escrow) payment and can verify + refund it."""

    name: str = "base"

    @abc.abstractmethod
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
        """Start a hosted charge for the FULL amount, collected to the platform
        balance (no subaccount split). Raises :class:`CollectionError` on failure.
        """

    @abc.abstractmethod
    def verify_charge(self, reference: str) -> ChargeStatus:
        """Re-query the provider for the authoritative status of ``reference``."""

    @abc.abstractmethod
    def refund(self, *, reference: str, amount_kobo: int, note: str) -> dict:
        """Refund the collected charge identified by our ``reference``. Raises
        :class:`CollectionError` on failure."""
