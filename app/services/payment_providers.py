from __future__ import annotations

import hashlib
import hmac
import logging
from decimal import ROUND_UP, Decimal

from app.core.config import settings
from app.services.paystack_http import paystack_async_client

logger = logging.getLogger(__name__)


def calculate_amount_with_paystack_fee(target_amount: Decimal) -> Decimal:
    """
    Calculate the gross amount to charge so you receive exactly target_amount after Paystack fees.

    Paystack fee structure (Nigeria):
    - Local cards: 1.5% + ₦100 (capped at ₦2,000)
    - Bank transfer: ₦50 flat (but we calculate for worst case - card)

    Formula: gross = (target + 100) / (1 - 0.015)
    This ensures after Paystack deducts 1.5% + ₦100, you receive target_amount.

    Examples:
    - Pro Pack ₦2,000 → Customer pays ~₦2,150 (you receive ₦2,000)
    - Invoice Pack ₦1,250 → Customer pays ~₦1,370 (you receive ₦1,250)
    """
    target = Decimal(target_amount)
    fee_percentage = Decimal("0.015")  # 1.5%
    flat_fee = Decimal("100")  # ₦100
    fee_cap = Decimal("2000")  # ₦2,000 max fee

    # Calculate gross amount needed
    # gross - (gross * 0.015 + 100) = target
    # gross * (1 - 0.015) = target + 100
    # gross = (target + 100) / 0.985
    gross = (target + flat_fee) / (Decimal("1") - fee_percentage)

    # Check if fee would exceed cap
    calculated_fee = gross * fee_percentage + flat_fee
    if calculated_fee > fee_cap:
        # Fee is capped, so just add ₦2,000 to target
        gross = target + fee_cap

    # Round up to nearest Naira
    return gross.quantize(Decimal("1"), rounding=ROUND_UP)


class PaystackProvider:
    name = "paystack"

    def __init__(self, secret: str):
        self.secret = secret
        self.base = "https://api.paystack.co"

    async def create_payment_link(
        self,
        reference: str,
        amount: Decimal,
        email: str | None = None,
        pass_fees_to_customer: bool = True,
    ) -> str:
        """
        Create a Paystack payment link.

        Args:
            reference: Unique transaction reference
            amount: Target amount you want to receive (in Naira)
            email: Customer email
            pass_fees_to_customer: If True, adds Paystack fees to amount so you receive exact target
        """
        # Calculate amount with fees if passing to customer
        if pass_fees_to_customer:
            charge_amount = calculate_amount_with_paystack_fee(amount)
            logger.info("Passing fees to customer: target=%s, charging=%s", amount, charge_amount)
        else:
            charge_amount = Decimal(amount)

        payload = {
            "reference": reference,
            "amount": int(charge_amount * 100),  # Paystack expects kobo
            "email": email or "placeholder@example.com",
            "callback_url": f"{settings.FRONTEND_URL}/payments/confirm",
        }
        try:
            async with paystack_async_client(timeout=10.0) as client:
                r = await client.post(
                    f"{self.base}/transaction/initialize",
                    headers={"Authorization": f"Bearer {self.secret}"},
                    json=payload,
                )
                r.raise_for_status()
                data = r.json()
            url = data.get("data", {}).get("authorization_url")
            if not url:
                raise ValueError("No authorization_url in Paystack response")
            return url
        except Exception as e:  # noqa: BLE001
            logger.error("Paystack payment initialization failed: %s", e)
            raise RuntimeError(
                f"Payment provider error: unable to create payment link (ref: {reference}). "
                "Please try again or contact support."
            ) from e

    def verify_webhook(self, raw_body: bytes, signature: str | None) -> bool:
        if not signature:
            return False
        digest = hmac.new(self.secret.encode(), raw_body, hashlib.sha512).hexdigest()
        return hmac.compare_digest(digest, signature)


class PaymentRouter:
    """Single-provider router for Paystack integrations."""

    def __init__(self, paystack_secret_key: str):
        if not paystack_secret_key:
            raise ValueError("Paystack secret key is required")
        self.provider = PaystackProvider(paystack_secret_key)

    async def create_payment_link(
        self,
        reference: str,
        amount: Decimal,
        email: str | None = None,
        pass_fees_to_customer: bool = True,
    ) -> str:
        return await self.provider.create_payment_link(reference, amount, email, pass_fees_to_customer)

    def verify_webhook(self, raw_body: bytes, signature: str | None) -> bool:
        return self.provider.verify_webhook(raw_body, signature)
