"""
OCR Invoice Routes - Photo-to-invoice creation via image upload.

Allows users to:
1. Upload receipt/invoice image
2. Extract data via OCR
3. Review and confirm before creating invoice
4. Or create invoice directly from image

Design:
- Two-step flow: parse → review → create (safer)
- One-step flow: parse and create immediately (faster)
- Supports multiple image formats (JPEG, PNG, WebP, etc.)
"""

import logging

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from sqlalchemy.orm import Session

from app.api.rate_limit import RATE_LIMITS, limiter
from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models import schemas
from app.services.invoice_service import build_invoice_service
from app.services.ocr_service import OCRService
from app.utils.feature_gate import check_invoice_limit, check_voice_ocr_quota

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/ocr", tags=["ocr"])


@router.post("/parse", response_model=schemas.OCRParseOut)
@limiter.limit(RATE_LIMITS["ocr_parse"])  # Rate limit: 10 images per minute
async def parse_receipt_image(
    request: Request,
    file: UploadFile = File(...),
    context: str = None,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Parse receipt/invoice image to extract data (Step 1).

    Included for every merchant, with a monthly usage limit for cost control.

    **Upload image → Get structured data → Review → Confirm**

    Supports:
    - JPEG, PNG, WebP, BMP, GIF
    - Max size: 10MB
    - Nigerian receipts optimized

    Args:
        file: Image file upload
        context: Optional business context (e.g., "hair salon invoice")

    Returns:
        Parsed invoice data with confidence score

    Example:
        ```python
        # Upload image
        files = {"file": open("receipt.jpg", "rb")}
        response = requests.post("/ocr/parse", files=files)

    # Review data (example – avoid print in production; use logger.debug)
    data = response.json()
    # logger.debug("OCR parse preview amount=%s customer=%s", data.get('amount'), data.get('customer_name'))

        # If good, create invoice with returned data
        ```

    Speed: ~5-10 seconds
    """
    # Preserve the shared access hook; it is a no-op under commission billing.
    check_voice_ocr_quota(db, current_user_id)

    # Check monthly OCR limit (cost control — 10 scans/month per user)
    from app.services.ocr_service import check_ocr_quota

    if not check_ocr_quota(current_user_id):
        raise HTTPException(status_code=429, detail="Monthly OCR limit reached (10 scans/month). Resets next month.")

    # Validate file type (fast check before reading bytes)
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/bmp", "image/gif"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, detail=f"Invalid file type: {file.content_type}. Allowed: JPEG, PNG, WebP, BMP, GIF"
        )

    # Validate file size (max 10MB)
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum size: 10MB")

    # Validate not empty
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # Validate magic bytes match claimed content type (prevents spoofed Content-Type)
    from app.utils.file_validation import validate_file_magic_bytes

    if not validate_file_magic_bytes(contents, file.content_type):
        raise HTTPException(
            status_code=400, detail="File content does not match its declared type. Upload a valid image."
        )

    logger.info(
        f"OCR parse request: user={current_user_id}, "
        f"filename={file.filename}, size={len(contents)} bytes, "
        f"context={context}"
    )

    # Parse image with OCR
    ocr = OCRService()
    result = await ocr.parse_receipt(contents, context)

    if not result.get("success"):
        logger.warning(f"OCR parsing failed: {result.get('error')}")
        raise HTTPException(
            status_code=422, detail=f"Could not extract data from image: {result.get('error', 'Unknown error')}"
        )

    # Record successful OCR usage against monthly quota
    from app.services.ocr_service import record_ocr_use

    record_ocr_use(current_user_id)

    logger.info(
        f"OCR success: amount={result['amount']}, "
        f"items={len(result['items'])}, "
        f"confidence={result['confidence']}"
    )

    # Return parsed data for review
    return schemas.OCRParseOut(**result)


@router.post("/create-invoice", response_model=schemas.InvoiceOut)
@limiter.limit(RATE_LIMITS["ocr_create_invoice"])
async def create_invoice_from_image(
    request: Request,
    file: UploadFile = File(...),
    customer_phone: str = None,
    context: str = None,
    current_user_id: int = Depends(get_current_user_id),
    db: Session = Depends(get_db),
):
    """
    Parse image AND create invoice in one step (convenience endpoint).

    Included for every merchant, with the same monthly OCR usage limit.

    **Quick flow: Upload image → Invoice created automatically**

    Use this when you trust OCR accuracy and want speed.
    Use /parse endpoint if you want to review data first.

    Args:
        file: Image file upload
        customer_phone: Optional phone number for customer
        context: Optional business context

    Returns:
        Created invoice with PDF link

    Example:
        ```python
        files = {"file": open("receipt.jpg", "rb")}
        data = {"customer_phone": "+2348012345678"}
        response = requests.post("/ocr/create-invoice", files=files, data=data)

    invoice = response.json()
    # logger.debug("OCR created invoice_id=%s pdf=%s", invoice.get('invoice_id'), invoice.get('pdf_url'))
        ```

    Note: Review the created invoice - OCR may have errors!
    """
    # Check if user has Business plan with available quota
    check_voice_ocr_quota(db, current_user_id)

    # Check monthly OCR limit (cost control)
    from app.services.ocr_service import check_ocr_quota

    if not check_ocr_quota(current_user_id):
        raise HTTPException(status_code=429, detail="Monthly OCR limit reached (10 scans/month). Resets next month.")

    # Check invoice creation limit
    check_invoice_limit(db, current_user_id)

    # Validate file type before reading (avoid wasting OpenAI call on bad input)
    allowed_types = ["image/jpeg", "image/png", "image/webp", "image/bmp", "image/gif"]
    if file.content_type not in allowed_types:
        raise HTTPException(
            status_code=400, detail=f"Invalid file type: {file.content_type}. Allowed: JPEG, PNG, WebP, BMP, GIF"
        )

    # Read and validate size/content
    contents = await file.read()
    if len(contents) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="File too large. Maximum size: 10MB")
    if len(contents) == 0:
        raise HTTPException(status_code=400, detail="Empty file uploaded")

    # First parse the image
    ocr = OCRService()
    result = await ocr.parse_receipt(contents, context)

    if not result.get("success"):
        raise HTTPException(status_code=422, detail=f"Could not extract data from image: {result.get('error')}")

    # Create invoice from parsed data
    invoice_service = build_invoice_service(db, user_id=current_user_id)

    try:
        # Use extracted customer name or default
        customer_name = result.get("customer_name", "Unknown Customer")
        if customer_name == "Unknown":
            customer_name = "Customer (from receipt)"

        # Build invoice lines from OCR items
        lines = []
        for item in result.get("items", []):
            lines.append(
                {
                    "description": item["description"],
                    "quantity": item["quantity"],
                    "unit_price": float(item["unit_price"]),
                }
            )

        # Create invoice
        invoice_data = schemas.InvoiceCreate(
            customer_name=customer_name,
            customer_phone=customer_phone,
            amount=float(result["amount"]),
            lines=lines,
            notes=f"Created from receipt image (OCR). Confidence: {result.get('confidence', 'medium')}",
        )

        invoice = invoice_service.create_invoice(
            issuer_id=current_user_id,
            data=invoice_data.model_dump(),
        )

        logger.info("Invoice created from OCR: invoice_id=%s", invoice.invoice_id)

        return invoice

    except Exception as e:
        logger.error("Failed to create invoice from OCR data: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to create invoice from image. Please try again.")
