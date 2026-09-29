"""
Shared invoice online-payment service.

Single source of truth for starting a Paystack payment for an invoice through
the issuer's subaccount. Reused by the public pay endpoint, the storefront
checkout, and the WhatsApp bot so there is no duplicated payment logic.
"""

from __future__ import annotations

import logging
import uuid

import httpx
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.payment_models import (
    PaymentProvider,
    PaymentStatus,
    PaymentTransaction,
)
from app.services.paystack_http import paystack_async_client

logger = logging.getLogger(__name__)


class PaymentInitError(Exception):
    """Raised when an invoice payment cannot be initialized."""

    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def start_invoice_payment(
    db: Session,
    invoice,
    issuer,
    hold: bool = False,
    charge_amount_kobo: int | None = None,
) -> dict:
    """
    Initialize a Paystack payment for ``invoice`` via ``issuer``'s subaccount.

    When ``hold`` is True (storefront escrow for an untrusted seller), the payment
    is collected to the SuoOps balance WITHOUT the subaccount split, so the
    seller's share is held until the buyer-protection window releases it (paid
    out later via a Transfer). Otherwise it splits to the seller's subaccount and
    settles normally.

    ``charge_amount_kobo`` lets the caller charge the buyer MORE than
    ``invoice.amount`` — storefront orders add the platform service fee (and any
    delivery) on top, while ``invoice.amount`` stays the seller's goods value so
    revenue/tax reporting is never inflated. Defaults to ``invoice.amount``.

    Returns ``{authorization_url, reference, amount}``. Raises PaymentInitError
    with a user-safe message + HTTP status on any failure.
    """
    if invoice.status in {"paid", "cancelled"}:
        raise PaymentInitError(f"Invoice is already {invoice.status}", 400)

    if not (getattr(issuer, "paystack_subaccount_active", False) and getattr(issuer, "paystack_subaccount_code", None)):
        raise PaymentInitError("This business has not enabled online payments yet.", 409)

    amount = invoice.amount
    if amount is None or amount <= 0:
        raise PaymentInitError("Invoice has no payable amount", 400)

    # The buyer's charge can exceed the invoice's merchandise value: a storefront
    # order adds the platform service fee (+ any delivery) on top, while
    # invoice.amount stays the seller's goods value so revenue/tax reporting is
    # never inflated. Ordinary payments charge exactly invoice.amount.
    charge_kobo = (
        int(charge_amount_kobo) if charge_amount_kobo is not None and int(charge_amount_kobo) > 0 else int(amount * 100)
    )

    if not settings.PAYSTACK_SECRET:
        raise PaymentInitError("Online payments are not configured", 503)

    # Platform commission via Paystack's flat transaction_charge (3%, min ₦20, tiered ₦2,000-per-₦500k cap):
    #  - Storefront orders never touch the wallet, so Paystack collects the 3%.
    #  - Business invoices already had the 3% debited from the wallet at creation,
    #    so Paystack takes nothing and the full amount settles to the business.
    from app.utils.feature_gate import platform_fee_kobo

    is_storefront = getattr(invoice, "channel", None) == "storefront"
    commission_kobo = min(platform_fee_kobo(amount), int(amount * 100)) if is_storefront else 0

    customer = getattr(invoice, "customer", None)
    customer_email = (
        (customer.email if customer and customer.email else None)
        or (f"{customer.phone}@suoops.com" if customer and customer.phone else None)
        or f"invoice-{invoice.invoice_id}@suoops.com"
    )

    reference = f"INVPAY-{invoice.invoice_id}-{uuid.uuid4().hex[:8].upper()}"

    # plan_before/plan_after are legacy NOT NULL columns from the old
    # subscription model. An invoice payment isn't a plan change, so record the
    # issuer's current plan for both.
    issuer_plan = getattr(getattr(issuer, "plan", None), "value", None) or "free"

    transaction = PaymentTransaction(
        user_id=issuer.id,
        reference=reference,
        amount=charge_kobo,  # kobo — the total the buyer is charged
        currency=getattr(invoice, "currency", "NGN") or "NGN",
        plan_before=issuer_plan,
        plan_after=issuer_plan,
        provider=PaymentProvider.PAYSTACK,
        status=PaymentStatus.PENDING,
        customer_email=customer_email,
        customer_phone=customer.phone if customer else None,
        payment_metadata={
            "payment_type": "invoice_payment",
            "invoice_id": invoice.invoice_id,
            "issuer_id": issuer.id,
        },
    )
    db.add(transaction)
    db.commit()

    # Escrow-hold collections are pluggable (Paystack default, Flutterwave
    # optional): collect the FULL amount to the platform/payout balance so it can
    # be held and released later. The collector is recorded on the transaction so
    # a refund follows the same rail.
    if hold:
        from app.services.collections import CollectionError, get_collection_provider

        collector = get_collection_provider()
        meta = dict(transaction.payment_metadata or {})
        meta["collector"] = collector.name
        transaction.payment_metadata = meta
        db.commit()
        try:
            charge = collector.initialize_hold_charge(
                amount_kobo=charge_kobo,
                reference=reference,
                customer_email=customer_email,
                customer_phone=customer.phone if customer else None,
                customer_name=getattr(customer, "name", None) if customer else None,
                callback_url=f"{settings.FRONTEND_URL}/pay/{invoice.invoice_id}?ref={reference}",
                narration=f"Storefront order — invoice {invoice.invoice_id}",
                metadata={
                    "payment_type": "invoice_payment",
                    "invoice_id": invoice.invoice_id,
                    "issuer_id": issuer.id,
                    "escrow_hold": True,
                    "collector": collector.name,
                },
            )
        except CollectionError as exc:
            transaction.status = PaymentStatus.FAILED
            db.commit()
            logger.error("Escrow collection init error (ref=%s): %s", reference, exc)
            raise PaymentInitError("Payment gateway error. Please try again.", 502) from exc
        return {
            "authorization_url": charge.authorization_url,
            "reference": reference,
            "amount": float(charge_kobo) / 100,
        }

    # Normal (non-hold) path: split to the seller's Paystack subaccount — they bear
    # the Paystack fee, SuoOps keeps the commission via transaction_charge.
    try:
        async with paystack_async_client(timeout=15.0) as client:
            init_payload = {
                "email": customer_email,
                "amount": charge_kobo,
                "reference": reference,
                "callback_url": f"{settings.FRONTEND_URL}/pay/{invoice.invoice_id}?ref={reference}",
                "metadata": {
                    "payment_type": "invoice_payment",
                    "invoice_id": invoice.invoice_id,
                    "issuer_id": issuer.id,
                    "escrow_hold": hold,
                },
                "subaccount": issuer.paystack_subaccount_code,
                "bearer": "subaccount",
                "transaction_charge": commission_kobo,
            }
            resp = await client.post(
                "https://api.paystack.co/transaction/initialize",
                headers={
                    "Authorization": f"Bearer {settings.PAYSTACK_SECRET}",
                    "Content-Type": "application/json",
                },
                json=init_payload,
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        transaction.status = PaymentStatus.FAILED
        db.commit()
        logger.error("Paystack invoice-pay init error (ref=%s): %s", reference, exc)
        raise PaymentInitError("Payment gateway error. Please try again.", 502) from exc

    if not data.get("status"):
        transaction.status = PaymentStatus.FAILED
        db.commit()
        raise PaymentInitError(data.get("message", "Payment initialization failed"), 502)

    return {
        "authorization_url": data["data"]["authorization_url"],
        "reference": reference,
        "amount": float(charge_kobo) / 100,
    }
