from __future__ import annotations

import logging
from decimal import Decimal

from app.core.config import settings
from app.services.payment_providers import PaymentRouter

logger = logging.getLogger(__name__)


class PaymentService:
    """Facade over payment providers via PaymentRouter.

    Important: Uses business's own Paystack credentials, not platform credentials.
    Money flows directly from customer to business's bank account.
    """

    def __init__(self, paystack_secret_key: str | None = None):
        """
        Initialize payment service.

        Args:
            paystack_secret_key: Business's own Paystack secret key.
                                If None, uses platform default (for testing/fallback only).
        """
        secret = paystack_secret_key or settings.PAYSTACK_SECRET
        if not secret:
            raise ValueError("Paystack secret key is required")
        self.router = PaymentRouter(secret)

    async def create_payment_link(self, reference: str, amount: Decimal) -> str:
        return await self.router.create_payment_link(reference, amount)

    def verify_webhook(
        self,
        raw_body: bytes,
        signature: str | None,
    ) -> bool:
        return self.router.verify_webhook(raw_body, signature)
