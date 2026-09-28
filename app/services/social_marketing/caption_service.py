"""Auto-generated captions for featured storefront products.

Uses the same OpenAI account/pattern as ocr_service.py, but a cheaper
text-only model since this is short marketing copy, not vision. If the LLM
call fails or OPENAI_API_KEY isn't set, falls back to a simple template so a
caption is always available — posting should never block on an LLM outage.
"""
from __future__ import annotations

import logging
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.core.config import settings
from app.models.inventory_models import Product

logger = logging.getLogger(__name__)

_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_MODEL = "gpt-4o-mini"  # cheap + fast; this is a two-sentence caption, not vision


def build_storefront_link(slug: str, platform: str) -> str:
    """Storefront link tagged for attribution — there's no per-product detail
    page today (the store page lists the whole catalog client-side), so this
    links to the store itself rather than a specific product."""
    params = {
        "utm_source": platform,
        "utm_medium": "social",
        "utm_campaign": "auto_feature",
    }
    return f"{settings.FRONTEND_URL}/store/{slug}?{urlencode(params)}"


def _fallback_caption(product: Product, business_name: str, link: str) -> str:
    price = f"₦{Decimal(product.selling_price):,.0f}"
    return (
        f"✨ {product.name} — {price}\n"
        f"From {business_name}, on SuoOps.\n\n"
        f"Shop now: {link}\n\n"
        f"#SuoOps #ShopNigerian #SmallBusiness"
    )


def generate_caption(product: Product, business_name: str, link: str) -> str:
    """Return a ready-to-post caption including the storefront link."""
    if not settings.OPENAI_API_KEY:
        return _fallback_caption(product, business_name, link)

    price = f"₦{Decimal(product.selling_price):,.0f}"
    prompt = (
        "Write a short, upbeat Instagram/Facebook caption (2-3 sentences max) "
        f"for this product, ending with 3-5 relevant hashtags. Product: "
        f"{product.name}. Description: {product.description or 'N/A'}. "
        f"Price: {price}. Sold by: {business_name} on SuoOps. "
        "Don't invent product details not given. No emoji spam — 1-2 max."
    )
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                _OPENAI_URL,
                headers={
                    "Authorization": f"******",
                    "Content-Type": "application/json",
                },
                json={
                    "model": _MODEL,
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 150,
                    "temperature": 0.7,
                },
            )
            resp.raise_for_status()
            data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return f"{text}\n\nShop now: {link}"
    except Exception:  # noqa: BLE001 — network/parse error, never block posting on it
        logger.warning("Caption generation failed for product %s; using fallback", product.id, exc_info=True)
        return _fallback_caption(product, business_name, link)
