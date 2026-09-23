"""Invoice creation workflow mixin."""
from __future__ import annotations

import datetime as dt
import logging
from decimal import Decimal

from sqlalchemy.orm import Session, joinedload

from app import metrics
from app.core.exceptions import MissingBankDetailsError
from app.models import models
from app.services.fiscalization_service import VATCalculator
from app.utils.id_generator import generate_id
from app.utils.invoice_delivery import invoice_has_contact, is_online_only

logger = logging.getLogger(__name__)


class InvoiceCreationMixin:
    """Handles invoice creation and caching concerns."""

    db: Session

    def _find_recent_duplicate(self, issuer_id: int, data: dict) -> "models.Invoice | None":
        """Return a just-created identical revenue invoice, if any (idempotency).

        Matches on issuer + amount + customer identity + item descriptions within
        a short window, so an accidental double-submit reuses the first invoice
        instead of spawning a duplicate. Deliberately conservative: a genuine
        re-issue of the exact same invoice can just be created again after the
        window passes.
        """
        try:
            amount = Decimal(str(data.get("amount")))
        except Exception:  # noqa: BLE001
            return None
        # Quick-sale (walk-in) entries intentionally repeat: a shop can sell the
        # same-priced item with the same generic "Walk-in Customer" name and
        # description to several different customers within the same minute.
        # Deduping those would silently merge distinct sales and undercount
        # real business activity, so this guard only applies to named-customer
        # invoices where a repeat really does look like an accidental
        # double-submit.
        if data.get("channel") == "quick_sale":
            return None
        name = str(data.get("customer_name") or "").strip().lower()
        if not name:
            return None
        phone = str(data.get("customer_phone") or "").strip() or None
        email = str(data.get("customer_email") or "").strip().lower() or None
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=60)

        want_descs = sorted(
            (str(ld.get("description") or "").strip().lower())
            for ld in (data.get("lines") or [])
            if str(ld.get("description") or "").strip()
        )

        candidates = (
            self.db.query(models.Invoice)
            .options(
                joinedload(models.Invoice.customer),
                joinedload(models.Invoice.lines),
            )
            .filter(
                models.Invoice.issuer_id == issuer_id,
                models.Invoice.invoice_type == "revenue",
                models.Invoice.amount == amount,
                models.Invoice.created_at >= cutoff,
            )
            .order_by(models.Invoice.created_at.desc())
            .limit(5)
            .all()
        )

        for inv in candidates:
            cust = getattr(inv, "customer", None)
            if str(getattr(cust, "name", "") or "").strip().lower() != name:
                continue
            cphone = str(getattr(cust, "phone", "") or "").strip() or None
            cemail = str(getattr(cust, "email", "") or "").strip().lower() or None
            if phone and cphone and phone != cphone:
                continue
            if email and cemail and email != cemail:
                continue
            # Same set of item descriptions (when the request carried explicit
            # lines) — guards against deduping two different invoices that merely
            # share an amount + customer inside the window.
            if want_descs:
                have_descs = sorted(
                    (str(ln.description or "").strip().lower())
                    for ln in (inv.lines or [])
                    if str(ln.description or "").strip()
                )
                if have_descs != want_descs:
                    continue
            return inv
        return None

    def create_invoice(
        self,
        issuer_id: int,
        data: dict[str, object],
        async_pdf: bool = False,
        created_by_user_id: int | None = None,
        consume_balance: bool = True,
    ) -> models.Invoice:
        invoice_type = data.get("invoice_type", "revenue")

        # Idempotency safety net. A double-submit — a fast double-tap (the button's
        # disabled state only applies on the next render), an auto-retried request
        # after a 401 token refresh, or a flaky mobile network — can hit this path
        # twice and create duplicate invoices that BOTH go out to the customer. If
        # an identical revenue invoice for this issuer was created seconds ago,
        # return it instead of creating a second one.
        if invoice_type == "revenue":
            existing = self._find_recent_duplicate(issuer_id, data)
            if existing is not None:
                logger.info(
                    "Deduped duplicate invoice create for issuer %s -> existing %s",
                    issuer_id, existing.invoice_id,
                )
                return existing

        if consume_balance:
            self.enforce_quota(issuer_id, invoice_type, amount=data.get("amount"))

        if invoice_type == "revenue":
            customer = self._get_or_create_customer(
                data.get("customer_name"),
                data.get("customer_phone"),
                data.get("customer_email"),
            )
        else:
            vendor_name = data.get("vendor_name") or data.get("merchant") or "Expense Vendor"
            customer = self._get_or_create_customer(vendor_name, None, None)

        discount_raw = data.get("discount_amount")
        discount_amount = Decimal(str(discount_raw)) if discount_raw else None

        # ── VAT calculation (opt-in: only for VAT-registered businesses) ──
        # SuoOps calculates VAT from what the business charges — not what the law assumes.
        from app.models.tax_models import TaxProfile
        tax_profile = self.db.query(TaxProfile).filter(TaxProfile.user_id == issuer_id).first()
        is_vat_registered = tax_profile.vat_registered if tax_profile else False

        default_description = (data.get("description") or "Item").strip() or "Item"

        if is_vat_registered:
            # VAT enabled: auto-detect category from item descriptions, then calculate
            lines_data_preview = data.get("lines") or [{"description": default_description}]
            combined_desc = " ".join(ld.get("description", "") for ld in lines_data_preview)
            vat_category = data.get("vat_category") or VATCalculator.detect_category(combined_desc)

            inv_amount = Decimal(str(data.get("amount")))
            taxable_amount = inv_amount - (discount_amount or Decimal(0))
            vat_result = VATCalculator.calculate(taxable_amount, vat_category)
        else:
            # VAT OFF by default — no VAT assumptions for non-registered businesses
            vat_category = "none"
            vat_result = {"vat_rate": Decimal("0"), "vat_amount": Decimal("0")}

        # Determine initial status based on invoice type and contact info
        customer_phone = data.get("customer_phone")
        customer_email = data.get("customer_email")
        has_contact_info = bool(customer_phone or customer_email)
        
        if invoice_type == "expense":
            status = "paid"
            paid_at = dt.datetime.now(dt.timezone.utc)
        elif has_contact_info:
            # Has contact info - pending notification
            status = "pending"
            paid_at = None
        else:
            # No contact info - skip to awaiting confirmation (manual payment tracking)
            status = "awaiting_confirmation"
            paid_at = None

        # ── Professional defaults ──────────────────────────────────────
        # Respect the seller's choice on due date: if they don't set one, DON'T
        # invent one. An auto due-date makes the invoice go "overdue" and fires
        # payment-reminder notifications the seller never asked for. Storefront
        # orders are paid instantly, so they never carry a due date either.
        due_date = data.get("due_date")

        # Professional payment instruction default for revenue invoices — only
        # reference a due date when one actually exists.
        notes = data.get("notes")
        if not notes and invoice_type == "revenue":
            notes = (
                "Payment is due by the date shown above. Thank you for your business."
                if due_date is not None
                else "Thank you for your business."
            )

        invoice = models.Invoice(
            invoice_id=generate_id("INV" if invoice_type == "revenue" else "EXP"),
            issuer_id=issuer_id,
            created_by_user_id=created_by_user_id or issuer_id,  # Track actual creator
            customer=customer,
            amount=Decimal(str(data.get("amount"))),
            currency=data.get("currency", "NGN"),
            discount_amount=discount_amount,
            due_date=due_date,
            status=status,
            paid_at=paid_at,
            invoice_type=invoice_type,
            category=data.get("category"),
            vendor_name=data.get("vendor_name"),
            merchant=data.get("merchant"),
            receipt_url=data.get("receipt_url"),
            receipt_text=data.get("receipt_text"),
            input_method=data.get("input_method"),
            channel=data.get("channel"),
            payment_method=data.get("payment_method"),
            verified=data.get("verified", False),
            expense_flag_reason=data.get("expense_flag_reason"),
            notes=notes,
            vat_rate=float(vat_result["vat_rate"]),
            vat_amount=vat_result["vat_amount"],
            vat_category=str(vat_category),
        )

        # Lock in the SuoOps commission for this invoice at creation so reports
        # read the fee actually charged, not a recompute at the current rate.
        # Storefront/online orders are billed the storefront rate (Paystack
        # collects it on payment); every other revenue invoice is billed the
        # manual rate from the wallet. Expenses carry no fee.
        if invoice_type == "revenue":
            from app.utils.feature_gate import platform_fee_kobo

            fee_channel = "storefront" if data.get("channel") == "storefront" else "manual"
            invoice.platform_fee_kobo = platform_fee_kobo(invoice.amount, channel=fee_channel)

        lines_data = data.get("lines") or [
            {"description": default_description, "quantity": 1, "unit_price": invoice.amount}
        ]
        for line_data in lines_data:
            description = (line_data.get("description") or default_description).strip() or default_description
            invoice.lines.append(
                models.InvoiceLine(
                    description=description,
                    quantity=line_data.get("quantity", 1),
                    unit_price=Decimal(str(line_data["unit_price"])),
                    product_id=line_data.get("product_id"),  # Link to inventory product
                )
            )

        self.db.add(invoice)
        self.db.commit()
        self.db.refresh(invoice)

        # Process inventory updates ONLY for expense invoices at creation time
        # Revenue invoices have inventory deducted when marked as PAID (see status.py)
        # This ensures proper workflow: Invoice Created -> Payment Received -> Stock Deducted
        if invoice_type == "expense" and hasattr(self, 'process_inventory_for_invoice'):
            self.process_inventory_for_invoice(invoice, lines_data)

        if self.cache:
            self.cache.invalidate_user_invoices(issuer_id)

        user = self.db.query(models.User).filter(models.User.id == issuer_id).one()
        metrics.invoice_created_by_plan(user.plan.value)
        total_amount = sum(float(line.unit_price) * line.quantity for line in invoice.lines)
        metrics.record_invoice_amount(total_amount)

        if invoice_type == "revenue" and invoice.channel in ("storefront", "quick_sale"):
            # Storefront orders and quick sales have no pre-payment PDF — the
            # deliverable (receipt) is produced on payment. A quick sale is
            # marked paid in the same request it's created in, so there's never
            # a "please pay" document to send; it also means we don't need the
            # business's bank details just to record a cash sale. Other
            # business invoices (even online-only) keep a PDF; it just hides
            # the bank and shows the pay link instead.
            invoice.pdf_url = None
        elif async_pdf:
            self._queue_pdf_generation(invoice, invoice_type, user)
        else:
            # PDF generation must never block invoice creation — the invoice row
            # is already committed above. If synchronous generation fails (e.g.
            # logo/S3 or renderer hiccup) fall back to async so the business still
            # gets a created invoice instead of a 500.
            try:
                invoice.pdf_url = self._generate_pdf(invoice, invoice_type, user)
            except Exception:
                logger.exception(
                    "Synchronous PDF generation failed for invoice %s; "
                    "falling back to async generation",
                    invoice.invoice_id,
                )
                try:
                    self._queue_pdf_generation(invoice, invoice_type, user)
                except Exception:
                    logger.exception(
                        "Async PDF fallback also failed to enqueue for invoice %s",
                        invoice.invoice_id,
                    )
                    invoice.pdf_url = None

        # Deduct from invoice_balance for revenue invoices (new billing model).
        # Storefront / online-commission orders pass consume_balance=False so the
        # business is not charged a pack invoice for an inbound sale.
        if invoice_type == "revenue" and consume_balance:
            self.deduct_invoice_balance(issuer_id, amount=invoice.amount)

        self.db.commit()
        self.db.refresh(invoice)
        invoice = (
            self.db.query(models.Invoice)
            .options(
                joinedload(models.Invoice.customer),
                joinedload(models.Invoice.issuer),
            )
            .filter(models.Invoice.id == invoice.id)
            .one()
        )

        logger.info(
            "Created %s invoice %s for issuer %s",
            invoice_type,
            invoice.invoice_id,
            issuer_id,
        )
        if invoice_type == "revenue":
            # Refresh user to get updated balance
            self.db.refresh(user)
            logger.info(
                "Revenue invoice - remaining balance: %d",
                getattr(user, 'invoice_balance', 0),
            )
            metrics.invoice_created()

            # Once-a-day WhatsApp professionalism-score nudge for the business
            # owner when they create a manual invoice (deduped inside the task).
            # Skip storefront orders — those are placed by the customer.
            if invoice.channel != "storefront":
                try:
                    from app.workers.tasks.welcome_tasks import (
                        send_daily_professionalism_score,
                    )

                    send_daily_professionalism_score.delay(issuer_id)
                except Exception:
                    logger.exception(
                        "Failed to enqueue professionalism-score nudge for %s", issuer_id
                    )

        if self.cache:
            self.cache.invalidate_user_invoices(issuer_id)

        return invoice

    def _queue_pdf_generation(self, invoice: models.Invoice, invoice_type: str, user: models.User) -> None:
        from app.storage.s3_client import s3_client
        from app.workers.tasks import generate_invoice_pdf_async

        bank_details = None
        online_only = is_online_only(
            user, has_contact=invoice_has_contact(invoice), channel=invoice.channel
        )
        if invoice_type == "revenue" and not online_only:
            bank_details = self._ensure_bank_details(user)

        # Generate fresh presigned URL for logo
        logo_url = None
        if user.logo_url:
            logo_key = s3_client.extract_key_from_url(user.logo_url)
            if logo_key:
                logo_url = s3_client.get_presigned_url(logo_key, expires_in=3600)
            if not logo_url:
                logo_url = user.logo_url  # Fallback to stored URL

        generate_invoice_pdf_async.delay(
            invoice_id=invoice.id,
            bank_details=bank_details,
            logo_url=logo_url,
            user_plan=user.plan.value,
        )
        invoice.pdf_url = None
        logger.info("Queued async PDF generation for invoice %s", invoice.invoice_id)

    def _generate_pdf(self, invoice: models.Invoice, invoice_type: str, user: models.User) -> str | None:
        from app.storage.s3_client import s3_client
        
        online_only = is_online_only(
            user, has_contact=invoice_has_contact(invoice), channel=invoice.channel
        )
        bank_details = (
            self._ensure_bank_details(user)
            if invoice_type == "revenue" and not online_only
            else None
        )
        
        # Generate fresh presigned URL for logo
        logo_url = None
        if user.logo_url:
            logo_key = s3_client.extract_key_from_url(user.logo_url)
            if logo_key:
                logo_url = s3_client.get_presigned_url(logo_key, expires_in=3600)
            if not logo_url:
                logo_url = user.logo_url  # Fallback to stored URL
        
        return self.pdf_service.generate_invoice_pdf(
            invoice,
            bank_details=bank_details,
            logo_url=logo_url,
            user_plan=user.plan.value,
        )

    def _ensure_bank_details(self, user: models.User) -> dict[str, str]:
        if not user.bank_name or not user.account_number:
            raise MissingBankDetailsError()
        return {
            "bank_name": user.bank_name,
            "account_number": user.account_number,
            "account_name": user.account_name,
        }

    def _normalize_phone(self, phone: str) -> str:
        """Normalize phone number to consistent format for storage and lookup."""
        from app.utils.phone import normalize_phone

        return normalize_phone(phone)

    def _get_or_create_customer(
        self, name: str, phone: str | None, email: str | None = None
    ) -> models.Customer:
        # Normalize phone for consistent lookup/storage
        normalized_phone = self._normalize_phone(phone) if phone else None
        
        # Build phone candidates for lookup (handle existing records with different formats)
        phone_candidates = set()
        if normalized_phone:
            phone_candidates.add(normalized_phone)
            # Also check legacy/local formats in case old records exist
            if normalized_phone.startswith("+234") and len(normalized_phone) == 14:
                digits_only = normalized_phone[1:]
                phone_candidates.add(digits_only)  # 234XXXXXXXXXX
                phone_candidates.add("0" + digits_only[3:])  # 0XXXXXXXXXX
            phone_candidates.add(phone)  # Original input too
        
        q = self.db.query(models.Customer).filter(models.Customer.name == name)
        if phone_candidates:
            q = q.filter(models.Customer.phone.in_(list(phone_candidates)))
        elif email:
            q = q.filter(models.Customer.email == email)
        else:
            # No phone or email provided - look for customer with same name AND no contact info
            # This prevents matching an existing customer with a different phone/email
            q = q.filter(models.Customer.phone.is_(None), models.Customer.email.is_(None))
        existing = q.first()
        if existing:
            if email and not existing.email:
                existing.email = email
            # Update phone to normalized format if different
            if normalized_phone and existing.phone != normalized_phone:
                existing.phone = normalized_phone
            return existing
        customer = models.Customer(name=name, phone=normalized_phone, email=email)
        self.db.add(customer)
        self.db.flush()
        return customer
