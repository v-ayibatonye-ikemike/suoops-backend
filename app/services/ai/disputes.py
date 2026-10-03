from __future__ import annotations

import datetime as dt
import json
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import models
from app.services.escrow_service import _norm_phone

from .gateway import AIGateway, AIGatewayError
from .types import AIMessage, AIRequest


class DisputeNotFoundError(LookupError):
    pass


class DisputeNarrativeOut(BaseModel):
    neutral_summary: str = Field(min_length=20, max_length=700)
    reviewer_questions: list[str] = Field(default_factory=list, max_length=5)


class DisputeAssistantService:
    def __init__(self, db: Session, *, gateway: AIGateway | None = None) -> None:
        self._db = db
        self._gateway = gateway or AIGateway(db)

    async def analyse(self, escrow_id: int, *, admin_user_id: int) -> dict:
        row = (
            self._db.query(
                models.StorefrontOrderEscrow,
                models.Invoice,
                models.User,
                models.Customer,
            )
            .join(models.Invoice, models.StorefrontOrderEscrow.invoice_id == models.Invoice.id)
            .join(models.User, models.StorefrontOrderEscrow.seller_id == models.User.id)
            .outerjoin(models.Customer, models.Invoice.customer_id == models.Customer.id)
            .filter(models.StorefrontOrderEscrow.id == escrow_id)
            .first()
        )
        if not row:
            raise DisputeNotFoundError("Dispute not found")
        escrow, invoice, seller, customer = row
        messages = (
            self._db.query(models.OrderMessage)
            .filter(models.OrderMessage.escrow_id == escrow.id)
            .order_by(models.OrderMessage.created_at.asc(), models.OrderMessage.id.asc())
            .all()
        )
        reputation = None
        if customer and customer.phone:
            reputation = (
                self._db.query(models.BuyerReputation)
                .filter(models.BuyerReputation.phone == _norm_phone(customer.phone))
                .first()
            )

        timeline = self._timeline(escrow, invoice)
        evidence = self._evidence(escrow, invoice, seller, messages, reputation)
        missing = self._missing_evidence(escrow, invoice, messages)
        flags = self._review_flags(escrow, seller, messages, reputation)
        summary = self._deterministic_summary(escrow, invoice, evidence, missing, flags)
        questions = self._deterministic_questions(escrow, missing, flags)
        ai_generated = False
        notice: str | None = None

        if settings.AI_DISPUTE_ASSISTANT_ENABLED:
            try:
                narrative = await self._gateway.generate_structured(
                    AIRequest(
                        feature="dispute_evidence_summary",
                        prompt_version="dispute-summary-v1",
                        messages=[
                            AIMessage(
                                role="system",
                                content=(
                                    "Write a neutral evidence summary and up to five reviewer questions "
                                    "using only the supplied records. Distinguish recorded facts from party "
                                    "claims. Do not decide credibility, recommend refund or release, assign "
                                    "fault, interpret law, or invent missing evidence. Return JSON with "
                                    "neutral_summary and reviewer_questions."
                                ),
                            ),
                            AIMessage(
                                role="user",
                                content=json.dumps(
                                    {
                                        "status": escrow.status,
                                        "amount_naira": round((escrow.gross_kobo or 0) / 100, 2),
                                        "buyer_claim": escrow.dispute_reason,
                                        "timeline": timeline,
                                        "evidence": self._minimised_ai_evidence(evidence),
                                        "missing_evidence": missing,
                                        "review_flags": flags,
                                        "messages": [
                                            {
                                                "sender": message.sender_role,
                                                "body": message.body_redacted[:500],
                                                "blocked": bool(message.blocked),
                                                "flag_reasons": message.flag_reasons,
                                            }
                                            for message in messages[-20:]
                                        ],
                                    },
                                    default=str,
                                    separators=(",", ":"),
                                ),
                            ),
                        ],
                        max_tokens=650,
                        temperature=0,
                        metadata={"escrow_id": escrow.id, "seller_id": seller.id},
                    ),
                    DisputeNarrativeOut,
                    actor_admin_user_id=admin_user_id,
                    data_owner_id=seller.id,
                    enforce_owner_quota=False,
                )
                generated = cast(DisputeNarrativeOut, narrative)
                if self._is_neutral(generated):
                    summary = generated.neutral_summary
                    questions = generated.reviewer_questions
                    ai_generated = True
                else:
                    notice = (
                        "AI summary was rejected because it suggested an outcome or assigned fault; "
                        "showing deterministic evidence review."
                    )
            except AIGatewayError as exc:
                notice = f"AI summary unavailable ({exc.code}); showing deterministic evidence review."

        return {
            "escrow_id": escrow.id,
            "invoice_id": invoice.invoice_id,
            "status": escrow.status,
            "amount_naira": round((escrow.gross_kobo or 0) / 100, 2),
            "neutral_summary": summary,
            "timeline": timeline,
            "evidence": evidence,
            "missing_evidence": missing,
            "review_flags": flags,
            "reviewer_questions": questions,
            "ai_generated": ai_generated,
            "generation_notice": notice,
            "decision_notice": (
                "This assistant organises evidence only. A human administrator must independently "
                "decide and confirm any refund, release, suspension or card block."
            ),
        }

    @staticmethod
    def _timeline(
        escrow: models.StorefrontOrderEscrow,
        invoice: models.Invoice,
    ) -> list[dict[str, object]]:
        events: list[dict[str, object]] = []

        def add(when: dt.datetime | None, event: str, detail: str, source: str) -> None:
            if when:
                events.append({"occurred_at": when, "event": event, "detail": detail, "source": source})

        add(escrow.created_at, "Order created", "Escrow record created for the storefront order.", "order")
        add(invoice.paid_at, "Payment confirmed", "Invoice payment was recorded as paid.", "payment")
        add(
            escrow.seller_dispatched_at,
            "Seller marked dispatched",
            DisputeAssistantService._dispatch_detail(escrow),
            "seller",
        )
        add(
            escrow.delivery_status_at,
            "Courier status updated",
            f"Courier status recorded as {escrow.delivery_status or 'unknown'}.",
            "courier",
        )
        add(
            escrow.courier_delivered_at,
            "Courier marked delivered",
            "Integrated courier reported the shipment delivered.",
            "courier",
        )
        add(
            escrow.seller_marked_delivered_at,
            "Seller marked delivered",
            escrow.delivery_proof_note or "Seller recorded delivery without a note.",
            "seller",
        )
        add(escrow.disputed_at, "Buyer reported a problem", escrow.dispute_reason or "No reason recorded.", "buyer")
        add(escrow.confirmed_at, "Buyer confirmed receipt", "Buyer confirmation was recorded.", "buyer")
        add(escrow.refunded_at, "Refund recorded", "Escrow was refunded to the buyer.", "payment")
        add(escrow.released_at, "Release recorded", "Escrow was released for seller payout.", "payment")
        return sorted(
            events,
            key=lambda item: DisputeAssistantService._utc(cast(dt.datetime, item["occurred_at"])),
        )

    @staticmethod
    def _dispatch_detail(escrow: models.StorefrontOrderEscrow) -> str:
        details = ["Seller recorded dispatch"]
        if escrow.dispatch_carrier:
            details.append(f"via {escrow.dispatch_carrier}")
        if escrow.dispatch_tracking:
            details.append(f"with tracking {escrow.dispatch_tracking}")
        return " ".join(details) + "."

    @staticmethod
    def _evidence(
        escrow: models.StorefrontOrderEscrow,
        invoice: models.Invoice,
        seller: models.User,
        messages: list[models.OrderMessage],
        reputation: models.BuyerReputation | None,
    ) -> list[dict[str, str]]:
        evidence: list[dict[str, str]] = []

        def add(label: str, detail: str | None, source: str) -> None:
            if detail:
                evidence.append({"label": label, "detail": detail, "source": source})

        add("Buyer report", escrow.dispute_reason, "buyer")
        add("Order contents", DisputeAssistantService._line_summary(invoice.lines), "invoice")
        add("Delivery destination", invoice.notes, "order")
        if escrow.seller_dispatched_at:
            add("Dispatch record", DisputeAssistantService._dispatch_detail(escrow), "seller")
        add("Dispatch note", escrow.dispatch_note, "seller")
        if escrow.dispatch_proof_url:
            add("Dispatch photo", "A seller dispatch photo is attached.", "seller")
        add("Delivery proof note", escrow.delivery_proof_note, "seller")
        if escrow.delivery_proof_url:
            add("Delivery photo", "A seller delivery photo is attached.", "seller")
        if escrow.delivery_status:
            add("Courier status", escrow.delivery_status, "courier")
        if escrow.shipbubble_tracking_url:
            add("Courier tracking", "An integrated courier tracking record is attached.", "courier")
        if escrow.review_reason:
            add("Automated review hold", escrow.review_reason, "risk controls")
        if messages:
            flagged = sum(bool(message.flagged) for message in messages)
            blocked = sum(bool(message.blocked) for message in messages)
            add(
                "Order messages",
                f"{len(messages)} recorded; {flagged} flagged and {blocked} blocked.",
                "order messaging",
            )
        if reputation:
            add(
                "Buyer dispute history",
                (
                    f"{reputation.disputes} prior/current reports, "
                    f"{reputation.false_disputes} previously ruled false; "
                    f"flagged={bool(reputation.flagged)}."
                ),
                "reputation",
            )
        if seller.circumvention_attempts:
            add(
                "Seller messaging history",
                f"{seller.circumvention_attempts} off-platform messaging attempts recorded.",
                "risk controls",
            )
        return evidence

    @staticmethod
    def _line_summary(lines: list[models.InvoiceLine]) -> str:
        if not lines:
            return "No invoice line items are recorded."
        return "; ".join(f"{line.quantity} × {line.description}" for line in lines[:20])

    @staticmethod
    def _missing_evidence(
        escrow: models.StorefrontOrderEscrow,
        invoice: models.Invoice,
        messages: list[models.OrderMessage],
    ) -> list[str]:
        missing: list[str] = []
        if not invoice.paid_at:
            missing.append("No confirmed payment timestamp is recorded.")
        if not escrow.dispute_reason:
            missing.append("No buyer dispute reason is recorded.")
        if escrow.requires_delivery:
            if not escrow.seller_dispatched_at:
                missing.append("The seller has not recorded dispatch.")
            if not escrow.dispatch_tracking and not escrow.dispatch_proof_url:
                missing.append("No seller tracking reference or dispatch photo is recorded.")
            if not escrow.courier_delivered_at and not escrow.seller_marked_delivered_at:
                missing.append("No courier-delivered event or seller delivery proof is recorded.")
            if not invoice.notes:
                missing.append("No delivery destination or landmark is recorded on the invoice.")
        elif not escrow.seller_marked_delivered_at and not escrow.confirmed_at:
            missing.append("No seller completion record or buyer confirmation is recorded for this service order.")
        if not messages:
            missing.append("No order-message history is available for delivery coordination.")
        return missing

    @staticmethod
    def _review_flags(
        escrow: models.StorefrontOrderEscrow,
        seller: models.User,
        messages: list[models.OrderMessage],
        reputation: models.BuyerReputation | None,
    ) -> list[str]:
        flags: list[str] = []
        reason = (escrow.dispute_reason or "").lower()
        if escrow.courier_delivered_at and any(
            term in reason for term in ("not delivered", "never arrived", "not arrive")
        ):
            flags.append("Buyer non-delivery claim conflicts with an integrated courier-delivered event.")
        if (
            escrow.seller_marked_delivered_at
            and escrow.seller_dispatched_at
            and DisputeAssistantService._utc(escrow.seller_marked_delivered_at)
            < DisputeAssistantService._utc(escrow.seller_dispatched_at)
        ):
            flags.append("Seller delivery timestamp precedes the recorded dispatch timestamp.")
        if (
            escrow.disputed_at
            and escrow.seller_dispatched_at
            and DisputeAssistantService._utc(escrow.seller_dispatched_at)
            > DisputeAssistantService._utc(escrow.disputed_at)
        ):
            flags.append("Seller recorded dispatch after the buyer opened the dispute.")
        if (
            escrow.disputed_at
            and escrow.seller_marked_delivered_at
            and DisputeAssistantService._utc(escrow.seller_marked_delivered_at)
            > DisputeAssistantService._utc(escrow.disputed_at)
        ):
            flags.append("Seller submitted delivery evidence after the buyer opened the dispute.")
        blocked = sum(message.blocked for message in messages if message.sender_role == "seller")
        if blocked:
            flags.append(f"{blocked} seller message(s) were blocked by off-platform safety controls.")
        if escrow.held_for_review and escrow.review_reason:
            flags.append(f"Order was already held by risk controls: {escrow.review_reason}.")
        if reputation and reputation.flagged:
            flags.append("Buyer reputation is flagged; review the underlying history without treating it as proof.")
        if seller.flagged_for_review:
            flags.append("Seller account is flagged for review; this is a risk signal, not proof for this order.")
        return flags

    @staticmethod
    def _deterministic_summary(
        escrow: models.StorefrontOrderEscrow,
        invoice: models.Invoice,
        evidence: list[dict[str, str]],
        missing: list[str],
        flags: list[str],
    ) -> str:
        claim = escrow.dispute_reason or "No buyer reason is recorded"
        return (
            f"Order {invoice.invoice_id or invoice.id} for ₦{(escrow.gross_kobo or 0) / 100:,.2f} "
            f"is {escrow.status}. Buyer report: {claim}. The review contains {len(evidence)} evidence "
            f"item(s), {len(missing)} missing-evidence item(s), and {len(flags)} review flag(s)."
        )

    @staticmethod
    def _deterministic_questions(
        escrow: models.StorefrontOrderEscrow,
        missing: list[str],
        flags: list[str],
    ) -> list[str]:
        questions: list[str] = []
        if escrow.requires_delivery:
            questions.append("Does courier or seller evidence identify the same destination and order?")
            questions.append("Do dispatch, delivery and dispute timestamps form a consistent sequence?")
        else:
            questions.append("What record demonstrates that the service or digital item was completed?")
        if missing:
            questions.append("Can the missing evidence be obtained from the buyer, seller or courier?")
        if flags:
            questions.append("Can each review flag be independently verified from its source record?")
        questions.append("Has each party's claim been separated from independently recorded platform evidence?")
        return questions[:5]

    @staticmethod
    def _utc(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc)

    @staticmethod
    def _minimised_ai_evidence(evidence: list[dict[str, str]]) -> list[dict[str, str]]:
        minimised: list[dict[str, str]] = []
        for item in evidence:
            detail = item["detail"]
            if item["label"] == "Delivery destination":
                detail = "A delivery destination is recorded in the order."
            minimised.append({**item, "detail": detail})
        return minimised

    @staticmethod
    def _is_neutral(narrative: DisputeNarrativeOut) -> bool:
        text = " ".join([narrative.neutral_summary, *narrative.reviewer_questions]).lower()
        prohibited = (
            "refund the buyer",
            "refund should",
            "should refund",
            "release to the seller",
            "release should",
            "should release",
            "side with",
            "buyer is at fault",
            "seller is at fault",
            "buyer is lying",
            "seller is lying",
        )
        return not any(phrase in text for phrase in prohibited)
