from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.rate_limit import limiter
from app.db.session import get_db
from app.models import schemas
from app.services.invoice_service import build_invoice_service
from app.services.paystack_http import paystack_async_client
from app.utils.invoice_delivery import invoice_has_contact, is_online_only

router = APIRouter(tags=["invoices-public"])


def _public_invoice_payload(invoice, issuer) -> dict[str, object]:
    customer_name = getattr(invoice.customer, "name", None) if invoice.customer else None
    lines_data = [
        {"description": ln.description, "quantity": ln.quantity, "unit_price": ln.unit_price}
        for ln in (invoice.lines or [])
    ]

    # Stored presigned URLs expire, so re-presign from the stable object key.
    from app.storage.s3_client import s3_client

    def _fresh_url(stored: str | None) -> str | None:
        if not stored:
            return None
        key = s3_client.extract_key_from_url(stored)
        if key:
            return s3_client.get_presigned_url(key, expires_in=3600) or stored
        return stored

    logo_url = _fresh_url(getattr(issuer, "logo_url", None))

    # Paid invoices expose their receipt (and invoice PDF, when one exists) so
    # the customer can download it right on the pay page. Nothing is exposed
    # before payment — online-only invoices have no invoice PDF at all.
    is_paid = invoice.status == "paid"
    receipt_pdf_url = _fresh_url(getattr(invoice, "receipt_pdf_url", None)) if is_paid else None
    pdf_url = _fresh_url(getattr(invoice, "pdf_url", None)) if is_paid else None

    only_online = is_online_only(
        issuer,
        has_contact=invoice_has_contact(invoice),
        channel=getattr(invoice, "channel", None),
    )
    # Pay-Now (online) is only offered for online-only invoices (storefront
    # orders). Business-created invoices are paid by bank transfer.
    online_enabled = only_online
    return {
        "invoice_id": invoice.invoice_id,
        "amount": invoice.amount,
        "currency": getattr(invoice, "currency", "NGN") or "NGN",
        "status": invoice.status,
        "due_date": invoice.due_date,
        "created_at": invoice.created_at,
        "paid_at": invoice.paid_at,
        "customer_name": customer_name,
        "business_name": getattr(issuer, "business_name", None),
        "business_logo_url": logo_url,
        # Online-only invoices never expose bank details so they cannot be paid
        # offline (which would bypass the platform commission).
        "bank_name": None if only_online else getattr(issuer, "bank_name", None),
        "account_number": None if only_online else getattr(issuer, "account_number", None),
        "account_name": None if only_online else getattr(issuer, "account_name", None),
        "online_payments_enabled": online_enabled,
        "online_only": only_online,
        "pdf_url": pdf_url,
        "receipt_pdf_url": receipt_pdf_url,
        "lines": lines_data,
    }


@router.get("/{invoice_id}", response_model=schemas.InvoicePublicOut)
@limiter.limit("15/minute")
def get_invoice_public(request: Request, invoice_id: str, db: Session = Depends(get_db)) -> schemas.InvoicePublicOut:
    svc = build_invoice_service(db)
    try:
        invoice, issuer = svc.get_public_invoice(invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return schemas.InvoicePublicOut.model_validate(_public_invoice_payload(invoice, issuer))


@router.post("/{invoice_id}/confirm-transfer", response_model=schemas.InvoicePublicOut)
@limiter.limit("3/minute")
def confirm_transfer(request: Request, invoice_id: str, db: Session = Depends(get_db)) -> schemas.InvoicePublicOut:
    svc = build_invoice_service(db)
    try:
        invoice, issuer = svc.get_public_invoice(invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if is_online_only(
        issuer,
        has_contact=invoice_has_contact(invoice),
        channel=getattr(invoice, "channel", None),
    ):
        raise HTTPException(status_code=400, detail="This invoice must be paid online.")

    try:
        svc.confirm_transfer(invoice_id)
        invoice, issuer = svc.get_public_invoice(invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    return schemas.InvoicePublicOut.model_validate(_public_invoice_payload(invoice, issuer))


@router.post("/{invoice_id}/pay")
@limiter.limit("5/minute")
async def initialize_invoice_payment(request: Request, invoice_id: str, db: Session = Depends(get_db)) -> dict:
    """
    Public: start an online payment for an invoice via the issuer's Paystack
    subaccount. Reuses the shared invoice_payment_service.
    """
    from app.services.invoice_payment_service import (
        PaymentInitError,
        start_invoice_payment,
    )

    svc = build_invoice_service(db)
    try:
        invoice, issuer = svc.get_public_invoice(invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    # Online payment is only available for storefront/online-only invoices.
    # Business-created invoices are settled by bank transfer.
    if not is_online_only(
        issuer,
        has_contact=invoice_has_contact(invoice),
        channel=getattr(invoice, "channel", None),
    ):
        raise HTTPException(status_code=400, detail="This invoice is paid by bank transfer.")

    # If this storefront order was set up as a buyer-protection HOLD, the retry
    # (e.g. after the customer cancelled at checkout) MUST use the same held
    # collection rail — not the default Paystack subaccount split. Otherwise the
    # money would bypass escrow and settle straight to the seller. A pending
    # escrow row means "this order is held".
    from app.models import models as _models

    held_escrow = (
        db.query(_models.StorefrontOrderEscrow)
        .filter(
            _models.StorefrontOrderEscrow.invoice_id == invoice.id,
            _models.StorefrontOrderEscrow.status == "pending",
        )
        .first()
    )
    held = held_escrow is not None
    # Re-derive the buyer's full charge (goods + service fee + delivery) from the
    # held escrow so a retry charges exactly what the first attempt did. The
    # invoice itself only records the seller's goods value.
    charge_kobo: int | None = None
    if held_escrow is not None:
        charge_kobo = (
            int(held_escrow.gross_kobo or 0)
            + int(getattr(held_escrow, "fee_kobo", 0) or 0)
            + int(getattr(held_escrow, "delivery_fee_kobo", 0) or 0)
        )

    try:
        return await start_invoice_payment(db, invoice, issuer, hold=held, charge_amount_kobo=charge_kobo)
    except PaymentInitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc


@router.post("/{invoice_id}/verify")
@limiter.limit("15/minute")
async def verify_invoice_payment(
    request: Request,
    invoice_id: str,
    reference: str,
    db: Session = Depends(get_db),
) -> dict:
    """
    Public: verify a Paystack payment on return from checkout and confirm the
    invoice — a pull-based fallback so a delayed/missed webhook doesn't leave the
    customer stuck on "pending". Only marks paid when Paystack itself reports
    success for a reference that belongs to this invoice.
    """
    import logging

    from app.core.config import settings
    from app.models.payment_models import PaymentStatus, PaymentTransaction

    logger = logging.getLogger(__name__)

    svc = build_invoice_service(db)
    try:
        invoice, issuer = svc.get_public_invoice(invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    if invoice.status == "paid":
        return {"status": "paid"}

    # The reference must be the one we created for THIS invoice's payment.
    txn = db.query(PaymentTransaction).filter(PaymentTransaction.reference == reference).one_or_none()
    meta_invoice = (txn.payment_metadata or {}).get("invoice_id") if txn else None
    if not reference.startswith("INVPAY-") or (txn and meta_invoice != invoice.invoice_id):
        return {"status": invoice.status}

    if not settings.PAYSTACK_SECRET:
        return {"status": invoice.status}

    # Ask Paystack directly whether this transaction succeeded.
    try:
        async with paystack_async_client(timeout=15.0) as client:
            resp = await client.get(
                f"https://api.paystack.co/transaction/verify/{reference}",
                headers={"Authorization": f"Bearer {settings.PAYSTACK_SECRET}"},
            )
        body = resp.json() if resp.content else {}
    except Exception as exc:  # pragma: no cover - network failure
        logger.warning("Paystack verify failed for %s: %s", reference, exc)
        return {"status": invoice.status}

    tx = body.get("data") or {}
    if resp.status_code == 200 and tx.get("status") == "success":
        try:
            svc.update_status(issuer.id, invoice.invoice_id, "paid", via_online=True)
        except Exception:  # pragma: no cover
            logger.exception("verify: failed to mark invoice %s paid", invoice.invoice_id)
        if txn:
            txn.status = PaymentStatus.SUCCESS
            db.commit()
        logger.info("✅ Invoice %s confirmed paid via verify-on-return (ref=%s)", invoice.invoice_id, reference)
        return {"status": "paid"}

    return {"status": invoice.status}
