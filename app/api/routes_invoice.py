import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, TypeAlias

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.api.dependencies import get_data_owner_id
from app.api.rate_limit import limiter
from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import models, schemas
from app.services.invoice_service import InvoiceService, build_invoice_service
from app.storage.s3_client import S3Client
from app.utils.feature_gate import FeatureGate, check_invoice_limit

router = APIRouter()
logger = logging.getLogger(__name__)

CurrentUserDep: TypeAlias = Annotated[int, Depends(get_current_user_id)]
DataOwnerDep: TypeAlias = Annotated[int, Depends(get_data_owner_id)]
DbDep: TypeAlias = Annotated[Session, Depends(get_db)]


def get_invoice_service_for_user(data_owner_id: DataOwnerDep, db: DbDep) -> InvoiceService:
    """Get InvoiceService for the data owner (team admin for members, self for solo/admin)."""
    return build_invoice_service(db, user_id=data_owner_id)


@router.post("/", response_model=schemas.InvoiceOut)
@limiter.limit("30/minute")
async def create_invoice(
    data: schemas.InvoiceCreate,
    request: Request,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
    async_pdf: bool = True,  # Default to async PDF generation for better performance
):
    """Create a new invoice with optional async PDF generation.
    
    Args:
        data: Invoice creation data
        current_user_id: Authenticated user ID
        data_owner_id: The user ID whose data we're accessing (team admin for members)
        db: Database session
        async_pdf: If True, PDF is generated in background (faster API response).
              If False, PDF is generated immediately (slower but PDF URL available in response).
              Defaults to True for better user experience. When an invoice email is requested,
              the system automatically forces synchronous generation so the attachment is present.
    """
    if data.invoice_type == "expense":
        from app.services.expense_service import record_expense_invoice

        description = data.description
        if not description and data.lines:
            description = data.lines[0].description
        return record_expense_invoice(
            db,
            user_id=data_owner_id,
            amount=data.amount,
            category=data.category,
            description=description,
            merchant=data.merchant or data.vendor_name,
            expense_date=data.due_date,
            input_method="manual",
            channel="dashboard",
            notes=data.notes,
            created_by_user_id=current_user_id,
        )

    # Require bank details before creating revenue invoices
    user = db.query(models.User).filter(models.User.id == data_owner_id).one_or_none()
    if user and (not user.bank_name or not user.account_number):
        raise HTTPException(
            status_code=400,
            detail="Please add your bank details in Settings before creating invoices. Your customers need to know where to pay.",
        )

    # Check invoice creation limit based on data owner's subscription plan
    check_invoice_limit(db, data_owner_id)
    
    svc = get_invoice_service_for_user(data_owner_id, db)

    # Ensure PDF exists before sending notifications so it's available when customer replies
    effective_async = async_pdf
    if async_pdf and (data.customer_email or data.customer_phone):
        effective_async = False
        logger.info(
            "Forcing synchronous PDF generation for invoice notifications | user=%s data_owner=%s",
            current_user_id,
            data_owner_id,
        )
    try:
        invoice = svc.create_invoice(
            issuer_id=data_owner_id,
            data=data.model_dump(),
            async_pdf=effective_async,
            created_by_user_id=current_user_id,  # Track actual creator for confirmation permissions
        )
        
        # Send notifications via available channels (Email, WhatsApp) - ONLY for revenue invoices
        # Note: WhatsApp uses centralized opt-in logic - new customers get template, opted-in get full invoice
        logger.info(
            "[INVOICE CREATE] invoice_type=%s, customer_email=%s, customer_phone=%s, invoice_id=%s",
            invoice.invoice_type,
            data.customer_email,
            data.customer_phone,
            invoice.invoice_id,
        )
        if invoice.invoice_type == "revenue" and (data.customer_email or data.customer_phone):
            from app.services.notification_service import NotificationService
            notification_service = NotificationService()
            
            logger.info(
                "[INVOICE NOTIFY] Sending notification for %s to email=%s, phone=%s",
                invoice.invoice_id,
                data.customer_email,
                data.customer_phone,
            )

            results = await notification_service.send_invoice_notification(
                invoice=invoice,
                customer_email=data.customer_email,
                customer_phone=data.customer_phone,
                pdf_url=invoice.pdf_url,
            )
            
            # Commit any changes made during notification (e.g., whatsapp_delivery_pending flag)
            db.commit()

            if async_pdf and not invoice.pdf_url:
                logger.info(
                    "Invoice %s notifications sent without PDF attachment (async PDF generation in progress)",
                    invoice.invoice_id,
                )

            logger.info(
                "Invoice %s notifications - Email: %s, WhatsApp: %s",
                invoice.invoice_id,
                results["email"],
                results["whatsapp"],
            )
        else:
            logger.info(
                "[INVOICE SKIP NOTIFY] Skipping notification for %s - invoice_type=%s, has_email=%s, has_phone=%s",
                invoice.invoice_id,
                invoice.invoice_type,
                bool(data.customer_email),
                bool(data.customer_phone),
            )
        
        return invoice
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.post("/quick-sale", response_model=schemas.InvoiceOut)
@limiter.limit("30/minute")
async def create_quick_sale(
    data: schemas.QuickSaleCreate,
    request: Request,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Record a walk-in / in-person sale and mark it paid in one step.

    Unlike a normal invoice, no customer contact is required and no bank
    details are needed (the deliverable is a receipt, not a "please pay"
    document) — this is for cash/POS-style sales that are already settled at
    the point of sale. The sale still goes through the same paid-invoice
    pipeline as any other invoice (inventory deduction, receipt generation,
    fraud-review gate on large self-confirmed amounts), so it behaves
    identically to a business manually marking an invoice paid.
    """
    check_invoice_limit(db, data_owner_id)

    svc = get_invoice_service_for_user(data_owner_id, db)
    sale_data = {
        "amount": data.amount,
        "currency": data.currency,
        "invoice_type": "revenue",
        "channel": "quick_sale",
        "payment_method": data.payment_method,
        "customer_name": data.customer_name or "Walk-in Customer",
        "lines": [
            {
                "description": data.description or "Walk-in sale",
                "quantity": 1,
                "unit_price": data.amount,
            }
        ],
    }
    try:
        invoice = svc.create_invoice(
            issuer_id=data_owner_id,
            data=sale_data,
            async_pdf=True,
            created_by_user_id=current_user_id,
        )
        # A walk-in sale with no customer contact is created as
        # "awaiting_confirmation" (see InvoiceCreationMixin.create_invoice) —
        # flip it straight to "paid" since the money was already collected.
        invoice = svc.update_status(
            data_owner_id,
            invoice.invoice_id,
            "paid",
            updated_by_user_id=current_user_id,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return invoice


@router.post("/upload-receipt", response_model=schemas.ReceiptUploadOut)
@limiter.limit("10/minute")
async def upload_expense_receipt(
    request: Request,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    file: UploadFile = File(...),
):
    """Upload expense receipt image and return S3 URL for use in invoice creation.
    
    This endpoint allows users to upload proof of purchase (receipt photo/PDF)
    before creating an expense invoice. The returned receipt_url can then be
    included in the invoice creation request.
    """
    # Validate file type
    allowed_types = ["image/jpeg", "image/jpg", "image/png", "image/webp", "image/bmp", "application/pdf"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid file type. Allowed: JPEG, PNG, WebP, BMP, PDF. Got: {file.content_type}"
        )
    
    # Validate file size (max 10MB)
    max_size = 10 * 1024 * 1024  # 10MB
    content = await file.read()
    if len(content) > max_size:
        raise HTTPException(status_code=400, detail="File too large. Maximum size is 10MB.")
    
    if len(content) == 0:
        raise HTTPException(status_code=400, detail="File is empty.")
    
    # Validate magic bytes match claimed content type (prevents spoofed Content-Type)
    from app.utils.file_validation import validate_file_magic_bytes
    if not validate_file_magic_bytes(content, file.content_type):
        raise HTTPException(
            status_code=400,
            detail="File content does not match its declared type. Upload a valid image or PDF."
        )
    
    try:
        # Upload to S3
        s3_client = S3Client()
        
        # Determine file extension
        ext = "jpg"
        if file.content_type == "application/pdf":
            ext = "pdf"
        elif file.content_type == "image/png":
            ext = "png"
        elif file.content_type == "image/webp":
            ext = "webp"
        
        # Create unique filename (use data_owner_id for team context)
        filename = f"receipts/user_{data_owner_id}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.{ext}"
        
        receipt_url = await s3_client.upload_file(
            content,
            filename,
            content_type=file.content_type
        )
        
        logger.info("Uploaded expense receipt for data_owner %s by user %s: %s", data_owner_id, current_user_id, receipt_url)
        
        return schemas.ReceiptUploadOut(
            receipt_url=receipt_url,
            filename=file.filename or filename,
        )
    
    except Exception as e:
        logger.error("Failed to upload receipt: %s", e)
        raise HTTPException(status_code=500, detail="Failed to upload receipt. Please try again.")


@router.get("/quota", response_model=schemas.InvoiceQuotaOut)
def get_invoice_quota(current_user_id: CurrentUserDep, data_owner_id: DataOwnerDep, db: DbDep):
    """Return current invoice balance for the data owner.

    NEW MODEL: Returns invoice_balance (purchased invoices remaining) instead of monthly limits.
    For team members, this returns the team admin's quota.
    """
    from app.utils.feature_gate import INVOICE_PACK_PRICE, INVOICE_PACK_SIZE
    
    gate = FeatureGate(db, data_owner_id)
    plan = gate.user.effective_plan  # Uses effective_plan to respect pro_override
    invoice_balance = int(getattr(gate.user, "invoice_balance", 0) or 0)  # field on User
    can_create, _ = gate.can_create_invoice()
    purchase_url = "/invoices/purchase-pack" if not can_create else None
    
    return schemas.InvoiceQuotaOut(
        invoice_balance=invoice_balance,
        current_plan=plan.value,
        can_create=can_create,
        pack_price=INVOICE_PACK_PRICE,
        pack_size=INVOICE_PACK_SIZE,
        purchase_url=purchase_url,
    )


@router.get("/", response_model=schemas.PaginatedResponse[schemas.InvoiceOut])
def list_invoices(
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep, 
    db: DbDep,
    invoice_type: str | None = None,  # Optional filter: "revenue", "expense", or None for all
    start_date: str | None = None,  # Optional date filter (YYYY-MM-DD)
    end_date: str | None = None,  # Optional date filter (YYYY-MM-DD)
    status: str | None = None,  # Optional status filter (server-side, spans all pages)
    search: str | None = None,  # Optional free-text search (id / amount / customer)
    skip: int = 0,
    limit: int = 50,
):
    from datetime import date
    from datetime import datetime as dt
    
    # Clamp pagination params to safe bounds
    skip = max(0, skip)
    limit = max(1, min(limit, 200))
    
    # Parse date strings into date objects for SQL-level filtering
    parsed_start: date | None = None
    parsed_end: date | None = None
    if start_date:
        try:
            parsed_start = dt.strptime(start_date, "%Y-%m-%d").date()
        except ValueError:
            pass
    if end_date:
        try:
            parsed_end = dt.strptime(end_date, "%Y-%m-%d").date()
        except ValueError:
            pass
    
    svc = get_invoice_service_for_user(data_owner_id, db)
    invoices, total = svc.list_invoices(
        data_owner_id,
        invoice_type=invoice_type,
        skip=skip,
        limit=limit,
        start_date=parsed_start,
        end_date=parsed_end,
        status=status,
        search=search,
    )
    # Per-status counts (across all pages) so the filter chips stay accurate even
    # when the current view is filtered/paginated.
    status_counts = svc.count_invoices_by_status(
        data_owner_id,
        invoice_type=invoice_type,
        start_date=parsed_start,
        end_date=parsed_end,
        search=search,
    )

    return schemas.PaginatedResponse[schemas.InvoiceOut](
        items=invoices,
        total=total,
        skip=skip,
        limit=limit,
        has_more=(skip + limit) < total,
        status_counts=status_counts,
    )


@router.get("/{invoice_id}", response_model=schemas.InvoiceOutDetailed)
def get_invoice(invoice_id: str, current_user_id: CurrentUserDep, data_owner_id: DataOwnerDep, db: DbDep):
    svc = get_invoice_service_for_user(data_owner_id, db)
    try:
        return svc.get_invoice(data_owner_id, invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.patch("/{invoice_id}", response_model=schemas.InvoiceOutDetailed)
def update_invoice_status(
    invoice_id: str,
    payload: schemas.InvoiceStatusUpdate,
    current_user_id: CurrentUserDep,
    data_owner_id: DataOwnerDep,
    db: DbDep,
):
    """Update invoice status. Only the creator or admin (issuer) can update it."""
    from app.models.models import Invoice
    
    # Scope lookup to data_owner to prevent cross-tenant information leaks
    invoice = db.query(Invoice).filter(
        Invoice.invoice_id == invoice_id,
        Invoice.issuer_id == data_owner_id,
    ).first()
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    # Allow update if:
    # 1. User is the creator (created_by_user_id)
    # 2. User is the admin/issuer (issuer_id) - business owner always has access
    # For old invoices without created_by_user_id, issuer_id is the owner
    is_creator = invoice.created_by_user_id == current_user_id
    is_admin = invoice.issuer_id == current_user_id
    
    if not is_creator and not is_admin:
        raise HTTPException(
            status_code=403, 
            detail="Only the invoice creator or business admin can update the status"
        )
    
    svc = get_invoice_service_for_user(data_owner_id, db)
    try:
        return svc.update_status(data_owner_id, invoice_id, payload.status, updated_by_user_id=current_user_id)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Invoice not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.get("/{invoice_id}/pdf")
def download_invoice_pdf(invoice_id: str, current_user_id: CurrentUserDep, data_owner_id: DataOwnerDep, db: DbDep):
    """Download PDF for an invoice. Serves local files when S3 is not configured."""
    svc = get_invoice_service_for_user(data_owner_id, db)
    try:
        invoice = svc.get_invoice(data_owner_id, invoice_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    
    if not invoice.pdf_url:
        raise HTTPException(status_code=404, detail="PDF not generated for this invoice")
    
    # If it's a file:// URL, serve from local filesystem
    if invoice.pdf_url.startswith("file://"):
        file_path = invoice.pdf_url.replace("file://", "")
        path = Path(file_path).resolve()
        
        # Defense-in-depth: restrict to the storage directory
        storage_root = Path("storage").resolve()
        if not str(path).startswith(str(storage_root)):
            raise HTTPException(status_code=403, detail="Access denied")
        
        if not path.exists():
            raise HTTPException(status_code=404, detail="PDF file not found on disk")
        
        return FileResponse(
            path=str(path),
            media_type="application/pdf",
            filename=f"{invoice_id}.pdf",
        )
    
    # If it's an HTTP URL (S3), re-sign a FRESH presigned URL before redirecting —
    # the stored one is short-lived and would 'Request has expired' for older invoices.
    from fastapi.responses import RedirectResponse

    from app.storage.s3_client import s3_client
    fresh_url = s3_client.refresh_presigned_url(invoice.pdf_url) or invoice.pdf_url
    return RedirectResponse(url=fresh_url)


@router.get("/{invoice_id}/verify", response_model=schemas.InvoiceVerificationOut)
def verify_invoice(invoice_id: str, db: DbDep):
    """Public endpoint to verify invoice authenticity via QR code scan.
    
    This endpoint does NOT require authentication - it's meant to be scanned
    by customers to verify the invoice is legitimate.
    
    Returns masked customer information for privacy while proving authenticity.
    """
    from datetime import datetime

    from app.models.models import Invoice
    
    invoice = db.query(Invoice).filter(Invoice.invoice_id == invoice_id).first()
    
    if not invoice:
        raise HTTPException(status_code=404, detail="Invoice not found")
    
    # Mask customer name for privacy (show first letter + asterisks)
    customer_name = invoice.customer.name
    if len(customer_name) > 2:
        masked_name = customer_name[0] + "*" * (len(customer_name) - 2) + customer_name[-1]
    else:
        masked_name = customer_name[0] + "*"
    
    # Resolve issuer (business) name via relationship (added FK issuer_id -> user.id)
    if getattr(invoice, "issuer", None):
        business_name = invoice.issuer.business_name or invoice.issuer.name
    else:
        business_name = "Business"

    # Unique, non-guessable authenticity stamp derived from the invoice + issuer
    # with the server secret — anyone can read it off the QR/receipt, but only
    # SuoOps can produce it, so a forged receipt can't fake a matching code.
    import base64
    import hashlib
    import hmac

    from app.core.config import settings

    _raw = f"{invoice.invoice_id}:{invoice.issuer_id}:{invoice.created_at.isoformat()}"
    _digest = hmac.new(settings.JWT_SECRET.encode(), _raw.encode(), hashlib.sha256).digest()
    _b32 = base64.b32encode(_digest).decode().rstrip("=")
    verification_code = f"SUP-{_b32[:4]}-{_b32[4:8]}"

    # What was bought (description + quantity), so a scan shows the actual order.
    items = [
        schemas.InvoiceVerificationItem(
            description=(line.description or "Item"),
            quantity=int(line.quantity or 1),
        )
        for line in (invoice.lines or [])
    ]

    # Fulfilment (storefront orders): show whether the seller has rendered the
    # service / delivered the goods, and where it sits in buyer protection — so
    # a scan proves more than "paid".
    from app.models.models import StorefrontOrderEscrow

    escrow = (
        db.query(StorefrontOrderEscrow)
        .filter(StorefrontOrderEscrow.invoice_id == invoice.id)
        .first()
    )
    fulfilment_status, fulfilment_label = _fulfilment(escrow)

    return schemas.InvoiceVerificationOut(
        invoice_id=invoice_id,
        status=invoice.status,
        amount=invoice.amount,
        customer_name=masked_name,
        business_name=business_name,
        verification_code=verification_code,
        items=items,
        fulfilment_status=fulfilment_status,
        fulfilment_label=fulfilment_label,
        created_at=invoice.created_at,
        verified_at=datetime.now(timezone.utc),
        authentic=True,
    )


def _fulfilment(escrow) -> tuple[str | None, str | None]:
    """Human-readable fulfilment state for a storefront order's escrow.

    Returns (status, label) or (None, None) for a non-storefront invoice (which
    has no delivery/service lifecycle — it's just paid or not).
    """
    if escrow is None:
        return None, None
    st = (escrow.status or "").lower()
    is_service = not bool(getattr(escrow, "requires_delivery", True))

    if st == "refunded":
        return "refunded", "Refunded to the buyer"
    if st == "disputed":
        return "disputed", "A problem was reported — under review"
    if st in ("canceled", "cancelled"):
        return "canceled", "Order canceled"
    if st == "pending":
        return "unpaid", "Awaiting payment"
    if st == "released":
        return "released", (
            "Service rendered — payment released to the seller"
            if is_service
            else "Delivered — payment released to the seller"
        )

    # 'held' — paid, within the buyer-protection window.
    if getattr(escrow, "confirmed_at", None):
        return "confirmed", (
            "Buyer confirmed the service was rendered"
            if is_service
            else "Buyer confirmed delivery"
        )
    if is_service:
        if getattr(escrow, "seller_marked_delivered_at", None):
            return "rendered", "Seller marked the service as rendered"
        return "in_progress", "Paid — service in progress"
    if getattr(escrow, "seller_marked_delivered_at", None) or getattr(
        escrow, "courier_delivered_at", None
    ):
        return "delivered", "Seller marked the order delivered"
    if getattr(escrow, "seller_dispatched_at", None):
        return "sent", "Sent out — on the way to the buyer"
    return "preparing", "Paid — the seller is preparing your order"


@router.post("/purchase-pack", response_model=schemas.InvoicePackPurchaseInitOut)
@limiter.limit("10/minute")
async def initialize_invoice_pack_purchase(
    request: Request,
    current_user_id: CurrentUserDep,
    db: DbDep,
    amount: int = 1250,
):
    """
    Initialize a Paystack payment to top up the prepaid wallet.

    **Parameters:**
    - amount: Top-up amount in Naira; must be an offered tier (1250/5000/20000).
      The customer additionally covers the Paystack fee at checkout; the wallet
      is credited the full tier amount.

    **Returns:**
    - authorization_url: Paystack checkout URL
    - reference: Payment reference for tracking
    - amount: Total charged in Naira (tier + Paystack fee)
    - wallet_credit_naira: Amount credited to the wallet (the tier)
    """
    import uuid

    import httpx

    from app.core.config import settings
    from app.models.payment_models import PaymentProvider, PaymentStatus, PaymentTransaction
    from app.services.payment_providers import calculate_amount_with_paystack_fee
    from app.services.paystack_http import paystack_async_client
    from app.utils.feature_gate import WALLET_TOPUP_TIERS

    if amount not in WALLET_TOPUP_TIERS:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid amount. Choose a top-up tier: {WALLET_TOPUP_TIERS}",
        )

    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Customer covers the Paystack fee; the wallet is credited the full tier.
    wallet_credit_kobo = amount * 100
    total_amount = int(calculate_amount_with_paystack_fee(amount))

    # Generate unique reference (INVPACK- so the webhook credits the wallet)
    reference = f"INVPACK-{current_user_id}-{uuid.uuid4().hex[:8].upper()}"

    # Record transaction (wallet top-up - plan stays the same)
    current_plan = user.plan.value if user.plan else "free"
    transaction = PaymentTransaction(
        user_id=current_user_id,
        reference=reference,
        amount=total_amount * 100,  # Store in kobo like other transactions
        currency="NGN",
        provider=PaymentProvider.PAYSTACK,
        status=PaymentStatus.PENDING,
        plan_before=current_plan,
        plan_after=current_plan,  # Wallet top-up doesn't change plan
        customer_email=user.email or (f"{user.phone}@suoops.com" if user.phone else None),
        customer_phone=user.phone,
        payment_metadata={
            "payment_type": "invoice_pack",
            "wallet_credit_kobo": wallet_credit_kobo,
        },
    )
    db.add(transaction)
    db.commit()
    
    # Initialize Paystack payment
    try:
        async with paystack_async_client() as client:
            resp = await client.post(
                "https://api.paystack.co/transaction/initialize",
                headers={
                    "Authorization": f"Bearer {settings.PAYSTACK_SECRET}",
                    "Content-Type": "application/json",
                },
                json={
                    "email": user.email or (f"{user.phone}@suoops.com" if user.phone else f"user{current_user_id}@suoops.com"),
                    "amount": int(total_amount * 100),  # Paystack expects kobo (includes fees)
                    "reference": reference,
                    "callback_url": f"{settings.FRONTEND_URL}/dashboard/billing/success?reference={reference}",
                    "metadata": {
                        "payment_type": "invoice_pack",
                        "user_id": current_user_id,
                        "wallet_credit_kobo": wallet_credit_kobo,
                    },
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as e:
        logger.error("Paystack API error: %s", e)
        transaction.status = PaymentStatus.FAILED
        db.commit()
        raise HTTPException(status_code=502, detail="Payment gateway error. Please try again.")
    
    if not data.get("status"):
        transaction.status = PaymentStatus.FAILED
        db.commit()
        raise HTTPException(status_code=502, detail=data.get("message", "Payment initialization failed"))
    
    auth_url = data["data"]["authorization_url"]
    
    logger.info(
        "Wallet top-up payment initialized | user=%s credit_naira=%d charged_naira=%d ref=%s",
        current_user_id, amount, total_amount, reference
    )
    
    return schemas.InvoicePackPurchaseInitOut(
        authorization_url=auth_url,
        reference=reference,
        amount=total_amount,
        invoices_to_add=0,
        wallet_credit_naira=amount,
    )
