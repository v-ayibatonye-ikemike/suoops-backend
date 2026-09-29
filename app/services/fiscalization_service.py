"""
Invoice Fiscalization Service (pre-integration placeholder).

Handles:
- Fiscal code generation
- Digital signatures
- QR code creation
- VAT calculations
- Future external transmission (FIRS or approved gateway)

Single Responsibility: Invoice fiscalization only
"""

import base64
import hashlib
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from io import BytesIO
from typing import Dict, Optional

import httpx
import qrcode
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.models import Invoice
from app.models.tax_models import FiscalInvoice, VATCategory
from app.services.nrs_client import get_nrs_client

logger = logging.getLogger(__name__)


class VATCalculator:
    """
    VAT calculation logic (SRP: Calculations only).

    Nigeria 2026 tax rules:
    - Standard rate: 7.5% (from settings.VAT_RATE)
    - Zero-rated: Medical, education, basic food
    - Exempt: Financial services
    """

    @staticmethod
    def _standard_rate() -> float:
        return settings.VAT_RATE

    @classmethod
    def _vat_rates(cls) -> dict:
        rate = cls._standard_rate()
        return {
            VATCategory.STANDARD: rate,
            VATCategory.ZERO_RATED: 0,
            VATCategory.EXEMPT: 0,
            VATCategory.EXPORT: 0,
        }

    @classmethod
    def calculate(cls, amount: Decimal, category: str = "standard") -> Dict[str, Decimal]:
        """
        Calculate VAT breakdown for an amount.

        Args:
            amount: Total amount (VAT inclusive)
            category: VAT category (standard/zero_rated/exempt/export)

        Returns:
            Dict with subtotal, vat_rate, vat_amount, total
        """
        rates = cls._vat_rates()
        vat_rate = rates.get(category, cls._standard_rate())

        # Calculate VAT from inclusive amount
        vat_amount = (Decimal(str(amount)) * Decimal(str(vat_rate))) / (Decimal("100") + Decimal(str(vat_rate)))
        subtotal = Decimal(str(amount)) - vat_amount

        return {
            "subtotal": round(subtotal, 2),
            "vat_rate": Decimal(str(vat_rate)),
            "vat_amount": round(vat_amount, 2),
            "total": Decimal(str(amount)),
        }

    @classmethod
    def detect_category(cls, description: str) -> str:
        """
        Auto-detect VAT category from item description.

        Uses keyword matching for NRS 2026 zero-rated items:
        - Medical supplies and services
        - Educational materials and tuition
        - Basic food items
        - Agricultural inputs
        """
        desc_lower = description.lower()

        # Zero-rated keywords (per NRS 2026 guidelines)
        zero_rated = [
            # Medical
            "medicine",
            "drug",
            "pharmaceutical",
            "medical",
            "hospital",
            "clinic",
            "health",
            "doctor",
            "surgery",
            "treatment",
            # Education
            "education",
            "school",
            "tuition",
            "textbook",
            "university",
            "college",
            "training",
            "course",
            "lecture",
            "book",
            # Basic food
            "bread",
            "rice",
            "flour",
            "milk",
            "egg",
            "vegetable",
            "fruit",
            "maize",
            "wheat",
            "cassava",
            "yam",
            "beans",
            "groundnut",
            # Agriculture
            "fertilizer",
            "seed",
            "farming",
            "agricultural",
            "livestock",
        ]

        # Exempt keywords
        exempt = ["insurance", "financial", "banking", "loan", "mortgage"]

        # Export keywords
        export = ["export", "international", "foreign", "overseas"]

        if any(kw in desc_lower for kw in zero_rated):
            return VATCategory.ZERO_RATED
        elif any(kw in desc_lower for kw in exempt):
            return VATCategory.EXEMPT
        elif any(kw in desc_lower for kw in export):
            return VATCategory.EXPORT

        return VATCategory.STANDARD


class FiscalCodeGenerator:
    """
    Fiscal code generation (SRP: Code generation only).

    Format: NGR-YYYYMMDD-USERID-INVOICEID-HASH
    Example: NGR-20260115-00123-00004567-A1B2C3D4
    """

    @staticmethod
    def generate(invoice: Invoice) -> str:
        """Generate unique fiscal code for invoice"""
        date_str = invoice.created_at.strftime("%Y%m%d")

        # Create unique hash from invoice data
        data = f"{invoice.issuer_id}-{invoice.id}-{invoice.amount}-{date_str}"
        hash_suffix = hashlib.sha256(data.encode()).hexdigest()[:8].upper()

        fiscal_code = f"NGR-{date_str}-{invoice.issuer_id:05d}-{invoice.id:08d}-{hash_suffix}"

        return fiscal_code

    @staticmethod
    def generate_signature(invoice: Invoice, fiscal_code: str) -> str:
        """
        Generate cryptographic signature for fiscal validation.

        Creates SHA256 hash of fiscal data for tamper detection.
        """
        signature_data = {
            "fiscal_code": fiscal_code,
            "issuer_id": invoice.issuer_id,
            "customer_name": invoice.customer.name if invoice.customer else "Unknown",
            "amount": str(invoice.amount),
            "vat_amount": str(invoice.vat_amount or 0),
            "timestamp": invoice.created_at.isoformat(),
        }

        # Create deterministic JSON string
        signature_string = json.dumps(signature_data, sort_keys=True)
        signature = hashlib.sha256(signature_string.encode()).hexdigest()

        return signature


class QRCodeGenerator:
    """QR code generation for fiscal invoices (SRP: QR codes only)"""

    @staticmethod
    def generate(fiscal_code: str, fiscal_signature: str, invoice: Invoice) -> str:
        """
        Generate QR code containing fiscal data.

        Returns base64-encoded PNG image for embedding in PDFs/web pages.
        """
        # Compact QR data structure
        qr_data = {
            "fc": fiscal_code,
            "fs": fiscal_signature[:16],  # Abbreviated signature
            "amt": float(invoice.amount),
            "vat": float(invoice.vat_amount or 0),
            "dt": invoice.created_at.strftime("%Y%m%d%H%M%S"),
        }

        # Generate QR code
        qr = qrcode.QRCode(version=1, box_size=10, border=4)
        qr.add_data(json.dumps(qr_data))
        qr.make(fit=True)

        # Convert to base64 PNG
        img = qr.make_image(fill_color="black", back_color="white")
        buffer = BytesIO()
        img.save(buffer, format="PNG")
        buffer.seek(0)

        qr_base64 = base64.b64encode(buffer.getvalue()).decode()

        return f"data:image/png;base64,{qr_base64}"


class FiscalTransmitter:
    """
    External fiscalization API communication (SRP: External API only).

    Placeholder transmitter; actual FIRS/Federal gateway integration pending credentials.
    Transmission is gated by settings.FISCALIZATION_ACCREDITED.
    """

    def __init__(self, api_url: Optional[str] = None, api_key: Optional[str] = None):
        self.api_url = api_url or getattr(settings, "FIRS_API_URL", None)
        self.api_key = api_key or getattr(settings, "FIRS_API_KEY", None)

    async def transmit(self, invoice: Invoice, fiscal_invoice: FiscalInvoice) -> Dict:
        """
        Transmit fiscalized invoice to external fiscalization API (placeholder).

        Returns response dict with transaction ID and validation status.
        Gating:
        - If settings.FISCALIZATION_ACCREDITED is False → skip, return pending_external.
        - If credentials missing → skip, return pending_configuration.
        """
        if not getattr(settings, "FISCALIZATION_ACCREDITED", False):
            logger.info("Fiscalization not accredited; skipping external transmission")
            return {"status": "pending_external", "message": "Accreditation pending"}
        if not self.api_url or not self.api_key:
            logger.info("Fiscalization API not configured; deferring transmission")
            return {"status": "pending_configuration", "message": "API credentials not set"}

        # Prepare NRS-compliant payload
        payload = {
            "fiscal_code": fiscal_invoice.fiscal_code,
            "fiscal_signature": fiscal_invoice.fiscal_signature,
            "invoice_data": {
                "invoice_number": invoice.invoice_id,
                "invoice_date": invoice.created_at.isoformat(),
                "customer_name": invoice.customer.name if invoice.customer else "Unknown",
                "subtotal": float(fiscal_invoice.subtotal),
                "vat_rate": fiscal_invoice.vat_rate,
                "vat_amount": float(fiscal_invoice.vat_amount),
                "total": float(fiscal_invoice.total_amount),
                "currency": "NGN",
                "items": [
                    {
                        "description": line.description,
                        "quantity": line.quantity,
                        "unit_price": float(line.unit_price),
                        "total": float(line.quantity * line.unit_price),
                    }
                    for line in invoice.lines
                ],
            },
        }

        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    f"{self.api_url}/fiscalize",
                    json=payload,
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    timeout=30.0,
                )

                if response.status_code == 200:
                    result = response.json()
                    logger.info("Invoice %s transmitted to fiscalization gateway successfully", invoice.id)
                    return {"status": "validated", "transaction_id": result.get("transaction_id"), "response": result}
                else:
                    logger.error("Fiscalization transmission failed: %s", response.status_code)
                    return {"status": "failed", "error": response.text}

            except Exception as e:
                logger.error("Fiscalization transmission exception: %s", e)
                return {"status": "failed", "error": str(e)}


class FiscalizationService:
    """
    Main fiscalization service (orchestrates other components).

    Coordinates:
    - VAT calculation
    - Fiscal code generation
    - QR code creation
    - Optional external transmission (FIRS) – gated & provisional
    """

    def __init__(self, db: Session):
        self.db = db
        self.vat_calculator = VATCalculator()
        self.code_generator = FiscalCodeGenerator()
        self.qr_generator = QRCodeGenerator()
        # Transmitter for external fiscalization (FIRS gateway placeholder)
        self.nrs_transmitter = FiscalTransmitter()
        # NRS client only initialized if feature flag enabled to avoid unnecessary resource usage
        self.nrs_client = get_nrs_client() if getattr(settings, "NRS_ENABLED", False) else None

    async def fiscalize_invoice(self, invoice_id: int) -> FiscalInvoice:
        """
        Fiscalize an invoice (provisional FIRS readiness).

        Process:
        1. Calculate VAT breakdown
        2. Generate fiscal code and signature
        3. Create QR code
        4. Attempt external transmission only if accredited & configured
        5. Store fiscal data

        Args:
            invoice_id: ID of invoice to fiscalize
        Returns:
            FiscalInvoice record with all fiscal data
        Raises:
            ValueError: If invoice not found or already fiscalized
        """
        # Fetch invoice
        invoice = self.db.query(Invoice).filter(Invoice.id == invoice_id).first()
        if not invoice:
            raise ValueError(f"Invoice {invoice_id} not found")

        # Check if already fiscalized
        if invoice.fiscal_data:
            logger.info("Invoice %s already fiscalized", invoice_id)
            return invoice.fiscal_data

        # Calculate VAT if not set
        if not invoice.vat_amount:
            vat_data = self.vat_calculator.calculate(invoice.amount, invoice.vat_category or "standard")
            invoice.vat_amount = vat_data["vat_amount"]
            invoice.vat_rate = float(vat_data["vat_rate"])

        # Generate fiscal elements
        fiscal_code = self.code_generator.generate(invoice)
        fiscal_signature = self.code_generator.generate_signature(invoice, fiscal_code)
        qr_code_data = self.qr_generator.generate(fiscal_code, fiscal_signature, invoice)

        # Create fiscal record
        fiscal_invoice = FiscalInvoice(
            invoice_id=invoice.id,
            fiscal_code=fiscal_code,
            fiscal_signature=fiscal_signature,
            qr_code_data=qr_code_data,
            subtotal=invoice.amount - (invoice.vat_amount or 0),
            vat_rate=invoice.vat_rate or 7.5,
            vat_amount=invoice.vat_amount or 0,
            total_amount=invoice.amount,
        )

        # Queue external transmission (async) instead of in-request I/O
        from app.workers.celery_app import celery_app  # local import to avoid early load cost

        celery_app.send_task("fiscalization.transmit_invoice", args=[fiscal_invoice.fiscal_code])
        tx_result = {"status": "queued", "message": "Transmission queued"}

        # Optional NRS transmission (stubbed) if feature flag enabled and accredited
        if getattr(settings, "NRS_ENABLED", False) and self.nrs_client:
            try:
                nrs_payload = {
                    "invoice_id": invoice.invoice_id,
                    "fiscal_code": fiscal_invoice.fiscal_code,
                    "amount": float(fiscal_invoice.total_amount),
                    "vat_amount": float(fiscal_invoice.vat_amount),
                    "issued_at": invoice.created_at.isoformat(),
                }
                nrs_result = self.nrs_client.transmit_invoice(nrs_payload)
                merged = fiscal_invoice.firs_response or {}
                merged["nrs"] = nrs_result
                fiscal_invoice.firs_response = merged
                if nrs_result.get("status") == "accepted" and not fiscal_invoice.transmitted_at:
                    fiscal_invoice.transmitted_at = datetime.now(timezone.utc)
            except Exception as e:
                logger.warning("NRS client transmission failed: %s", e)

        fiscal_invoice.firs_validation_status = tx_result.get("status", "pending")
        fiscal_invoice.firs_transaction_id = tx_result.get("transaction_id")
        if not fiscal_invoice.firs_response:
            fiscal_invoice.firs_response = tx_result

        # Update invoice flags
        invoice.is_fiscalized = True
        invoice.fiscal_code = fiscal_code

        # Persist
        self.db.add(fiscal_invoice)
        self.db.commit()
        self.db.refresh(fiscal_invoice)
        logger.info("Invoice %s fiscalized (queued transmit): %s", invoice_id, fiscal_code)
        return fiscal_invoice
