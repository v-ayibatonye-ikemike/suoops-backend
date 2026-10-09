from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.bot.conversation_window import is_window_open
from app.models import models
from app.models.ai_models import AICollectionDraft
from app.services.notification.service import NotificationService

from .gateway import AIGateway
from .types import AIMessage, AIRequest

COLLECTION_COOLDOWN_DAYS = 3


class CollectionConflictError(ValueError):
    pass


class CollectionDeliveryError(RuntimeError):
    pass


class ReminderStyleOut(BaseModel):
    opening: str = Field(min_length=1, max_length=220)
    closing: str = Field(min_length=1, max_length=220)


class CollectionsAssistantService:
    def __init__(
        self,
        db: Session,
        *,
        gateway: AIGateway | None = None,
        notification_service: NotificationService | None = None,
    ) -> None:
        self._db = db
        self._gateway = gateway or AIGateway(db)
        self._notifications = notification_service or NotificationService()

    def priorities(self, *, actor_user_id: int, data_owner_id: int, limit: int = 10) -> dict:
        now = dt.datetime.now(dt.timezone.utc)
        cutoff = now - dt.timedelta(days=COLLECTION_COOLDOWN_DAYS)
        recent_reminder_invoice_ids = select(models.InvoiceReminderLog.invoice_id).where(
            models.InvoiceReminderLog.sent_at >= cutoff
        )
        closed_draft_invoice_ids = select(AICollectionDraft.invoice_id).where(
            AICollectionDraft.data_owner_id == data_owner_id,
            AICollectionDraft.dedupe_key.like(f"{now.date().isoformat()}:%"),
            AICollectionDraft.status.in_(("sent", "dismissed")),
        )
        invoices = (
            self._db.query(models.Invoice)
            .join(models.Customer, models.Customer.id == models.Invoice.customer_id)
            .filter(
                models.Invoice.issuer_id == data_owner_id,
                models.Invoice.invoice_type == "revenue",
                models.Invoice.status == "pending",
                models.Invoice.due_date.isnot(None),
                models.Invoice.due_date < now.replace(hour=0, minute=0, second=0, microsecond=0),
                or_(models.Invoice.channel.is_(None), models.Invoice.channel != "storefront"),
                ~models.Invoice.id.in_(recent_reminder_invoice_ids),
                ~models.Invoice.id.in_(closed_draft_invoice_ids),
            )
            .all()
        )
        ranked = sorted(
            ((invoice, *self._score_invoice(invoice)) for invoice in invoices),
            key=lambda row: (-row[1], -float(row[0].amount)),
        )[:limit]
        drafts = [
            self._upsert_draft(
                invoice,
                score=score,
                level=level,
                reason_codes=reason_codes,
                explanation=explanation,
                actor_user_id=actor_user_id,
                data_owner_id=data_owner_id,
            )
            for invoice, score, level, reason_codes, explanation in ranked
        ]
        return {
            "generated_at": now,
            "cooldown_days": COLLECTION_COOLDOWN_DAYS,
            "drafts": [self._draft_out(draft) for draft in drafts if draft.status in ("draft", "failed")],
            "total_overdue_amount": sum(float(invoice.amount) for invoice, *_ in ranked),
            "eligible_count": len(ranked),
        }

    def get_draft(self, public_id: str, data_owner_id: int, *, lock: bool = False) -> AICollectionDraft:
        query = self._db.query(AICollectionDraft).filter(
            AICollectionDraft.public_id == public_id,
            AICollectionDraft.data_owner_id == data_owner_id,
        )
        if lock:
            query = query.with_for_update()
        draft = cast(AICollectionDraft | None, query.one_or_none())
        if not draft:
            raise LookupError("Collection draft not found")
        return draft

    async def enhance_draft(
        self,
        public_id: str,
        *,
        actor_user_id: int,
        data_owner_id: int,
    ) -> dict:
        draft = self.get_draft(public_id, data_owner_id)
        if draft.status != "draft":
            raise CollectionConflictError(f"Draft is already {draft.status}")
        invoice = self._invoice_for_draft(draft)
        days_overdue = self._days_overdue(invoice)
        tone = self._tone(days_overdue)
        style = await self._gateway.generate_structured(
            AIRequest(
                feature="collection_reminder_draft",
                prompt_version="collection-reminder-v1",
                messages=[
                    AIMessage(
                        role="system",
                        content=(
                            "Write only an opening and closing for a respectful Nigerian SME payment reminder. "
                            "Do not include names, amounts, invoice numbers, bank details, links, threats, fees, "
                            "legal claims, or facts not provided. Return JSON with opening and closing."
                        ),
                    ),
                    AIMessage(
                        role="user",
                        content=(
                            f"Tone: {tone}. Days overdue: {days_overdue}. "
                            f"Customer previously paid {self._customer_payment_rate(invoice.customer_id):.0f}% "
                            "of their invoices."
                        ),
                    ),
                ],
                metadata={"invoice_id": invoice.invoice_id, "tone": tone},
            ),
            ReminderStyleOut,
            actor_user_id=actor_user_id,
            data_owner_id=data_owner_id,
        )
        draft.message = self._assemble_message(invoice, style.opening, style.closing)
        draft.ai_generated = True
        self._db.commit()
        self._db.refresh(draft)
        return self._draft_out(draft)

    def update_draft(
        self,
        public_id: str,
        *,
        data_owner_id: int,
        subject: str | None,
        message: str,
    ) -> dict:
        draft = self.get_draft(public_id, data_owner_id, lock=True)
        if draft.status not in ("draft", "failed"):
            raise CollectionConflictError(f"Draft is already {draft.status}")
        draft.subject = subject.strip() if subject else None
        draft.message = message.strip()
        draft.status = "draft"
        draft.failure_reason = None
        self._db.commit()
        self._db.refresh(draft)
        return self._draft_out(draft)

    async def send_draft(
        self,
        public_id: str,
        *,
        actor_user_id: int,
        data_owner_id: int,
        subject: str | None,
        message: str,
    ) -> dict:
        draft = self.get_draft(public_id, data_owner_id, lock=True)
        if draft.status != "draft":
            raise CollectionConflictError(f"Draft is already {draft.status}")
        invoice = self._invoice_for_draft(draft)
        now = dt.datetime.now(dt.timezone.utc)
        if invoice.status != "pending" or not invoice.due_date or invoice.due_date.date() >= now.date():
            raise CollectionConflictError("Invoice is no longer overdue")
        recent = (
            self._db.query(models.InvoiceReminderLog.id)
            .filter(
                models.InvoiceReminderLog.invoice_id == invoice.id,
                models.InvoiceReminderLog.sent_at >= now - dt.timedelta(days=COLLECTION_COOLDOWN_DAYS),
            )
            .first()
        )
        if recent:
            raise CollectionConflictError(
                f"A reminder was already sent within the last {COLLECTION_COOLDOWN_DAYS} days"
            )

        draft.subject = subject.strip() if subject else draft.subject
        draft.message = message.strip()
        delivered, recipient = await self._deliver(draft, invoice)
        if not delivered:
            draft.status = "failed"
            draft.failure_reason = f"{draft.channel} delivery failed"
            self._db.commit()
            raise CollectionDeliveryError(draft.failure_reason)

        draft.status = "sent"
        draft.sent_at = now
        draft.failure_reason = None
        self._db.add(
            models.InvoiceReminderLog(
                invoice_id=invoice.id,
                reminder_type=f"ai_collection_{now:%Y%m%d}",
                channel=draft.channel,
                recipient=recipient,
            )
        )
        self._db.commit()
        self._db.refresh(draft)
        return self._draft_out(draft)

    def dismiss_draft(self, public_id: str, *, data_owner_id: int) -> dict:
        draft = self.get_draft(public_id, data_owner_id, lock=True)
        if draft.status not in ("draft", "failed"):
            raise CollectionConflictError(f"Draft is already {draft.status}")
        draft.status = "dismissed"
        draft.dismissed_at = dt.datetime.now(dt.timezone.utc)
        self._db.commit()
        self._db.refresh(draft)
        return self._draft_out(draft)

    def metrics(self, data_owner_id: int) -> dict:
        sent_count = (
            self._db.query(func.count(AICollectionDraft.id))
            .filter(
                AICollectionDraft.data_owner_id == data_owner_id,
                AICollectionDraft.status == "sent",
            )
            .scalar()
            or 0
        )
        first_send = (
            self._db.query(
                AICollectionDraft.invoice_id.label("invoice_id"),
                func.min(AICollectionDraft.sent_at).label("first_sent_at"),
            )
            .filter(
                AICollectionDraft.data_owner_id == data_owner_id,
                AICollectionDraft.status == "sent",
                AICollectionDraft.sent_at.isnot(None),
            )
            .group_by(AICollectionDraft.invoice_id)
            .subquery()
        )
        recovered = (
            self._db.query(
                func.count(models.Invoice.id).label("count"),
                func.coalesce(func.sum(models.Invoice.amount), Decimal("0")).label("amount"),
            )
            .join(first_send, first_send.c.invoice_id == models.Invoice.id)
            .filter(
                models.Invoice.status == "paid",
                models.Invoice.paid_at.isnot(None),
                models.Invoice.paid_at >= first_send.c.first_sent_at,
            )
            .first()
        )
        recovered_count = int(recovered.count or 0)
        return {
            "sent_reminders": int(sent_count),
            "recovered_invoices": recovered_count,
            "recovered_amount": float(recovered.amount or 0),
            "recovery_rate": round(recovered_count / sent_count * 100, 1) if sent_count else 0.0,
        }

    def _score_invoice(self, invoice: models.Invoice) -> tuple[int, str, list[str], str]:
        days = self._days_overdue(invoice)
        amount = float(invoice.amount)
        payment_rate = self._customer_payment_rate(invoice.customer_id)
        days_points = min(40, max(5, round(days / 30 * 40)))
        amount_points = 30 if amount >= 500_000 else 20 if amount >= 100_000 else 10 if amount >= 25_000 else 5
        history_points = 20 if payment_rate >= 75 else 12 if payment_rate >= 40 else 5
        contact_points = 10 if self._available_channel(invoice) != "unavailable" else 0
        score = min(100, days_points + amount_points + history_points + contact_points)
        level = "critical" if score >= 80 else "high" if score >= 60 else "medium" if score >= 40 else "low"
        reasons = [f"{days}_days_overdue", f"amount_{self._amount_band(amount)}", f"payment_rate_{round(payment_rate)}"]
        if contact_points:
            reasons.append("contact_channel_available")
        explanation = (
            f"Score {score}/100: {days} days overdue, ₦{amount:,.0f} outstanding, "
            f"{payment_rate:.0f}% historical payment rate"
            + (", and a permitted contact channel is available." if contact_points else ".")
        )
        return score, level, reasons, explanation

    def _upsert_draft(
        self,
        invoice: models.Invoice,
        *,
        score: int,
        level: str,
        reason_codes: list[str],
        explanation: str,
        actor_user_id: int,
        data_owner_id: int,
    ) -> AICollectionDraft:
        dedupe_key = f"{dt.datetime.now(dt.timezone.utc).date().isoformat()}:{invoice.id}"
        existing = cast(
            AICollectionDraft | None,
            self._db.query(AICollectionDraft)
            .filter(
                AICollectionDraft.data_owner_id == data_owner_id,
                AICollectionDraft.dedupe_key == dedupe_key,
            )
            .one_or_none(),
        )
        if existing:
            return existing
        channel = self._available_channel(invoice)
        subject = f"Payment reminder for invoice {invoice.invoice_id}" if channel == "email" else None
        opening, closing = self._deterministic_style(invoice)
        draft = AICollectionDraft(
            public_id=str(uuid.uuid4()),
            data_owner_id=data_owner_id,
            created_by_user_id=actor_user_id,
            invoice_id=invoice.id,
            channel=channel,
            recipient_masked=self._masked_recipient(invoice, channel),
            subject=subject,
            message=self._assemble_message(invoice, opening, closing),
            priority_score=score,
            priority_level=level,
            reason_codes=reason_codes,
            explanation=explanation,
            ai_generated=False,
            dedupe_key=dedupe_key,
            status="draft",
        )
        self._db.add(draft)
        self._db.commit()
        self._db.refresh(draft)
        return draft

    async def _deliver(self, draft: AICollectionDraft, invoice: models.Invoice) -> tuple[bool, str]:
        customer = invoice.customer
        if draft.channel == "email" and customer.email:
            delivered = await self._notifications.send_email(
                customer.email,
                draft.subject or f"Payment reminder for invoice {invoice.invoice_id}",
                draft.message,
            )
            return delivered, customer.email
        if draft.channel == "whatsapp" and customer.phone:
            if not customer.whatsapp_opted_in or not is_window_open(customer.phone):
                raise CollectionConflictError("Customer WhatsApp conversation window is no longer open")
            from app.core.whatsapp import get_whatsapp_client
            from app.utils.whatsapp_budget import can_send_whatsapp, record_whatsapp_send

            if not can_send_whatsapp(priority=True):
                raise CollectionDeliveryError("WhatsApp reminder budget is currently unavailable")
            delivered = get_whatsapp_client().send_text(customer.phone, draft.message)
            if delivered:
                record_whatsapp_send(priority=True)
            return delivered, customer.phone
        raise CollectionConflictError("No permitted delivery channel is available")

    def _draft_out(self, draft: AICollectionDraft) -> dict:
        invoice = self._invoice_for_draft(draft)
        return {
            "id": draft.public_id,
            "invoice_id": invoice.invoice_id,
            "customer_name": invoice.customer.name,
            "amount": float(invoice.amount),
            "currency": invoice.currency,
            "days_overdue": self._days_overdue(invoice),
            "channel": draft.channel,
            "recipient_masked": draft.recipient_masked,
            "subject": draft.subject,
            "message": draft.message,
            "priority_score": draft.priority_score,
            "priority_level": draft.priority_level,
            "reasons": list(draft.reason_codes or []),
            "explanation": draft.explanation,
            "ai_generated": draft.ai_generated,
            "status": draft.status,
            "created_at": draft.created_at,
            "sent_at": draft.sent_at,
            "can_send": draft.status == "draft" and draft.channel != "unavailable",
        }

    def _invoice_for_draft(self, draft: AICollectionDraft) -> models.Invoice:
        invoice = (
            self._db.query(models.Invoice)
            .filter(
                models.Invoice.id == draft.invoice_id,
                models.Invoice.issuer_id == draft.data_owner_id,
            )
            .one_or_none()
        )
        if not invoice:
            raise LookupError("Invoice for collection draft not found")
        return cast(models.Invoice, invoice)

    def _customer_payment_rate(self, customer_id: int) -> float:
        total, paid = (
            self._db.query(
                func.count(models.Invoice.id),
                func.sum(case((models.Invoice.status == "paid", 1), else_=0)),
            )
            .filter(
                models.Invoice.customer_id == customer_id,
                models.Invoice.invoice_type == "revenue",
            )
            .one()
        )
        return float(paid or 0) / int(total or 1) * 100

    @staticmethod
    def _days_overdue(invoice: models.Invoice) -> int:
        if not invoice.due_date:
            return 0
        return max(0, int((dt.datetime.now(dt.timezone.utc).date() - invoice.due_date.date()).days))

    def _available_channel(self, invoice: models.Invoice) -> str:
        customer = invoice.customer
        if customer.phone and customer.whatsapp_opted_in and is_window_open(customer.phone):
            return "whatsapp"
        if customer.email:
            return "email"
        return "unavailable"

    @staticmethod
    def _masked_recipient(invoice: models.Invoice, channel: str) -> str:
        customer = invoice.customer
        if channel == "email" and customer.email:
            local, domain = customer.email.split("@", 1)
            return f"{local[:1]}***@{domain}"
        if channel == "whatsapp" and customer.phone:
            return f"***{customer.phone[-4:]}"
        return "No permitted contact channel"

    def _deterministic_style(self, invoice: models.Invoice) -> tuple[str, str]:
        days = self._days_overdue(invoice)
        if days <= 3:
            return (
                "I hope you are doing well. This is a friendly reminder about the invoice below.",
                "If you have already paid, please disregard this message. Thank you.",
            )
        if days <= 13:
            return (
                "I am following up on the overdue invoice below.",
                "Please let us know if you need the payment details resent. Thank you.",
            )
        return (
            "This is an important follow-up regarding the overdue invoice below.",
            "Please reply with an expected payment date or let us know if there is an issue we should resolve.",
        )

    def _assemble_message(self, invoice: models.Invoice, opening: str, closing: str) -> str:
        customer_name = (invoice.customer.name or "there").split()[0]
        business_name = invoice.issuer.business_name or invoice.issuer.name or "the business"
        due_date = invoice.due_date.strftime("%d %b %Y") if invoice.due_date else "the agreed date"
        return (
            f"Hi {customer_name},\n\n{opening}\n\n"
            f"Invoice: {invoice.invoice_id}\n"
            f"Amount: {invoice.currency} {float(invoice.amount):,.2f}\n"
            f"Due date: {due_date}\n\n"
            f"{closing}\n\n— {business_name}"
        )

    @staticmethod
    def _tone(days_overdue: int) -> str:
        return "gentle" if days_overdue <= 3 else "professional" if days_overdue <= 13 else "firm but respectful"

    @staticmethod
    def _amount_band(amount: float) -> str:
        if amount >= 500_000:
            return "very_high"
        if amount >= 100_000:
            return "high"
        if amount >= 25_000:
            return "medium"
        return "low"
