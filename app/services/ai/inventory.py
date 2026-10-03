from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy import case, func
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.inventory_models import (
    Product,
    PurchaseOrder,
    PurchaseOrderLine,
    PurchaseOrderStatus,
    StockMovement,
    StockMovementType,
)
from app.services.inventory.purchase_order_service import PurchaseOrderService

from .gateway import AIGateway, AIGatewayError
from .types import AIMessage, AIRequest

LOOKBACK_DAYS = 30
TARGET_COVER_DAYS = 30
SAFETY_STOCK_DAYS = 7
SLOW_STOCK_DAYS = 60
OPEN_ORDER_STATUSES = (
    PurchaseOrderStatus.DRAFT,
    PurchaseOrderStatus.PENDING,
    PurchaseOrderStatus.CONFIRMED,
)


class InventoryAdviceConflictError(ValueError):
    pass


class InventoryNarrative(BaseModel):
    headline: str = Field(min_length=1, max_length=180)
    summary: str = Field(min_length=1, max_length=800)


@dataclass(frozen=True)
class SalesFacts:
    sold_30: int
    sold_recent_14: int
    sold_previous_14: int
    sold_60: int


class InventoryAdviserService:
    def __init__(self, db: Session, *, gateway: AIGateway | None = None) -> None:
        self._db = db
        self._gateway = gateway or AIGateway(db)

    async def advice(
        self,
        *,
        actor_user_id: int,
        data_owner_id: int,
        enhance: bool = False,
    ) -> dict:
        recommendations = self._recommendations(data_owner_id)
        reorder = [item for item in recommendations if item["recommendation"] == "reorder_now"]
        slow = [item for item in recommendations if item["recommendation"] == "slow_stock"]
        estimated_cost = round(
            sum(float(item["estimated_order_cost"] or 0) for item in reorder),
            2,
        )
        narrative = self._deterministic_narrative(reorder, slow, estimated_cost)
        notice: str | None = None
        ai_generated = False

        if enhance and settings.AI_INVENTORY_ENHANCEMENT_ENABLED:
            try:
                narrative = await self._enhance_narrative(
                    recommendations,
                    actor_user_id=actor_user_id,
                    data_owner_id=data_owner_id,
                )
                ai_generated = True
            except AIGatewayError as exc:
                notice = f"AI explanation unavailable ({exc.code}); showing verified inventory calculations."

        return {
            "generated_at": dt.datetime.now(dt.timezone.utc),
            "lookback_days": LOOKBACK_DAYS,
            "target_cover_days": TARGET_COVER_DAYS,
            "headline": narrative.headline,
            "summary": narrative.summary,
            "ai_generated": ai_generated,
            "generation_notice": notice,
            "reorder_count": len(reorder),
            "slow_stock_count": len(slow),
            "estimated_reorder_cost": estimated_cost,
            "recommendations": recommendations,
        }

    def create_purchase_order(
        self,
        product_ids: list[int],
        *,
        data_owner_id: int,
    ) -> dict:
        selected_ids = sorted(set(product_ids))
        existing = self._existing_ai_order(selected_ids, data_owner_id)
        if existing:
            return self._purchase_order_out(existing, created=False)

        recommendations = {
            item["product_id"]: item
            for item in self._recommendations(data_owner_id)
            if item["recommendation"] == "reorder_now"
        }
        invalid_ids = [product_id for product_id in selected_ids if product_id not in recommendations]
        if invalid_ids:
            raise InventoryAdviceConflictError(
                "Selected products are no longer eligible for a reorder recommendation"
            )

        quantities = {
            product_id: int(recommendations[product_id]["recommended_order_quantity"])
            for product_id in selected_ids
        }
        approval_facts = {
            "owner": data_owner_id,
            "products": [{"id": product_id, "quantity": quantities[product_id]} for product_id in selected_ids],
        }
        approval_key = hashlib.sha256(
            json.dumps(approval_facts, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
        marker = f"AI inventory advice {approval_key}"
        existing = (
            self._db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.user_id == data_owner_id,
                PurchaseOrder.status.in_(OPEN_ORDER_STATUSES),
                PurchaseOrder.notes.contains(marker),
            )
            .one_or_none()
        )
        if existing:
            return self._purchase_order_out(existing, created=False)

        order = PurchaseOrderService(self._db, data_owner_id).generate_draft(
            selected_ids,
            quantities=quantities,
            notes=f"{marker}. Merchant-approved draft from verified velocity and stock-cover calculations.",
        )
        if not order or not order.lines:
            raise InventoryAdviceConflictError("No eligible products were available for this purchase order")
        return self._purchase_order_out(order, created=True)

    def _existing_ai_order(self, product_ids: list[int], data_owner_id: int) -> PurchaseOrder | None:
        candidates = (
            self._db.query(PurchaseOrder)
            .filter(
                PurchaseOrder.user_id == data_owner_id,
                PurchaseOrder.status.in_(OPEN_ORDER_STATUSES),
                PurchaseOrder.notes.like("AI inventory advice %"),
            )
            .all()
        )
        expected = set(product_ids)
        return next(
            (
                order
                for order in candidates
                if {line.product_id for line in order.lines} == expected
            ),
            None,
        )

    def _recommendations(self, data_owner_id: int) -> list[dict]:
        now = dt.datetime.now(dt.timezone.utc)
        products = (
            self._db.query(Product)
            .filter(
                Product.user_id == data_owner_id,
                Product.is_active.is_(True),
                Product.track_stock.is_(True),
                Product.fulfilment_type == "physical",
            )
            .order_by(Product.name)
            .all()
        )
        if not products:
            return []

        sales = self._sales_facts(data_owner_id, now)
        incoming = self._incoming_stock(data_owner_id)
        recommendations = [
            self._recommendation(
                product,
                sales.get(product.id, SalesFacts(0, 0, 0, 0)),
                incoming.get(product.id, 0),
                now,
            )
            for product in products
        ]
        priority = {"reorder_now": 0, "watch": 1, "slow_stock": 2, "healthy": 3, "insufficient_data": 4}
        return sorted(
            recommendations,
            key=lambda item: (
                priority[item["recommendation"]],
                item["days_of_stock"] if item["days_of_stock"] is not None else math.inf,
                item["product_name"].lower(),
            ),
        )

    def _sales_facts(self, data_owner_id: int, now: dt.datetime) -> dict[int, SalesFacts]:
        cutoff_60 = now - dt.timedelta(days=SLOW_STOCK_DAYS)
        cutoff_30 = now - dt.timedelta(days=LOOKBACK_DAYS)
        cutoff_28 = now - dt.timedelta(days=28)
        cutoff_14 = now - dt.timedelta(days=14)
        rows = (
            self._db.query(
                StockMovement.product_id,
                func.sum(case((StockMovement.created_at >= cutoff_30, -StockMovement.quantity), else_=0)),
                func.sum(case((StockMovement.created_at >= cutoff_14, -StockMovement.quantity), else_=0)),
                func.sum(
                    case(
                        (
                            (StockMovement.created_at >= cutoff_28) & (StockMovement.created_at < cutoff_14),
                            -StockMovement.quantity,
                        ),
                        else_=0,
                    )
                ),
                func.sum(case((StockMovement.created_at >= cutoff_60, -StockMovement.quantity), else_=0)),
            )
            .filter(
                StockMovement.user_id == data_owner_id,
                StockMovement.movement_type == StockMovementType.SALE,
                StockMovement.created_at >= cutoff_60,
            )
            .group_by(StockMovement.product_id)
            .all()
        )
        return {
            int(product_id): SalesFacts(
                sold_30=max(0, int(sold_30 or 0)),
                sold_recent_14=max(0, int(sold_recent_14 or 0)),
                sold_previous_14=max(0, int(sold_previous_14 or 0)),
                sold_60=max(0, int(sold_60 or 0)),
            )
            for product_id, sold_30, sold_recent_14, sold_previous_14, sold_60 in rows
        }

    def _incoming_stock(self, data_owner_id: int) -> dict[int, int]:
        rows = (
            self._db.query(
                PurchaseOrderLine.product_id,
                func.sum(PurchaseOrderLine.quantity - PurchaseOrderLine.quantity_received),
            )
            .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderLine.purchase_order_id)
            .filter(
                PurchaseOrder.user_id == data_owner_id,
                PurchaseOrder.status.in_(OPEN_ORDER_STATUSES),
            )
            .group_by(PurchaseOrderLine.product_id)
            .all()
        )
        return {int(product_id): max(0, int(quantity or 0)) for product_id, quantity in rows}

    def _recommendation(
        self,
        product: Product,
        sales: SalesFacts,
        incoming: int,
        now: dt.datetime,
    ) -> dict:
        created_date = product.created_at.date() if product.created_at else now.date()
        age_days = max(1, (now.date() - created_date).days + 1)
        observation_days = min(LOOKBACK_DAYS, max(7, age_days))
        velocity = sales.sold_30 / observation_days
        days_of_stock = round(product.quantity_in_stock / velocity, 1) if velocity > 0 else None
        trend = self._demand_trend(sales)
        target_units = math.ceil(velocity * (TARGET_COVER_DAYS + SAFETY_STOCK_DAYS))
        recommended_quantity = max(0, target_units - product.quantity_in_stock - incoming)
        low_stock = product.quantity_in_stock <= product.reorder_level
        slow_stock = age_days >= SLOW_STOCK_DAYS and sales.sold_60 == 0 and product.quantity_in_stock > 0

        reason_codes: list[str] = []
        if low_stock:
            reason_codes.append("at_or_below_reorder_level")
        if product.quantity_in_stock <= 0:
            reason_codes.append("out_of_stock")
        if days_of_stock is not None and days_of_stock <= 14:
            reason_codes.append("less_than_14_days_cover")
        if trend == "rising":
            reason_codes.append("demand_rising")
        if incoming:
            reason_codes.append("open_purchase_order_stock")

        if slow_stock:
            recommendation = "slow_stock"
            recommended_quantity = 0
            reason_codes.append("no_sales_60_days")
            explanation = (
                f"No sales were recorded in 60 days and {product.quantity_in_stock} {product.unit} remain. "
                "Avoid reordering; consider merchandising or a margin-safe promotion."
            )
        elif sales.sold_30 == 0 and age_days < SLOW_STOCK_DAYS:
            if low_stock and incoming == 0:
                recommendation = "reorder_now"
                recommended_quantity = max(
                    product.reorder_quantity,
                    product.reorder_level * 2 - product.quantity_in_stock,
                )
                reason_codes.append("configured_reorder_threshold")
                explanation = (
                    f"Stock is {product.quantity_in_stock} {product.unit}, at or below the configured reorder level "
                    f"of {product.reorder_level}. There is not enough sales history yet, so the configured reorder "
                    "quantity is used."
                )
            else:
                recommendation = "insufficient_data"
                explanation = (
                    "Fewer than 60 days of history and no recorded sales yet; "
                    "keep recording sales before forecasting."
                )
        elif recommended_quantity > 0 and (low_stock or days_of_stock is None or days_of_stock <= 14):
            recommendation = "reorder_now"
            explanation = self._velocity_explanation(
                product,
                sales,
                velocity,
                days_of_stock,
                incoming,
                recommended_quantity,
            )
        elif days_of_stock is not None and days_of_stock <= TARGET_COVER_DAYS:
            recommendation = "watch"
            explanation = self._velocity_explanation(product, sales, velocity, days_of_stock, incoming, 0)
        else:
            recommendation = "healthy"
            explanation = self._velocity_explanation(product, sales, velocity, days_of_stock, incoming, 0)

        estimated_cost = (
            float((product.cost_price or Decimal("0")) * recommended_quantity)
            if recommended_quantity > 0 and product.cost_price is not None
            else None
        )
        return {
            "product_id": product.id,
            "product_name": product.name,
            "sku": product.sku,
            "unit": product.unit,
            "current_stock": product.quantity_in_stock,
            "incoming_stock": incoming,
            "units_sold_30_days": sales.sold_30,
            "daily_sales_velocity": round(velocity, 2),
            "days_of_stock": days_of_stock,
            "demand_trend": trend,
            "recommendation": recommendation,
            "recommended_order_quantity": recommended_quantity,
            "estimated_order_cost": round(estimated_cost, 2) if estimated_cost is not None else None,
            "explanation": explanation,
            "reason_codes": reason_codes,
        }

    @staticmethod
    def _demand_trend(sales: SalesFacts) -> str:
        if sales.sold_recent_14 == 0 and sales.sold_previous_14 == 0:
            return "no_sales"
        if sales.sold_previous_14 == 0:
            return "rising"
        change = (sales.sold_recent_14 - sales.sold_previous_14) / sales.sold_previous_14
        return "rising" if change >= 0.25 else "falling" if change <= -0.25 else "steady"

    @staticmethod
    def _velocity_explanation(
        product: Product,
        sales: SalesFacts,
        velocity: float,
        days_of_stock: float | None,
        incoming: int,
        recommended_quantity: int,
    ) -> str:
        cover = (
            f"about {days_of_stock:g} days of stock remain"
            if days_of_stock is not None
            else "stock cover is unknown"
        )
        incoming_text = f", with {incoming} {product.unit} already on open purchase orders" if incoming else ""
        action = (
            f" Recommend ordering {recommended_quantity} {product.unit} for 30 days of cover plus 7 safety days."
            if recommended_quantity
            else ""
        )
        return (
            f"{sales.sold_30} {product.unit} sold in 30 days ({velocity:.2f}/day); {cover}{incoming_text}.{action}"
        )

    @staticmethod
    def _deterministic_narrative(
        reorder: list[dict],
        slow: list[dict],
        estimated_cost: float,
    ) -> InventoryNarrative:
        if reorder:
            headline = f"{len(reorder)} product{'s' if len(reorder) != 1 else ''} may need restocking"
            cost_text = f" Estimated draft cost: ₦{estimated_cost:,.0f}." if estimated_cost else ""
            summary = (
                "Recommendations use recorded sales, current stock and open purchase orders."
                f"{cost_text} Review quantities before creating a draft purchase order."
            )
        elif slow:
            headline = f"{len(slow)} slow-moving product{'s' if len(slow) != 1 else ''} need attention"
            summary = "No immediate reorder is suggested. Review merchandising before discounting or buying more stock."
        else:
            headline = "Inventory cover looks stable"
            summary = "No tracked physical product currently needs a demand-based reorder."
        return InventoryNarrative(headline=headline, summary=summary)

    async def _enhance_narrative(
        self,
        recommendations: list[dict],
        *,
        actor_user_id: int,
        data_owner_id: int,
    ) -> InventoryNarrative:
        facts = [
            {
                "product": item["product_name"],
                "stock": item["current_stock"],
                "incoming": item["incoming_stock"],
                "sold_30_days": item["units_sold_30_days"],
                "days_of_stock": item["days_of_stock"],
                "recommendation": item["recommendation"],
                "recommended_order_quantity": item["recommended_order_quantity"],
            }
            for item in recommendations[:20]
        ]
        result = await self._gateway.generate_structured(
            AIRequest(
                feature="inventory_advice_explanation",
                prompt_version="inventory-advice-v1",
                messages=[
                    AIMessage(
                        role="system",
                        content=(
                            "Explain the supplied verified inventory calculations to a Nigerian SME merchant. "
                            "Do not change quantities, invent forecasts, suppliers, prices or sales. Do not tell the "
                            "merchant that an order has been placed. Return JSON with headline and summary."
                        ),
                    ),
                    AIMessage(role="user", content=json.dumps(facts, separators=(",", ":"))),
                ],
            ),
            InventoryNarrative,
            actor_user_id=actor_user_id,
            data_owner_id=data_owner_id,
        )
        return cast(InventoryNarrative, result)

    @staticmethod
    def _purchase_order_out(order: PurchaseOrder, *, created: bool) -> dict:
        return {
            "id": order.id,
            "order_number": order.order_number,
            "status": "draft",
            "total_amount": float(order.total_amount or 0),
            "lines": [
                {
                    "product_id": line.product_id,
                    "product_name": line.product.name,
                    "quantity": line.quantity,
                    "unit_cost": float(line.unit_cost) if line.unit_cost is not None else None,
                    "total_cost": float(line.total_cost) if line.total_cost is not None else None,
                }
                for line in order.lines
            ],
            "created": created,
            "notice": (
                "Draft purchase order created. Review supplier, costs and quantities before sending it."
                if created
                else "This recommendation already has an open draft purchase order."
            ),
        }
