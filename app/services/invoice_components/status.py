"""Status update and public retrieval helpers."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy.orm import Session, joinedload, selectinload

from app import metrics
from app.core.exceptions import InvalidInvoiceStatusError, InvoiceNotFoundError
from app.models import models
from app.utils.async_utils import run_async
from app.utils.invoice_delivery import invoice_has_contact, is_online_only

logger = logging.getLogger(__name__)


def _manual_confirm_needs_review(db: Session, invoice: models.Invoice) -> bool:
    """True when a SELF 'mark as paid' on this large MANUAL invoice should be held
    for review — i.e. the amount is over the review ceiling AND the confirming
    account looks low-trust (flagged, brand-new, dormant, or with no prior paid
    invoice). Gated by settings.MANUAL_CONFIRM_REVIEW_NAIRA (0 disables)."""
    from app.core.config import settings

    ceiling = settings.MANUAL_CONFIRM_REVIEW_NAIRA or 0
    if ceiling <= 0 or float(invoice.amount or 0) <= ceiling:
        return False

    issuer = invoice.issuer or (db.query(models.User).filter(models.User.id == invoice.issuer_id).first())
    if issuer is None:
        return True  # unknown issuer → hold to be safe

    now = dt.datetime.now(dt.timezone.utc)

    def _aware(v):
        return v.replace(tzinfo=dt.timezone.utc) if (v is not None and v.tzinfo is None) else v

    # Flagged for review → always hold.
    if getattr(issuer, "flagged_for_review", False):
        return True

    # Brand-new account.
    created = _aware(getattr(issuer, "created_at", None))
    if created is not None and (now - created).days < settings.MANUAL_CONFIRM_MIN_ACCOUNT_AGE_DAYS:
        return True

    # Dormant (no recent login) — the ELECTRIFY pattern: big invoices then gone.
    last_login = _aware(getattr(issuer, "last_login", None))
    if last_login is None or (now - last_login).days > settings.MANUAL_CONFIRM_DORMANT_DAYS:
        return True

    # No prior PAID revenue invoice (this one excluded) → unproven payer.
    prior_paid = (
        db.query(models.Invoice.id)
        .filter(
            models.Invoice.issuer_id == issuer.id,
            models.Invoice.invoice_type == "revenue",
            models.Invoice.status == "paid",
            models.Invoice.id != invoice.id,
        )
        .first()
    )
    return prior_paid is None


class InvoiceStatusMixin:
    db: Session

    def update_status(
        self,
        issuer_id: int,
        invoice_id: str,
        status: str,
        updated_by_user_id: int | None = None,
        via_online: bool = False,
        force: bool = False,
    ) -> models.Invoice:
        if status not in {"pending", "awaiting_confirmation", "paid", "cancelled"}:
            raise InvalidInvoiceStatusError(new_status=status)

        invoice = (
            self.db.query(models.Invoice)
            .options(
                joinedload(models.Invoice.customer),
                joinedload(models.Invoice.issuer),
                joinedload(models.Invoice.created_by),  # Load creator for PDF
                joinedload(models.Invoice.status_updated_by),  # Load confirmer for PDF
                selectinload(models.Invoice.lines),  # Load lines for inventory processing
            )
            .filter(models.Invoice.invoice_id == invoice_id, models.Invoice.issuer_id == issuer_id)
            .one_or_none()
        )
        if not invoice:
            raise InvoiceNotFoundError(invoice_id)

        previous_status = invoice.status
        if previous_status == status:
            return invoice

        # ── Invoice status state machine ─────────────────────────────
        # pending             → awaiting_confirmation, paid, cancelled
        # awaiting_confirmation → pending, paid, cancelled
        # paid                → (terminal — no further changes)
        # cancelled           → pending (re-open only)
        _VALID_TRANSITIONS: dict[str, set[str]] = {
            "pending": {"awaiting_confirmation", "paid", "cancelled"},
            "awaiting_confirmation": {"pending", "paid", "cancelled"},
            "paid": set(),  # terminal state
            "cancelled": {"pending"},  # can only re-open
        }

        allowed = _VALID_TRANSITIONS.get(previous_status, set())
        if status not in allowed:
            raise ValueError(f"Cannot change invoice from '{previous_status}' to '{status}'")

        # ── Anti-GMV-bloat: hold a large SELF manual confirmation for review ──
        # A low-trust account marking a big MANUAL invoice paid ITSELF (not an
        # online payment) is the classic way junk/fake invoices bloat GMV. Block
        # that confirmation (it stays unpaid, out of GMV) unless it's a real
        # online payment (via_online) or a super-admin force-confirm.
        if (
            status == "paid"
            and previous_status != "paid"
            and not via_online
            and not force
            and invoice.invoice_type == "revenue"
            and (invoice.channel is None or invoice.channel != "storefront")
            and _manual_confirm_needs_review(self.db, invoice)
        ):
            raise ValueError(
                "This invoice is too large to mark paid from a new or inactive "
                "account. For your protection it's been held for review — please "
                "contact SuoOps support to verify the payment."
            )

        # Offline settlement of an online-only invoice consumes a pack. The
        # business either earns us a commission (paid online via webhook,
        # via_online=True) or pays for a pack (marking it paid manually). This
        # Offline settlement of an online-only invoice consumes a pack. The
        # business either earns us a commission (paid online via webhook,
        # via_online=True) or pays for a pack (marking it paid manually). This
        # closes the "free invoice + bypass the commission" loophole.
        #
        # EXCEPTION: an invoice already in `awaiting_confirmation` got there via
        # the customer's on-platform bank-transfer button, which is ONLY shown
        # for NON online-only invoices. So it was genuinely a bank-transfer
        # invoice when the customer paid — confirming it must follow the normal
        # (no wallet charge) path even if the business enabled online payments
        # afterward. Otherwise the business gets stuck unable to confirm a real
        # bank transfer because their wallet is empty.
        if (
            status == "paid"
            and previous_status not in ("paid", "awaiting_confirmation")
            and not via_online
            and is_online_only(
                invoice.issuer,
                has_contact=invoice_has_contact(invoice),
                channel=invoice.channel,
            )
        ):
            self.enforce_quota(issuer_id, "revenue", amount=invoice.amount)
            self.deduct_invoice_balance(issuer_id, amount=invoice.amount)

        invoice.status = status

        # Track who updated the status and when
        if updated_by_user_id and status in {"paid", "cancelled"}:
            invoice.status_updated_by_user_id = updated_by_user_id
            invoice.status_updated_at = dt.datetime.now(dt.timezone.utc)

        if status == "paid" and invoice.paid_at is None:
            invoice.paid_at = dt.datetime.now(dt.timezone.utc)
        self.db.commit()

        if invoice.paid_at and invoice.paid_at.tzinfo is None:
            invoice.paid_at = invoice.paid_at.replace(tzinfo=dt.timezone.utc)
            self.db.commit()

        if status == "paid" and previous_status != "paid":
            # Refresh invoice to load updated relationships (created_by, status_updated_by)
            self.db.refresh(invoice)
            # Explicitly load the status_updated_by relationship if we just set it
            if invoice.status_updated_by_user_id and not invoice.status_updated_by:
                invoice = (
                    self.db.query(models.Invoice)
                    .options(
                        joinedload(models.Invoice.customer),
                        joinedload(models.Invoice.issuer),  # Load issuer for logo/business name
                        joinedload(models.Invoice.created_by),
                        joinedload(models.Invoice.status_updated_by),
                        selectinload(models.Invoice.lines),
                    )
                    .filter(models.Invoice.invoice_id == invoice_id)
                    .one()
                )
            self._handle_manual_payment(invoice, via_online=via_online)

            # ── First-paid referral nudge: when this paid invoice is the
            #    user's first, queue a one-tap WhatsApp referral card. The
            #    Celery task itself confirms "first paid" + dedups, so we
            #    can dispatch optimistically without a count query here.
            try:
                from app.workers.tasks.welcome_tasks import (
                    send_first_paid_referral_nudge,
                )

                send_first_paid_referral_nudge.delay(invoice.issuer_id)
            except Exception:
                logger.exception(
                    "Failed to enqueue first-paid referral nudge for issuer %s",
                    invoice.issuer_id,
                )

        if self.cache:
            self.cache.invalidate_invoice(invoice_id)
            self.cache.invalidate_user_invoices(issuer_id)

        return self.get_invoice(issuer_id, invoice_id)

    def confirm_transfer(self, invoice_id: str) -> models.Invoice:
        normalized_id = (invoice_id or "").strip().upper()
        invoice = (
            self.db.query(models.Invoice)
            .options(selectinload(models.Invoice.customer))
            .filter(models.Invoice.invoice_id == normalized_id)
            .populate_existing()
            .with_for_update()
            .one_or_none()
        )
        if not invoice:
            raise ValueError("Invoice not found")

        if invoice.status in {"paid", "awaiting_confirmation"}:
            return invoice

        if invoice.status != "pending":
            raise InvalidInvoiceStatusError(
                current_status=invoice.status,
                new_status="awaiting_confirmation",
            )

        previous_status = invoice.status
        invoice.status = "awaiting_confirmation"
        self.db.commit()
        if self.cache:
            self.cache.invalidate_invoice(invoice.invoice_id)
            self.cache.invalidate_user_invoices(invoice.issuer_id)
        logger.info(
            "Invoice %s status transitioned %s → awaiting_confirmation after customer confirmation",
            invoice_id,
            previous_status,
        )
        self._notify_business_of_transfer(invoice)
        return invoice

    def get_public_invoice(self, invoice_id: str) -> tuple[models.Invoice, models.User]:
        # Invoice IDs are stored uppercase (generate_id ends with .upper()), but a
        # pay link can arrive lower/mixed case (e.g. WhatsApp URL buttons may
        # lowercase the suffix), so normalise before the exact-match lookup.
        normalized_id = (invoice_id or "").strip().upper()
        invoice = (
            self.db.query(models.Invoice)
            .options(selectinload(models.Invoice.customer), selectinload(models.Invoice.lines))
            .filter(models.Invoice.invoice_id == normalized_id)
            .one_or_none()
        )
        if not invoice:
            raise ValueError("Invoice not found")

        issuer = self.db.query(models.User).filter(models.User.id == invoice.issuer_id).one_or_none()
        if not issuer:
            raise ValueError("Invoice issuer not found")
        return invoice, issuer

    def _handle_manual_payment(self, invoice: models.Invoice, via_online: bool = False) -> None:
        metrics.invoice_paid()

        # Capture invoice_id early to avoid DB access issues after potential errors
        invoice_id = invoice.invoice_id

        # NOTE: escrow activation (pending -> held) is done by the webhook
        # handler `_finalize_invoice_payment` in routes_webhooks.py, which has
        # the charge_reference / card_fingerprint / review_reason context
        # needed for refunds + card-fraud gating. Do NOT re-activate here —
        # firing without that context would leave those fields blank and
        # defeat card-fraud detection on webhook-confirmed orders.

        # Process inventory deduction for revenue invoices when paid
        self._process_inventory_on_payment(invoice)

        # Generate the receipt PDF up front. The WhatsApp receipt/invoice
        # templates have a REQUIRED document (PDF) header, so if the PDF link is
        # missing Meta rejects the template and the free-form fallback is blocked
        # for a first-time buyer. We try ONCE inline (fast path); on failure we
        # queue the generation via Celery instead of retrying inline — WeasyPrint
        # can take seconds and a second inline retry under load stacks up on the
        # request thread. The Celery task has its own retry with backoff.
        try:
            if not invoice.receipt_pdf_url:
                invoice.receipt_pdf_url = self.pdf_service.generate_receipt_pdf(invoice)
                self.db.commit()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Receipt PDF generation failed for %s (queuing async): %s",
                invoice_id,
                exc,
            )
            try:
                from app.workers.tasks.pdf_tasks import generate_receipt_pdf_async

                generate_receipt_pdf_async.delay(invoice.id)
            except Exception as exc2:  # noqa: BLE001
                logger.error(
                    "Failed to queue receipt PDF for %s: %s",
                    invoice_id,
                    exc2,
                )
        if not invoice.receipt_pdf_url:
            logger.error(
                "No receipt PDF for %s — the WhatsApp receipt template can't attach its "
                "required document header, so it may not deliver to a first-time buyer.",
                invoice_id,
            )

        # Send receipt notification (do this BEFORE low stock check to ensure receipt is sent)
        logger.info("Invoice %s manually marked as paid, sending receipt", invoice_id)
        try:
            from app.services.notification.service import NotificationService

            service = NotificationService()
            customer_email = getattr(invoice.customer, "email", None) if invoice.customer else None
            customer_phone = getattr(invoice.customer, "phone", None) if invoice.customer else None

            async def _run():  # pragma: no cover - network IO
                return await service.send_receipt_notification(
                    invoice=invoice,
                    customer_email=customer_email,
                    customer_phone=customer_phone,
                    # Prefer the receipt PDF just generated above. The invoice
                    # PDF may still be None here (storefront orders generate it
                    # asynchronously), which would leave the receipt with no
                    # attachment.
                    pdf_url=invoice.receipt_pdf_url or invoice.pdf_url,
                )

            results = run_async(_run())
            if results:
                logger.info(
                    "Receipt sent for invoice %s - Email: %s, WhatsApp: %s",
                    invoice_id,
                    results["email"],
                    results["whatsapp"],
                )

            # If no customer contact info, notify business with receipt PDF via WhatsApp
            if not customer_email and not customer_phone:
                self._notify_business_with_receipt(invoice)

        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send receipt notifications for %s: %s", invoice_id, exc)

        # Tell the business their money landed. Fire for any online (Paystack)
        # payment — storefront orders and business invoices paid via the link —
        # since the owner didn't mark it themselves. Manual "mark as paid" (the
        # owner's own action) doesn't need a notification.
        if via_online or getattr(invoice, "channel", None) == "storefront":
            self._notify_business_of_order(invoice)

        # Referral settlement for online/storefront sales: SuoOps collects the
        # flat 3% via Paystack on a storefront order (the wallet is untouched),
        # so pay the referrer their share of THAT 3% fee — never the gross sale.
        if getattr(invoice, "channel", None) == "storefront":
            try:
                from app.services.referral_service import ReferralService
                from app.utils.feature_gate import platform_fee_kobo

                fee_naira = platform_fee_kobo(invoice.amount) // 100
                if fee_naira > 0:
                    ReferralService(self.db).process_storefront_commission(
                        invoice.issuer_id, suoops_fee_naira=fee_naira
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Failed to process storefront referral commission for %s: %s",
                    invoice_id,
                    exc,
                )

        # Check for low stock and send alerts (non-critical, done after receipt is sent)
        self._check_and_send_low_stock_alerts(invoice)

    def _notify_business_of_order(self, invoice: models.Invoice) -> None:
        """Alert the business of a new paid storefront order so they can fulfil it."""
        user = getattr(invoice, "issuer", None) or (
            self.db.query(models.User).filter(models.User.id == invoice.issuer_id).one_or_none()
        )
        if not user:
            logger.warning("Cannot notify business of order %s: issuer missing", invoice.invoice_id)
            return

        from app.core.config import settings

        frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
        order_link = f"{frontend_url.rstrip('/')}/dashboard/invoices/{invoice.invoice_id}"
        customer_name = invoice.customer.name if invoice.customer else "Customer"
        customer_phone = getattr(invoice.customer, "phone", None) if invoice.customer else None

        items = "\n".join(f"• {ln.quantity} × {ln.description}" for ln in (invoice.lines or [])) or "• (see dashboard)"
        settle_bank = getattr(user, "payout_bank_name", None) or getattr(user, "bank_name", None)
        settle_to = f"your {settle_bank} account" if settle_bank else "your bank account"
        is_storefront = getattr(invoice, "channel", None) == "storefront"
        # Buyer-protection HELD order? (an escrow row exists for this invoice)
        held_escrow = None
        if is_storefront:
            held_escrow = (
                self.db.query(models.StorefrontOrderEscrow)
                .filter(models.StorefrontOrderEscrow.invoice_id == invoice.id)
                .first()
            )
        # Automated courier order? The courier collects and delivers using the
        # buyer's contact + address it already holds, so the seller doesn't need
        # (and shouldn't receive) the buyer's phone or home address.
        is_courier = bool(held_escrow is not None and held_escrow.delivery_courier)
        # Service/digital order (nothing ships) — protection is confirm + fast
        # auto-release, so drop all the deliver/dispatch language.
        is_service = held_escrow is not None and not getattr(held_escrow, "requires_delivery", True)
        if is_storefront:
            header = "🛒 New paid order — payment confirmed ✅"
            if is_service:
                footer = (
                    "✅ No delivery needed — this is a service order. You'll be paid your "
                    "full amount automatically after the buyer-protection window, or "
                    f"sooner when the buyer confirms.\n🔗 Order details:\n{order_link}"
                )
            elif held_escrow is not None:
                footer = (
                    "📦 Prepare and deliver the order. Mark it “sent out”, then "
                    "“delivered”, in your dashboard to add proof — that protects your "
                    f"payout.\n🔗 Order details:\n{order_link}"
                )
            else:
                footer = (
                    "📦 No action needed on payment — just prepare and deliver the order.\n"
                    f"🔗 Order details:\n{order_link}"
                )
        else:
            header = "💰 Payment received — invoice paid ✅"
            footer = (
                "📦 No action needed — your customer already has the receipt.\n" f"🔗 Invoice details:\n{order_link}"
            )
        # Settlement copy differs for held (buyer-protection) vs normal orders.
        if held_escrow is not None:
            _confirm_clause = (
                "sooner when the buyer confirms it's done" if is_service else "sooner if the buyer confirms delivery"
            )
            settle_line = (
                "💰 Payment is HELD under buyer protection. Your FULL payout is "
                f"released to {settle_to} on our next daily settlement run once the "
                f"buyer-protection window passes — {_confirm_clause}. (The service "
                "fee was paid by the buyer.)\n\n"
            )
        else:
            settle_line = (
                f"💰 Your full payment settles to {settle_to} by the next business "
                "day. (The service fee was paid by the buyer.)\n\n"
            )
        # Delivery details (GPS maps link + landmark note) are stored on the
        # invoice notes at order time — surface them so the business knows where
        # to deliver.
        # Delivery details. For automated courier orders the courier handles the
        # buyer's address — tell the seller how to hand off, not where the buyer
        # lives. For self-delivery orders, surface the buyer's address note.
        delivery_block = ""
        if is_service:
            pass  # service/digital order — nothing ships
        elif is_storefront and is_courier:
            courier = held_escrow.delivery_courier
            if held_escrow.delivery_service_type == "dropoff":
                station = held_escrow.delivery_dropoff_station
                where = f" at {station}" if station else ""
                delivery_block = (
                    f"\n\n🚚 Drop the package off for {courier}{where}, then mark it "
                    f"“sent out” to book it. {courier} delivers it to the buyer."
                )
            else:
                delivery_block = (
                    f"\n\n🚚 {courier} will pick up from your store address — mark the "
                    f"order “sent out” to book the pickup. {courier} delivers it to the buyer."
                )
        elif is_storefront:
            notes = (getattr(invoice, "notes", None) or "").strip()
            if notes:
                delivery_block = f"\n\n🚚 {notes}"
        message = (
            f"{header}\n\n"
            f"👤 {customer_name}"
            + (f" ({customer_phone})" if (customer_phone and not is_courier) else "")
            + f"\n💵 ₦{invoice.amount:,.2f} — paid online\n"
            + settle_line
            + f"{items}"
            f"{delivery_block}\n\n"
            f"{footer}"
        )

        try:
            from app.services.notification.service import NotificationService

            service = NotificationService()

            async def _run():  # pragma: no cover - network IO
                if user.email:
                    try:
                        await service.send_email(
                            to_email=user.email,
                            subject=f"New paid order — {invoice.invoice_id}",
                            body=message,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Order email to business failed %s: %s", invoice.invoice_id, exc)
                if user.phone and not is_storefront:
                    # Storefront orders notify the business by EMAIL ONLY — the
                    # free-form WhatsApp alert fails outside WhatsApp's 24h window
                    # (error 131047), so email is the reliable channel.
                    try:
                        from app.bot.whatsapp_client import WhatsAppClient

                        whatsapp_key = getattr(settings, "WHATSAPP_API_KEY", None)
                        if whatsapp_key:
                            WhatsAppClient(whatsapp_key).send_text(user.phone, message)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Order WhatsApp to business failed %s: %s", invoice.invoice_id, exc)

            run_async(_run())
        except Exception as exc:  # noqa: BLE001
            logger.error("Order notification dispatch failed for %s: %s", invoice.invoice_id, exc)

    def _notify_business_of_transfer(self, invoice: models.Invoice) -> None:
        # Per-invoice notification cooldown (anti-griefing, defense-in-depth on
        # top of the status-transition guard in confirm_transfer): even if the
        # invoice is flipped back to 'pending' and re-confirmed, don't re-blast
        # the business more than once per window. Fail-open so a cache blip never
        # drops a genuine notification.
        try:
            from app.db.redis_client import get_redis_client

            _k = f"invoice:xfernotify:{invoice.invoice_id}"
            if not get_redis_client().set(_k, "1", nx=True, ex=600):
                logger.info(
                    "Skipping duplicate transfer notification for invoice %s (cooldown)",
                    invoice.invoice_id,
                )
                return
        except Exception:  # noqa: BLE001 — fail open
            pass

        try:
            user = self.db.query(models.User).filter(models.User.id == invoice.issuer_id).one_or_none()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to load issuer for invoice %s: %s", invoice.invoice_id, exc)
            return

        if not user:
            logger.warning("Cannot notify business for invoice %s: issuer missing", invoice.invoice_id)
            return

        from app.core.config import settings

        frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
        verify_link = f"{frontend_url.rstrip('/')}/dashboard/invoices/{invoice.invoice_id}"

        customer_name = invoice.customer.name if invoice.customer else "Customer"
        message = (
            f"💰 Payment Notification!\n\n"
            f"Customer reported a bank transfer for:\n\n"
            f"📄 Invoice: {invoice.invoice_id}\n"
            f"💵 Amount: ₦{invoice.amount:,.2f}\n"
            f"👤 Customer: {customer_name}\n\n"
            f"🔗 Verify & Mark as Paid:\n{verify_link}\n\n"
            f"✅ Please verify the funds in your bank account "
            f"and mark the invoice as PAID to send the customer their receipt."
        )

        try:
            from app.services.notification.service import NotificationService

            service = NotificationService()

            async def _run():  # pragma: no cover - network IO
                results = {"email": False, "whatsapp": False}
                if user.email:
                    try:
                        results["email"] = await service.send_email(
                            to_email=user.email,
                            subject=f"Payment Confirmation - Invoice {invoice.invoice_id}",
                            body=message,
                        )
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed email notify business %s: %s", invoice.invoice_id, exc)
                if user.phone:
                    try:
                        # Send WhatsApp notification to business
                        from app.bot.whatsapp_client import WhatsAppClient
                        from app.core.config import settings

                        whatsapp_key = getattr(settings, "WHATSAPP_API_KEY", None)
                        if whatsapp_key:
                            client = WhatsAppClient(whatsapp_key)
                            frontend_url = getattr(settings, "FRONTEND_URL", "https://suoops.com")
                            verify_link = f"{frontend_url.rstrip('/')}/dashboard/invoices/{invoice.invoice_id}"
                            whatsapp_message = (
                                f"💰 Payment Notification!\n\n"
                                f"Customer reported a bank transfer for:\n\n"
                                f"📄 Invoice: {invoice.invoice_id}\n"
                                f"💵 Amount: ₦{invoice.amount:,.2f}\n"
                            )
                            if invoice.customer:
                                whatsapp_message += f"👤 Customer: {invoice.customer.name}\n"
                            whatsapp_message += (
                                f"\n🔗 Verify & Mark as Paid:\n{verify_link}\n\n"
                                f"✅ Please verify the funds in your bank account "
                                f"and mark the invoice as PAID to send the customer their receipt.\n\n"
                                f"💡 _Tip: If link doesn't load, long-press and select 'Open in Browser'_"
                            )
                            client.send_text(user.phone, whatsapp_message)
                            results["whatsapp"] = True
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Failed WhatsApp notify business %s: %s", invoice.invoice_id, exc)
                logger.info(
                    "Business notification for invoice %s - Email: %s, WhatsApp: %s",
                    invoice.invoice_id,
                    results["email"],
                    results["whatsapp"],
                )

            run_async(_run())
        except Exception as exc:  # noqa: BLE001
            logger.error("Notification dispatch failed for invoice %s: %s", invoice.invoice_id, exc)

    def _notify_business_with_receipt(self, invoice: models.Invoice) -> None:
        """
        Notify the business owner with the receipt PDF via WhatsApp.

        This is called when an invoice is marked as paid but has no customer
        contact info (no email/phone). In this case, the business needs the
        receipt PDF to give to the customer manually.
        """
        if not invoice.issuer:
            logger.warning("No issuer found for invoice %s", invoice.invoice_id)
            return

        user = invoice.issuer
        if not user.phone:
            logger.warning("Business %s has no phone for receipt notification", user.id)
            return

        try:
            from app.bot.whatsapp_client import WhatsAppClient
            from app.core.config import settings

            whatsapp_key = getattr(settings, "WHATSAPP_API_KEY", None)
            if not whatsapp_key:
                logger.warning("No WhatsApp API key configured for receipt notification")
                return

            client = WhatsAppClient(whatsapp_key)

            customer_name = invoice.customer.name if invoice.customer else "Customer"

            receipt_message = (
                f"✅ Invoice Paid!\n\n"
                f"📄 Invoice: {invoice.invoice_id}\n"
                f"💵 Amount: ₦{invoice.amount:,.2f}\n"
                f"👤 Customer: {customer_name}\n\n"
                f"Share this receipt with your customer."
            )

            client.send_text(user.phone, receipt_message)

            # Send receipt PDF as document (better UX than URL link)
            if invoice.pdf_url and invoice.pdf_url.startswith("http"):
                client.send_document(
                    user.phone,
                    invoice.pdf_url,
                    f"Receipt_{invoice.invoice_id}.pdf",
                    f"🧾 Receipt for {customer_name} - ₦{invoice.amount:,.2f}",
                )

            logger.info("Receipt PDF sent to business %s for invoice %s", user.id, invoice.invoice_id)

        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send receipt to business %s: %s", invoice.invoice_id, exc)

    def _process_inventory_on_payment(self, invoice: models.Invoice) -> None:
        """
        Process inventory deduction when a revenue invoice is marked as paid.

        This is the key automation point:
        - Deducts stock for all line items linked to products
        - Records stock movements for audit trail
        - Updates COGS for tax reporting

        For expense invoices, inventory is added at creation time (purchases).
        For revenue invoices, inventory is deducted at payment time (sales).
        """
        if invoice.invoice_type != "revenue":
            return  # Only process revenue invoices on payment

        try:
            from decimal import Decimal

            from app.services.inventory import build_inventory_service

            # Check if any lines have products linked
            has_inventory_items = any(line.product_id for line in invoice.lines)
            if not has_inventory_items:
                return

            inventory_service = build_inventory_service(self.db, invoice.issuer_id)

            # Process each line item
            for line in invoice.lines:
                if not line.product_id:
                    continue

                try:
                    inventory_service.record_sale(
                        product_id=line.product_id,
                        quantity=line.quantity,
                        unit_price=Decimal(str(line.unit_price)),
                        invoice_line_id=line.id,
                        reference_id=invoice.invoice_id,
                    )
                    logger.info(
                        f"Stock deducted for product {line.product_id}: "
                        f"{line.quantity} units via invoice {invoice.invoice_id}"
                    )
                except ValueError as e:
                    # Log insufficient stock but don't block payment
                    logger.warning(
                        "Insufficient stock for product %s on invoice %s: %s",
                        line.product_id,
                        invoice.invoice_id,
                        e,
                    )

        except Exception as e:
            logger.error("Inventory processing failed for invoice %s: %s", invoice.invoice_id, e)

    def _check_and_send_low_stock_alerts(self, invoice: models.Invoice) -> None:
        """
        Check for low stock items after a sale and send alerts.

        This implements the alert workflow:
        - After inventory is deducted, check all affected products
        - If any product is at or below reorder level, send alert
        - Optionally generate draft purchase order suggestions
        """
        if invoice.invoice_type != "revenue":
            return

        # Capture values upfront to avoid DB access after potential errors
        invoice_id = invoice.invoice_id
        issuer_id = invoice.issuer_id

        try:
            from app.models.inventory_models import Product

            # Get products that were just affected
            affected_product_ids = [line.product_id for line in invoice.lines if line.product_id]

            if not affected_product_ids:
                return

            # Check which products are now low stock
            low_stock_products = (
                self.db.query(Product)
                .filter(
                    Product.id.in_(affected_product_ids),
                    Product.track_stock.is_(True),
                    Product.quantity_in_stock <= Product.reorder_level,
                )
                .all()
            )

            if not low_stock_products:
                return

            # Build alert message
            alert_items = []
            for product in low_stock_products:
                status = "OUT OF STOCK" if product.quantity_in_stock <= 0 else "LOW STOCK"
                alert_items.append(
                    f"• {product.name} ({product.sku}): {product.quantity_in_stock} {product.unit} "
                    f"[Reorder: {product.reorder_quantity}] - {status}"
                )

            # Generate draft purchase order for low stock products
            # Note: This may fail if purchase_order table doesn't exist yet - that's OK
            purchase_order = None
            try:
                from app.services.inventory import build_inventory_service

                inventory_service = build_inventory_service(self.db, issuer_id)
                product_ids = [p.id for p in low_stock_products]
                purchase_order = inventory_service.generate_draft_purchase_order(
                    product_ids=product_ids,
                    trigger_invoice_id=invoice_id,
                )
                if purchase_order:
                    logger.info(
                        "Draft purchase order %s generated for %s low stock products",
                        purchase_order.order_number,
                        len(low_stock_products),
                    )
            except Exception as e:
                # Rollback to clear any pending rollback state from failed PO creation
                self.db.rollback()
                logger.warning("Could not generate purchase order: %s", e)

            po_message = ""
            if purchase_order:
                po_message = f"\n\n📋 Draft Purchase Order #{purchase_order.id} has been created for your review."

            message = (
                f"⚠️ Stock Alert after Invoice {invoice_id}\n\n"
                f"The following products need reordering:\n\n"
                + "\n".join(alert_items)
                + po_message
                + "\n\nPlease review and place orders with suppliers."
            )

            # Send notification to business owner
            po_id = purchase_order.id if purchase_order else None
            self._send_low_stock_notification(issuer_id, message, low_stock_products, po_id)

            logger.info(
                "Low stock alert sent for %s products after invoice %s",
                len(low_stock_products),
                invoice_id,
            )

        except Exception as e:
            # Rollback to prevent cascading errors
            try:
                self.db.rollback()
            except Exception:
                pass
            logger.error("Low stock check failed for invoice %s: %s", invoice_id, e)

    def _send_low_stock_notification(
        self, user_id: int, message: str, products: list, purchase_order_id: int | None = None
    ) -> None:
        """Send low stock alert to business owner via email and WhatsApp."""
        try:
            user = self.db.query(models.User).filter(models.User.id == user_id).one_or_none()
            if not user:
                return

            from app.services.notification.service import NotificationService

            service = NotificationService()

            po_suffix = f" - PO #{purchase_order_id}" if purchase_order_id else ""

            async def _run():
                if user.email:
                    try:
                        await service.send_email(
                            to_email=user.email,
                            subject=f"⚠️ Low Stock Alert - {len(products)} products need reordering{po_suffix}",
                            body=message,
                        )
                    except Exception as exc:
                        logger.error("Failed to send low stock email: %s", exc)

            run_async(_run())

            # Also send WhatsApp template if configured (one per product)
            from app.core.config import settings as _settings

            template_name = _settings.WHATSAPP_TEMPLATE_LOW_STOCK_ALERT
            if template_name and user.phone:
                try:
                    from app.bot.whatsapp_client import WhatsAppClient
                    from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

                    client = WhatsAppClient(_settings.WHATSAPP_API_KEY)
                    lang = _settings.WHATSAPP_TEMPLATE_LANGUAGE or "en"
                    for product in products:
                        if not can_send_whatsapp(priority=False):
                            break
                        product_name = product.name or "Unknown product"
                        qty = str(product.quantity_in_stock)
                        reorder_level = str(product.reorder_level)
                        components = [
                            {
                                "type": "body",
                                "parameters": [
                                    {"type": "text", "text": product_name},
                                    {"type": "text", "text": qty},
                                    {"type": "text", "text": reorder_level},
                                ],
                            }
                        ]
                        if client.send_template(user.phone, template_name, lang, components):
                            record_whatsapp_send(priority=False)
                except Exception as exc:
                    logger.error("Failed to send low stock WhatsApp: %s", exc)

        except Exception as e:
            logger.error("Low stock notification failed: %s", e)
