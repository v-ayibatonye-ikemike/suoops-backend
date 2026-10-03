from __future__ import annotations

import datetime as dt
import itertools
import json
import math
from collections import Counter, defaultdict
from decimal import Decimal
from typing import cast

from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload

from app.bot.conversation_window import is_window_open
from app.core.config import settings
from app.models import models
from app.models.inventory_models import Product
from app.services.analytics_service import calculate_storefront_insights

from .gateway import AIGateway, AIGatewayError
from .types import AIMessage, AIRequest


class StorefrontAdviceConflictError(ValueError):
    pass


class ProductCopyOut(BaseModel):
    description: str = Field(min_length=20, max_length=800)


class StorefrontAdviserService:
    def __init__(self, db: Session, *, gateway: AIGateway | None = None) -> None:
        self._db = db
        self._gateway = gateway or AIGateway(db)

    def advice(self, data_owner_id: int) -> dict:
        owner = self._owner(data_owner_id)
        products = self._products(data_owner_id)
        sales = self._storefront_sales(data_owner_id)
        listings = [self._listing_advice(product, sales.get(product.id, 0)) for product in products]
        profile_score = self._profile_score(owner)
        listing_score = round(sum(item["quality_score"] for item in listings) / len(listings)) if listings else 0
        quality_score = round(profile_score * 0.4 + listing_score * 0.6)
        funnel = self._funnel(data_owner_id)
        bundles = self._bundle_suggestions(data_owner_id, products)
        drafts = self._reengagement_drafts(owner, products)

        if not owner.storefront_enabled:
            headline = "Your storefront is not live yet"
            summary = "Complete the storefront setup before using conversion and merchandising recommendations."
        elif any(item["recommendation"] == "improve_listing" for item in listings):
            count = sum(item["recommendation"] == "improve_listing" for item in listings)
            headline = f"{count} listing{'s' if count != 1 else ''} can be improved"
            summary = (
                "Start with missing photos, descriptions and categories "
                "so buyers can understand what they will receive."
            )
        elif funnel["abandoned_orders_30_days"] > funnel["paid_orders_30_days"]:
            headline = "More storefront orders are being abandoned than paid"
            summary = (
                "Review product clarity, delivery expectations and checkout "
                "readiness before promoting the store further."
            )
        else:
            headline = "Your storefront is ready for merchandising"
            summary = (
                "Use verified sales and margin signals to feature products, "
                "group complementary items and apply safe promotions."
            )

        return {
            "generated_at": dt.datetime.now(dt.timezone.utc),
            "quality_score": quality_score,
            "headline": headline,
            "summary": summary,
            "funnel": funnel,
            "listings": listings,
            "bundle_suggestions": bundles,
            "reengagement_drafts": drafts,
        }

    async def draft_copy(
        self,
        product_id: int,
        *,
        actor_user_id: int,
        data_owner_id: int,
    ) -> dict:
        product = self._product(product_id, data_owner_id)
        deterministic = self._deterministic_description(product)
        description = deterministic
        ai_generated = False
        notice: str | None = None

        if settings.AI_STOREFRONT_ENHANCEMENT_ENABLED:
            facts = {
                "name": product.name,
                "category": product.category.name if product.category else None,
                "price_ngn": float(product.selling_price),
                "unit": product.unit,
                "fulfilment_type": product.fulfilment_type,
                "existing_description": product.description,
            }
            try:
                result = await self._gateway.generate_structured(
                    AIRequest(
                        feature="storefront_product_copy",
                        prompt_version="storefront-copy-v1",
                        messages=[
                            AIMessage(
                                role="system",
                                content=(
                                    "Write a clear storefront product description using only "
                                    "the supplied verified facts. Do not invent materials, sizes, "
                                    "colours, origin, health benefits, guarantees, scarcity, "
                                    "reviews, delivery times or discounts. Return JSON with description."
                                ),
                            ),
                            AIMessage(role="user", content=json.dumps(facts, separators=(",", ":"))),
                        ],
                    ),
                    ProductCopyOut,
                    actor_user_id=actor_user_id,
                    data_owner_id=data_owner_id,
                )
                description = cast(ProductCopyOut, result).description
                ai_generated = True
            except AIGatewayError as exc:
                notice = f"AI copy unavailable ({exc.code}); showing a verified factual draft."

        return {
            "product_id": product.id,
            "description": description,
            "ai_generated": ai_generated,
            "generation_notice": notice,
        }

    def apply_copy(self, product_id: int, description: str, *, data_owner_id: int) -> dict:
        product = self._product(product_id, data_owner_id, lock=True)
        product.description = description.strip()
        self._db.commit()
        self._db.refresh(product)
        return self._product_out(product)

    def apply_merchandising(self, product_ids: list[int], *, data_owner_id: int) -> dict:
        selected = set(product_ids)
        products = self._products(data_owner_id, lock=True)
        available_ids = {product.id for product in products if self._is_listable(product)}
        if not selected.issubset(available_ids):
            raise StorefrontAdviceConflictError("Only complete, active storefront listings can be featured")
        for product in products:
            product.storefront_featured = product.id in selected
        self._db.commit()
        return {
            "products": [self._product_out(product) for product in products if product.id in selected],
            "notice": "Featured products updated. Buyers will see them first in your storefront.",
        }

    def apply_promotion(
        self,
        product_id: int,
        discount_percent: int,
        *,
        data_owner_id: int,
    ) -> dict:
        product = self._product(product_id, data_owner_id, lock=True)
        max_safe = self._max_safe_discount(product)
        if discount_percent > max_safe:
            raise StorefrontAdviceConflictError(
                f"Discount exceeds the margin-safe maximum of {max_safe}% for this product"
            )
        product.storefront_discount_percent = discount_percent
        self._db.commit()
        self._db.refresh(product)
        return self._product_out(product)

    def apply_bundle(
        self,
        product_ids: list[int],
        title: str,
        *,
        active: bool,
        data_owner_id: int,
    ) -> dict:
        selected = set(product_ids)
        products = self._products(data_owner_id, lock=True)
        chosen = [product for product in products if product.id in selected]
        if len(chosen) != len(selected) or any(not self._is_listable(product) for product in chosen):
            raise StorefrontAdviceConflictError("Bundle products must be active, complete storefront listings")
        if active:
            valid_pairs = {
                frozenset(suggestion["product_ids"]) for suggestion in self._bundle_suggestions(data_owner_id, products)
            }
            if frozenset(selected) not in valid_pairs:
                raise StorefrontAdviceConflictError("This bundle is no longer supported by recent paid orders")
        for product in chosen:
            product.storefront_bundle_label = title.strip() if active else None
            if active:
                product.storefront_featured = True
        self._db.commit()
        return {
            "products": [self._product_out(product) for product in chosen],
            "notice": (
                "Shop-together bundle published at normal verified product prices."
                if active
                else "Shop-together bundle removed."
            ),
        }

    def _owner(self, data_owner_id: int) -> models.User:
        owner = self._db.query(models.User).filter(models.User.id == data_owner_id).one_or_none()
        if not owner:
            raise LookupError("Storefront owner not found")
        return cast(models.User, owner)

    def _products(self, data_owner_id: int, *, lock: bool = False) -> list[Product]:
        query = (
            self._db.query(Product)
            .options(joinedload(Product.category))
            .filter(Product.user_id == data_owner_id, Product.is_active.is_(True))
        )
        if lock:
            query = query.with_for_update()
        return cast(list[Product], query.order_by(Product.name).all())

    def _product(self, product_id: int, data_owner_id: int, *, lock: bool = False) -> Product:
        query = (
            self._db.query(Product)
            .options(joinedload(Product.category))
            .filter(Product.id == product_id, Product.user_id == data_owner_id, Product.is_active.is_(True))
        )
        if lock:
            query = query.with_for_update()
        product = query.one_or_none()
        if not product:
            raise LookupError("Product not found")
        return cast(Product, product)

    def _funnel(self, data_owner_id: int) -> dict:
        today = dt.date.today()
        insights = calculate_storefront_insights(
            self._db,
            data_owner_id,
            today - dt.timedelta(days=29),
            today,
            Decimal("1"),
            top_limit=100,
        )
        paid = int(insights["paid_orders"])
        abandoned = int(insights["abandoned_orders"])
        if not insights["views"]:
            explanation = "No storefront views have been recorded yet, so conversion cannot be assessed."
        elif paid == 0 and abandoned:
            explanation = "Buyers started orders but none were paid in the last 30 days."
        elif abandoned > paid:
            explanation = "Abandoned orders exceed paid orders; clarify listings, delivery and checkout expectations."
        else:
            explanation = "Paid orders are keeping pace with recorded abandoned orders."
        return {
            "views_lifetime": int(insights["views"]),
            "orders_30_days": int(insights["orders"]),
            "paid_orders_30_days": paid,
            "abandoned_orders_30_days": abandoned,
            "lifetime_conversion_rate": float(insights["conversion_rate"]),
            "explanation": explanation,
        }

    def _storefront_sales(self, data_owner_id: int) -> dict[int, int]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=30)
        rows = (
            self._db.query(
                models.InvoiceLine.product_id,
                func.sum(models.InvoiceLine.quantity),
            )
            .join(models.Invoice, models.Invoice.id == models.InvoiceLine.invoice_id)
            .join(models.StorefrontOrderEscrow, models.StorefrontOrderEscrow.invoice_id == models.Invoice.id)
            .filter(
                models.StorefrontOrderEscrow.seller_id == data_owner_id,
                models.StorefrontOrderEscrow.status.in_(("held", "released")),
                models.StorefrontOrderEscrow.created_at >= cutoff,
                models.InvoiceLine.product_id.isnot(None),
            )
            .group_by(models.InvoiceLine.product_id)
            .all()
        )
        return {int(product_id): int(units or 0) for product_id, units in rows}

    def _listing_advice(self, product: Product, units_sold: int) -> dict:
        issues: list[str] = []
        score = 0
        if product.image_url:
            score += 25
        else:
            issues.append("Add a clear product photo")
        description_length = len((product.description or "").strip())
        if description_length >= 60:
            score += 25
        elif description_length >= 20:
            score += 15
            issues.append("Add more useful product detail")
        else:
            issues.append("Add a product description")
        if product.category_id:
            score += 15
        else:
            issues.append("Choose a category")
        if product.selling_price > 0:
            score += 15
        else:
            issues.append("Set a valid selling price")
        if not product.track_stock or product.quantity_in_stock > 0:
            score += 10
        else:
            issues.append("Restock before promoting")
        if product.cost_price is not None:
            score += 10
        else:
            issues.append("Add cost price for margin-safe promotions")

        max_safe = self._max_safe_discount(product)
        suggested = 5 if units_sold == 0 and max_safe >= 5 and product.quantity_in_stock > 0 else 0
        if not self._is_listable(product):
            recommendation = "improve_listing"
            explanation = "This product cannot appear publicly until it has both a description and photo."
        elif product.track_stock and product.quantity_in_stock <= 0:
            recommendation = "out_of_stock"
            explanation = (
                "This listing is complete but unavailable; restock requests can be re-engaged after stock returns."
            )
        elif units_sold >= 2:
            recommendation = "feature"
            explanation = f"{units_sold} units sold through paid storefront orders in 30 days; consider featuring it."
        elif suggested:
            recommendation = "promote"
            explanation = (
                f"No paid storefront sales in 30 days. A {suggested}% test discount stays within the "
                f"{max_safe}% margin-safe cap."
            )
        else:
            recommendation = "healthy"
            explanation = (
                "The listing is complete; keep collecting storefront sales before changing its position or price."
            )
        return {
            "product_id": product.id,
            "product_name": product.name,
            "quality_score": score,
            "issues": issues,
            "units_sold_30_days": units_sold,
            "recommendation": recommendation,
            "explanation": explanation,
            "current_discount_percent": product.storefront_discount_percent,
            "max_safe_discount_percent": max_safe,
            "suggested_discount_percent": suggested,
            "featured": product.storefront_featured,
            "bundle_label": product.storefront_bundle_label,
        }

    def _bundle_suggestions(self, data_owner_id: int, products: list[Product]) -> list[dict]:
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=90)
        rows = (
            self._db.query(models.InvoiceLine.invoice_id, models.InvoiceLine.product_id)
            .join(
                models.StorefrontOrderEscrow, models.StorefrontOrderEscrow.invoice_id == models.InvoiceLine.invoice_id
            )
            .filter(
                models.StorefrontOrderEscrow.seller_id == data_owner_id,
                models.StorefrontOrderEscrow.status.in_(("held", "released")),
                models.StorefrontOrderEscrow.created_at >= cutoff,
                models.InvoiceLine.product_id.isnot(None),
            )
            .all()
        )
        by_invoice: dict[int, set[int]] = defaultdict(set)
        for invoice_id, product_id in rows:
            by_invoice[int(invoice_id)].add(int(product_id))
        pairs: Counter[tuple[int, int]] = Counter()
        for product_ids in by_invoice.values():
            pairs.update(itertools.combinations(sorted(product_ids), 2))
        product_map = {product.id: product for product in products if self._is_listable(product)}
        suggestions = []
        for (first_id, second_id), count in pairs.most_common(3):
            first = product_map.get(first_id)
            second = product_map.get(second_id)
            if count < 2 or not first or not second:
                continue
            title = f"{first.name} + {second.name}"
            suggestions.append(
                {
                    "title": title[:120],
                    "product_ids": [first.id, second.id],
                    "product_names": [first.name, second.name],
                    "supporting_orders": count,
                    "reason": f"Bought together in {count} paid storefront orders in the last 90 days.",
                }
            )
        return suggestions

    def _reengagement_drafts(self, owner: models.User, products: list[Product]) -> list[dict]:
        product_map = {product.id: product for product in products}
        notifications = (
            self._db.query(models.StorefrontStockNotification)
            .filter(
                models.StorefrontStockNotification.user_id == owner.id,
                models.StorefrontStockNotification.notified.is_(False),
            )
            .order_by(models.StorefrontStockNotification.created_at)
            .limit(20)
            .all()
        )
        base = (settings.FRONTEND_URL or "").rstrip("/")
        drafts = []
        for notification in notifications:
            product = product_map.get(notification.product_id)
            if not product or (product.track_stock and product.quantity_in_stock <= 0):
                continue
            if not is_window_open(notification.phone):
                continue
            link = f"{base}/store/{owner.storefront_slug}?p={product.id}" if base and owner.storefront_slug else ""
            drafts.append(
                {
                    "notification_id": notification.id,
                    "product_id": product.id,
                    "product_name": product.name,
                    "recipient_masked": f"***{notification.phone[-4:]}",
                    "message": (
                        f"{product.name} is back in stock at {owner.business_name or owner.name}."
                        + (f" View it here: {link}" if link else "")
                        + " You asked to be notified when it returned."
                    ),
                }
            )
        return drafts[:10]

    @staticmethod
    def _profile_score(owner: models.User) -> int:
        checks = (
            bool(owner.storefront_enabled),
            bool(owner.storefront_description),
            bool(owner.logo_url),
            bool(owner.storefront_state),
            bool(owner.storefront_hours),
        )
        return sum(checks) * 20

    @staticmethod
    def _is_listable(product: Product) -> bool:
        return bool(product.is_active and (product.description or "").strip() and product.image_url)

    @staticmethod
    def _max_safe_discount(product: Product) -> int:
        price = Decimal(product.selling_price or 0)
        cost = product.cost_price
        if price <= 0 or cost is None or cost <= 0 or cost >= price:
            return 0
        minimum_sale_price = cost / Decimal("0.80")
        safe_percent = int(math.floor((price - minimum_sale_price) / price * 100))
        return max(0, min(20, safe_percent))

    @staticmethod
    def _deterministic_description(product: Product) -> str:
        category = f" in {product.category.name}" if product.category else ""
        fulfilment = {
            "physical": "Available as a physical item",
            "service": "Available as a service",
            "digital": "Available as a digital product",
        }.get(product.fulfilment_type, "Available")
        return (
            f"{product.name}{category}. {fulfilment} at NGN {float(product.selling_price):,.2f} " f"per {product.unit}."
        )

    @staticmethod
    def _product_out(product: Product) -> dict:
        return {
            "product_id": product.id,
            "product_name": product.name,
            "description": product.description,
            "featured": product.storefront_featured,
            "discount_percent": product.storefront_discount_percent,
            "bundle_label": product.storefront_bundle_label,
        }
