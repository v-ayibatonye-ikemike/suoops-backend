from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import models
from app.models.ai_models import AICopilotBriefing, AIProposedAction
from app.models.inventory_models import Product
from app.services.analytics_service import calculate_cash_position, calculate_customer_insights

from .gateway import AIGateway, AIGatewayError
from .types import AIMessage, AIRequest


class BriefingNarrative(BaseModel):
    headline: str = Field(min_length=1, max_length=180)
    summary: str = Field(min_length=1, max_length=800)


@dataclass(frozen=True)
class CopilotAction:
    action_type: str
    title: str
    reason: str
    action_url: str
    payload: dict[str, object]
    dedupe_key: str


class UnsupportedCopilotQuestion(ValueError):
    pass


class CommerceCopilotService:
    def __init__(self, db: Session, *, gateway: AIGateway | None = None) -> None:
        self._db = db
        self._gateway = gateway or AIGateway(db)

    async def daily_briefing(self, *, actor_user_id: int, data_owner_id: int, enhance: bool = True) -> dict:
        facts = self._briefing_facts(data_owner_id)
        actions = self._recommend_actions(facts)
        persisted_actions = [
            persisted
            for action in actions
            if (
                persisted := self._upsert_action(
                    action,
                    actor_user_id=actor_user_id,
                    data_owner_id=data_owner_id,
                )
            )
            is not None
        ]
        narrative = self._deterministic_narrative(facts)
        notice: str | None = None
        ai_generated = False

        facts_hash = hashlib.sha256(
            json.dumps(facts, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()
        cached = self._cached_briefing(data_owner_id, facts_hash)
        if cached:
            narrative = BriefingNarrative(headline=cached.headline, summary=cached.summary)
            notice = cached.generation_notice
            ai_generated = cached.ai_generated
        elif enhance and settings.AI_COPILOT_ENHANCEMENT_ENABLED:
            try:
                narrative = await self._enhance_narrative(
                    facts,
                    actor_user_id=actor_user_id,
                    data_owner_id=data_owner_id,
                )
                ai_generated = True
            except AIGatewayError as exc:
                notice = f"AI enhancement unavailable ({exc.code}); showing verified business facts."
            self._save_briefing(data_owner_id, facts_hash, narrative, ai_generated, notice)

        return {
            "generated_at": dt.datetime.now(dt.timezone.utc),
            "data_as_of": dt.datetime.now(dt.timezone.utc),
            "headline": narrative.headline,
            "summary": narrative.summary,
            "ai_generated": ai_generated,
            "generation_notice": notice,
            "facts": facts,
            "actions": [self._action_out(action) for action in persisted_actions],
            "suggested_questions": self.suggested_questions(),
        }

    def answer_question(self, question: str, *, data_owner_id: int) -> dict:
        normalized = " ".join(question.lower().strip().split())
        if not normalized or len(normalized) > 500:
            raise ValueError("Question must contain between 1 and 500 characters")

        intent = self._classify_question(normalized)
        facts = self._briefing_facts(data_owner_id)
        currency = "₦"

        if intent == "cash":
            cash = facts["cash"]
            answer = (
                f"You collected {currency}{cash['cash_collected_today']:,.0f} today and "
                f"{currency}{cash['cash_collected_this_week']:,.0f} in the last 7 days."
            )
            evidence = ["paid invoices and recorded quick sales"]
        elif intent == "overdue":
            overdue = facts["overdue"]
            answer = (
                f"{overdue['count']} invoice{'s are' if overdue['count'] != 1 else ' is'} overdue, "
                f"totalling {currency}{overdue['amount']:,.0f}."
            )
            if overdue["top"]:
                first = overdue["top"][0]
                answer += (
                    f" The largest is {currency}{first['amount']:,.0f} from {first['customer_name']} "
                    f"({first['days_overdue']} days overdue)."
                )
            evidence = ["pending revenue invoices whose due date has passed"]
        elif intent == "outstanding":
            cash = facts["cash"]
            answer = (
                f"Your total outstanding amount is {currency}{cash['total_outstanding']:,.0f}. "
                f"{currency}{cash['expected_inflow_7_days']:,.0f} is due within the next 7 days."
            )
            evidence = ["pending and awaiting-confirmation revenue invoices"]
        elif intent == "best_sellers":
            products = facts["top_products"]
            if products:
                listing = ", ".join(f"{item['name']} ({item['units']} sold)" for item in products[:3])
                answer = f"Your top products in the last 30 days are {listing}."
            else:
                answer = "There are no paid product sales in the last 30 days yet."
            evidence = ["paid invoice line items from the last 30 days"]
        elif intent == "customers":
            customers = facts["customers"]
            answer = (
                f"You have {customers['at_risk']} at-risk and {customers['dormant']} dormant "
                "customers in the customers currently analysed."
            )
            evidence = ["customer invoice recency and payment history"]
        elif intent == "stock":
            inventory = facts["inventory"]
            answer = (
                f"{inventory['low_stock_count']} product{'s are' if inventory['low_stock_count'] != 1 else ' is'} "
                f"low on stock, including {', '.join(inventory['low_stock_names'][:3])}."
                if inventory["low_stock_count"]
                else "No tracked products are currently at or below their reorder level."
            )
            evidence = ["current stock and configured reorder levels"]
        elif intent == "priorities":
            actions = self._recommend_actions(facts)
            if actions:
                answer = "Your priorities are: " + "; ".join(action.title for action in actions[:3]) + "."
            else:
                answer = "Nothing urgent is showing right now. Keep recording sales and expenses for better guidance."
            evidence = ["cash, overdue invoices, customer activity, stock and storefront setup"]
        else:
            return {
                "intent": "unsupported",
                "answer": (
                    "I can answer verified questions about cash collected, outstanding or overdue invoices, "
                    "top products, customer activity, low stock, and today's priorities."
                ),
                "evidence": [],
                "generated_at": dt.datetime.now(dt.timezone.utc),
                "suggested_questions": self.suggested_questions(),
            }

        return {
            "intent": intent,
            "answer": answer,
            "evidence": evidence,
            "generated_at": dt.datetime.now(dt.timezone.utc),
            "suggested_questions": self.suggested_questions(),
        }

    def list_actions(self, data_owner_id: int, status: str = "proposed") -> list[AIProposedAction]:
        return cast(
            list[AIProposedAction],
            (
                self._db.query(AIProposedAction)
                .filter(AIProposedAction.data_owner_id == data_owner_id, AIProposedAction.status == status)
                .order_by(AIProposedAction.created_at.desc())
                .limit(50)
                .all()
            ),
        )

    def decide_action(
        self,
        public_id: str,
        *,
        decision: str,
        actor_user_id: int,
        data_owner_id: int,
    ) -> AIProposedAction:
        if decision not in {"accepted", "dismissed"}:
            raise ValueError("Decision must be accepted or dismissed")
        action = cast(
            AIProposedAction | None,
            (
                self._db.query(AIProposedAction)
                .filter(
                    AIProposedAction.public_id == public_id,
                    AIProposedAction.data_owner_id == data_owner_id,
                )
                .one_or_none()
            )
        )
        if not action:
            raise LookupError("Proposed action not found")
        if action.status != "proposed":
            raise ValueError(f"Proposed action is already {action.status}")
        action.status = decision
        action.decided_by_user_id = actor_user_id
        action.decided_at = dt.datetime.now(dt.timezone.utc)
        self._db.commit()
        self._db.refresh(action)
        return action

    @staticmethod
    def suggested_questions() -> list[str]:
        return [
            "How much did I collect this week?",
            "Who owes me money?",
            "What are my best-selling products?",
            "What should I do today?",
        ]

    def _briefing_facts(self, user_id: int) -> dict:
        cash = calculate_cash_position(self._db, user_id)
        overdue_rows = (
            self._db.query(
                models.Invoice.invoice_id,
                models.Invoice.amount,
                models.Invoice.due_date,
                models.Customer.name,
            )
            .join(models.Customer, models.Customer.id == models.Invoice.customer_id)
            .filter(
                models.Invoice.issuer_id == user_id,
                models.Invoice.invoice_type == "revenue",
                models.Invoice.status == "pending",
                models.Invoice.due_date.isnot(None),
                models.Invoice.due_date < dt.datetime.now(dt.timezone.utc),
            )
            .order_by(models.Invoice.amount.desc())
            .limit(5)
            .all()
        )
        today = dt.datetime.now(dt.timezone.utc).date()
        overdue_top = [
            {
                "invoice_id": row.invoice_id,
                "customer_name": row.name,
                "amount": float(row.amount),
                "days_overdue": max(0, (today - row.due_date.date()).days),
            }
            for row in overdue_rows
        ]

        customer_insights = calculate_customer_insights(self._db, user_id, limit=100)
        customer_summary = customer_insights["summary"]
        low_stock = (
            self._db.query(Product.name)
            .filter(
                Product.user_id == user_id,
                Product.is_active.is_(True),
                Product.track_stock.is_(True),
                Product.quantity_in_stock <= Product.reorder_level,
            )
            .order_by(Product.quantity_in_stock.asc(), Product.name.asc())
            .limit(10)
            .all()
        )
        since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        top_products = (
            self._db.query(
                models.InvoiceLine.description,
                func.coalesce(func.sum(models.InvoiceLine.quantity), 0).label("units"),
                func.coalesce(
                    func.sum(models.InvoiceLine.quantity * models.InvoiceLine.unit_price), Decimal("0")
                ).label("revenue"),
            )
            .join(models.Invoice, models.Invoice.id == models.InvoiceLine.invoice_id)
            .filter(
                models.Invoice.issuer_id == user_id,
                models.Invoice.invoice_type == "revenue",
                models.Invoice.status == "paid",
                models.Invoice.paid_at >= since,
            )
            .group_by(models.InvoiceLine.description)
            .order_by(func.sum(models.InvoiceLine.quantity).desc())
            .limit(5)
            .all()
        )
        user = self._db.query(models.User).filter(models.User.id == user_id).one()
        return {
            "cash": cash,
            "overdue": {
                "count": cash["overdue_count"],
                "amount": cash["total_overdue"],
                "top": overdue_top,
            },
            "customers": {
                "at_risk": int(customer_summary.get("at_risk", 0)),
                "dormant": int(customer_summary.get("dormant", 0)),
                "vip": int(customer_summary.get("vip", 0)),
            },
            "inventory": {
                "low_stock_count": len(low_stock),
                "low_stock_names": [row.name for row in low_stock],
            },
            "top_products": [
                {"name": row.description, "units": int(row.units), "revenue": float(row.revenue)}
                for row in top_products
            ],
            "storefront": {
                "enabled": bool(user.storefront_enabled),
                "views": int(user.storefront_views or 0),
            },
        }

    def _recommend_actions(self, facts: dict) -> list[CopilotAction]:
        today = dt.date.today().isoformat()
        actions: list[CopilotAction] = []
        overdue = facts["overdue"]
        if overdue["count"]:
            actions.append(
                CopilotAction(
                    action_type="review_overdue_invoices",
                    title=f"Review {overdue['count']} overdue invoice{'s' if overdue['count'] != 1 else ''}",
                    reason=f"₦{overdue['amount']:,.0f} is overdue and may need a follow-up.",
                    action_url="/dashboard/collections",
                    payload={"invoice_ids": [item["invoice_id"] for item in overdue["top"]]},
                    dedupe_key=f"{today}:review_overdue_invoices",
                )
            )
        inventory = facts["inventory"]
        if inventory["low_stock_count"]:
            actions.append(
                CopilotAction(
                    action_type="review_low_stock",
                    title=f"Review {inventory['low_stock_count']} low-stock product"
                    f"{'s' if inventory['low_stock_count'] != 1 else ''}",
                    reason="These tracked products are at or below their configured reorder level.",
                    action_url="/dashboard/inventory",
                    payload={"product_names": inventory["low_stock_names"]},
                    dedupe_key=f"{today}:review_low_stock",
                )
            )
        customers = facts["customers"]
        follow_up_count = customers["at_risk"] + customers["dormant"]
        if follow_up_count:
            actions.append(
                CopilotAction(
                    action_type="review_inactive_customers",
                    title=f"Review {follow_up_count} inactive customer{'s' if follow_up_count != 1 else ''}",
                    reason="Their recent invoice activity suggests they may need re-engagement.",
                    action_url="/dashboard/analytics",
                    payload={"at_risk": customers["at_risk"], "dormant": customers["dormant"]},
                    dedupe_key=f"{today}:review_inactive_customers",
                )
            )
        if not facts["storefront"]["enabled"]:
            actions.append(
                CopilotAction(
                    action_type="complete_storefront",
                    title="Complete your online storefront",
                    reason="A storefront gives customers a direct place to browse and order.",
                    action_url="/dashboard/settings#storefront",
                    payload={},
                    dedupe_key=f"{today}:complete_storefront",
                )
            )
        return actions[:3]

    @staticmethod
    def _deterministic_narrative(facts: dict) -> BriefingNarrative:
        cash = facts["cash"]
        overdue = facts["overdue"]
        headline = "Your business is up to date"
        if overdue["amount"] > 0:
            headline = f"₦{overdue['amount']:,.0f} needs collection attention"
        elif facts["inventory"]["low_stock_count"]:
            headline = f"{facts['inventory']['low_stock_count']} product(s) need stock attention"
        elif cash["cash_collected_today"] > 0:
            headline = f"₦{cash['cash_collected_today']:,.0f} collected today"

        summary = (
            f"You collected ₦{cash['cash_collected_this_week']:,.0f} in the last 7 days. "
            f"₦{cash['total_outstanding']:,.0f} is outstanding, including "
            f"₦{overdue['amount']:,.0f} overdue. "
            f"{facts['inventory']['low_stock_count']} tracked product(s) are low on stock."
        )
        return BriefingNarrative(headline=headline, summary=summary)

    async def _enhance_narrative(
        self,
        facts: dict,
        *,
        actor_user_id: int,
        data_owner_id: int,
    ) -> BriefingNarrative:
        request = AIRequest(
            feature="daily_briefing",
            prompt_version="daily-briefing-v1",
            messages=[
                AIMessage(
                    role="system",
                    content=(
                        "You are SuoOps Commerce Copilot. Write a calm, useful headline and a two-sentence summary "
                        "using only the supplied verified JSON facts. Keep every amount and count exact. "
                        "Do not give tax, credit, legal, pricing, refund, or payout decisions. Return JSON with "
                        "headline and summary."
                    ),
                ),
                AIMessage(role="user", content=json.dumps(facts, sort_keys=True, default=str)),
            ],
            metadata={"surface": "dashboard"},
        )
        return await self._gateway.generate_structured(
            request,
            BriefingNarrative,
            actor_user_id=actor_user_id,
            data_owner_id=data_owner_id,
        )

    def _cached_briefing(self, data_owner_id: int, facts_hash: str) -> AICopilotBriefing | None:
        return cast(
            AICopilotBriefing | None,
            (
                self._db.query(AICopilotBriefing)
                .filter(
                    AICopilotBriefing.data_owner_id == data_owner_id,
                    AICopilotBriefing.briefing_date == dt.date.today(),
                    AICopilotBriefing.facts_hash == facts_hash,
                )
                .one_or_none()
            ),
        )

    def _save_briefing(
        self,
        data_owner_id: int,
        facts_hash: str,
        narrative: BriefingNarrative,
        ai_generated: bool,
        notice: str | None,
    ) -> None:
        retention_cutoff = dt.date.today() - dt.timedelta(days=30)
        self._db.query(AICopilotBriefing).filter(
            AICopilotBriefing.data_owner_id == data_owner_id,
            AICopilotBriefing.briefing_date < retention_cutoff,
        ).delete(synchronize_session=False)
        briefing = cast(
            AICopilotBriefing | None,
            (
                self._db.query(AICopilotBriefing)
                .filter(
                    AICopilotBriefing.data_owner_id == data_owner_id,
                    AICopilotBriefing.briefing_date == dt.date.today(),
                )
                .one_or_none()
            )
        )
        if not briefing:
            briefing = AICopilotBriefing(data_owner_id=data_owner_id, briefing_date=dt.date.today())
        briefing.facts_hash = facts_hash
        briefing.headline = narrative.headline
        briefing.summary = narrative.summary
        briefing.ai_generated = ai_generated
        briefing.generation_notice = notice
        briefing.generated_at = dt.datetime.now(dt.timezone.utc)
        self._db.add(briefing)
        self._db.commit()

    def _upsert_action(
        self,
        proposed: CopilotAction,
        *,
        actor_user_id: int,
        data_owner_id: int,
    ) -> AIProposedAction | None:
        existing = cast(
            AIProposedAction | None,
            (
                self._db.query(AIProposedAction)
                .filter(
                    AIProposedAction.data_owner_id == data_owner_id,
                    AIProposedAction.dedupe_key == proposed.dedupe_key,
                )
                .one_or_none()
            )
        )
        if existing:
            return existing if existing.status == "proposed" else None
        action = AIProposedAction(
            public_id=str(uuid.uuid4()),
            data_owner_id=data_owner_id,
            proposed_by_user_id=actor_user_id,
            action_type=proposed.action_type,
            title=proposed.title,
            reason=proposed.reason,
            action_url=proposed.action_url,
            payload=proposed.payload,
            dedupe_key=proposed.dedupe_key,
            status="proposed",
        )
        self._db.add(action)
        try:
            self._db.commit()
        except IntegrityError:
            self._db.rollback()
            return cast(
                AIProposedAction,
                (
                    self._db.query(AIProposedAction)
                    .filter(
                        AIProposedAction.data_owner_id == data_owner_id,
                        AIProposedAction.dedupe_key == proposed.dedupe_key,
                    )
                    .one()
                )
            )
        self._db.refresh(action)
        return action

    @staticmethod
    def _action_out(action: AIProposedAction) -> dict:
        return {
            "id": action.public_id,
            "action_type": action.action_type,
            "title": action.title,
            "reason": action.reason,
            "action_url": action.action_url,
            "status": action.status,
            "created_at": action.created_at,
        }

    @staticmethod
    def _classify_question(question: str) -> str:
        if any(term in question for term in ("what should i do", "priority", "priorities", "attention today")):
            return "priorities"
        if any(term in question for term in ("overdue", "who owes", "late invoice", "unpaid customer")):
            return "overdue"
        if any(term in question for term in ("outstanding", "expected inflow", "money due")):
            return "outstanding"
        if any(term in question for term in ("best sell", "best-sell", "top product", "most sold", "selling most")):
            return "best_sellers"
        if any(term in question for term in ("low stock", "run out", "restock", "inventory attention")):
            return "stock"
        if any(term in question for term in ("customer", "dormant", "at risk", "inactive buyer")):
            return "customers"
        if any(term in question for term in ("collect", "cash", "revenue", "made", "sales this week")):
            return "cash"
        return "unsupported"
