"""
Public storefront: a shareable catalog of a business's inventory.

A business opts in (``storefront_enabled``) and gets a vanity slug. Customers
open ``suoops.com/store/<slug>`` to browse the business's active products.
Read-only for now (browse + contact); online ordering reuses the invoice
"Pay Now" + subaccount flow added elsewhere.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from typing import Annotated
from urllib.parse import quote_plus

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.dependencies import get_data_owner_id
from app.api.rate_limit import limiter
from app.api.routes_auth import get_current_user_id
from app.core.config import settings
from app.db.session import get_db
from app.models import models
from app.models.inventory_models import Product, ProductCategory

logger = logging.getLogger(__name__)

# Authenticated storefront management endpoints.
router = APIRouter()
# Public (unauthenticated) storefront endpoints.
public_router = APIRouter()

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,58}[a-z0-9]$")


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:58] or "shop"


def _unique_slug(db: Session, base: str, user_id: int) -> str:
    """Return a slug unique across users (append -2, -3, ... if needed)."""
    candidate = base
    n = 1
    while True:
        existing = (
            db.query(models.User)
            .filter(models.User.storefront_slug == candidate, models.User.id != user_id)
            .first()
        )
        if not existing:
            return candidate
        n += 1
        candidate = f"{base}-{n}"[:60]


def _listable_product_conditions() -> list:
    """A product is publicly listable only when it's active AND has a description
    AND a photo — the exact rule the store page uses to display items. The
    directory + 'live in search' gate reuse this, so a store counted as live
    always has at least one item a shopper can actually see and buy.
    """
    return [
        Product.is_active.is_(True),
        Product.description.isnot(None),
        Product.description != "",
        Product.image_url.isnot(None),
        Product.image_url != "",
    ]


def _presign(url: str | None, *, expires_in: int = 3600) -> str | None:
    """Presign an S3 URL, optionally with a longer TTL for public/cacheable assets.

    We cache the presigned URL in-process for half the TTL so repeat requests
    (page loads, refreshes) return the SAME URL — browsers and CDNs can then
    cache the image across visits instead of re-fetching it every render.
    """
    if not url:
        return None
    try:
        from app.storage.s3_client import s3_client

        key = s3_client.extract_key_from_url(url)
        if not key:
            return url
        # Serve from the tiny in-process cache when a fresh-enough URL exists.
        import time

        cache_key = (key, expires_in)
        cached = _PRESIGN_CACHE.get(cache_key)
        now = time.time()
        # Keep it stable for HALF the TTL so a browser cache actually hits.
        if cached and cached[1] > now:
            # Refresh recency for LRU eviction.
            _PRESIGN_CACHE.move_to_end(cache_key)
            return cached[0]
        fresh = s3_client.get_presigned_url(key, expires_in=expires_in) or url
        _PRESIGN_CACHE[cache_key] = (fresh, now + max(60, expires_in // 2))
        _PRESIGN_CACHE.move_to_end(cache_key)
        # Bound memory: evict oldest when we exceed the cap. 4k entries ≈ a few MB.
        while len(_PRESIGN_CACHE) > _PRESIGN_CACHE_MAX:
            _PRESIGN_CACHE.popitem(last=False)
        return fresh
    except Exception:  # noqa: BLE001
        pass
    return url


# In-process presign cache (ordered for LRU eviction). Not persisted — a new
# process picks its own URL and browsers just re-fetch once, then cache again.
from collections import OrderedDict

_PRESIGN_CACHE: "OrderedDict[tuple[str, int], tuple[str, float]]" = OrderedDict()
_PRESIGN_CACHE_MAX = 4096
# AWS presigned URLs max out at 7 days; used for the public storefront so image
# URLs stay stable long enough for browsers to reuse them across visits.
_PUBLIC_ASSET_TTL = 7 * 24 * 3600


def _wa_url(user) -> str | None:
    """Public WhatsApp order link for the business (only if phone is verified)."""
    if not (getattr(user, "phone_verified", False) and getattr(user, "phone", None)):
        return None
    digits = re.sub(r"\D", "", user.phone)
    return f"https://wa.me/{digits}" if digits else None


class StorefrontEnableIn(BaseModel):
    slug: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=160)


class StorefrontUpdateIn(BaseModel):
    # Change the store URL slug. Not auto-derived from the business name (that
    # would break links already shared); the seller edits it deliberately.
    slug: str | None = Field(default=None, max_length=60)
    description: str | None = Field(default=None, max_length=160)
    address: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=80)
    state: str | None = Field(default=None, max_length=80)
    # {"0": {"open": "09:00", "close": "18:00"}, ...} — 0=Mon; null day = closed.
    hours: dict | None = None
    announcement: str | None = Field(default=None, max_length=200)
    # Opt-in: let SuoOps feature this store's products on its own Facebook
    # Page + Instagram (a curated daily batch, not every product). Off by
    # default — see User.social_promotion_opt_in.
    social_promotion_opt_in: bool | None = None


class StorefrontOut(BaseModel):
    enabled: bool
    slug: str | None
    link: str | None
    description: str | None = None
    product_count: int = 0
    address: str | None = None
    city: str | None = None
    state: str | None = None
    lat: float | None = None
    lng: float | None = None
    hours: dict | None = None
    announcement: str | None = None
    views: int = 0
    # Owner-facing profile completeness (drives the dashboard nudge).
    has_logo: bool = False
    online_payments: bool = False
    listable_product_count: int = 0
    suggestions: list[str] = Field(default_factory=list)
    social_promotion_opt_in: bool = False


def _storefront_out(db: Session, user) -> StorefrontOut:
    """Build the owner-facing storefront payload from a user row."""
    return StorefrontOut(
        enabled=bool(user.storefront_enabled),
        slug=user.storefront_slug,
        link=_link_for(user.storefront_slug) if user.storefront_enabled else None,
        description=user.storefront_description,
        product_count=_product_count(db, user.id),
        address=user.storefront_address,
        city=user.storefront_city,
        state=user.storefront_state,
        lat=user.storefront_lat,
        lng=user.storefront_lng,
        hours=user.storefront_hours,
        announcement=user.storefront_announcement,
        views=user.storefront_views or 0,
        has_logo=bool(user.logo_url),
        online_payments=bool(getattr(user, "paystack_subaccount_active", False)),
        listable_product_count=_listable_product_count(db, user.id),
        suggestions=_storefront_suggestions(db, user),
        social_promotion_opt_in=bool(user.social_promotion_opt_in),
    )


class StoreOrderItem(BaseModel):
    product_id: int
    quantity: int = Field(ge=1, le=50)


class StoreOrderIn(BaseModel):
    customer_name: str = Field(min_length=1, max_length=100)
    customer_phone: str = Field(min_length=6, max_length=20)
    items: list[StoreOrderItem] = Field(min_length=1, max_length=20)
    # Customer's GPS location (drives the buyer-protection window). Optional at
    # the API layer for backward-compat; the storefront UI captures it via GPS.
    customer_lat: float | None = Field(default=None, ge=-90, le=90)
    customer_lng: float | None = Field(default=None, ge=-180, le=180)
    # Optional landmark / delivery instructions the buyer can add so the
    # business can find them (the GPS pin is the primary delivery detail).
    delivery_note: str | None = Field(default=None, max_length=200)
    # Optional courier selection (buyer-pays-delivery). The server re-quotes and
    # validates the fee for this courier — the client can't set the price.
    delivery_courier_id: str | None = Field(default=None, max_length=60)
    delivery_service_code: str | None = Field(default=None, max_length=60)


def _link_for(slug: str | None) -> str | None:
    return f"{settings.FRONTEND_URL}/store/{slug}" if slug else None


def _product_count(db: Session, user_id: int) -> int:
    """Active products in the user's catalog (what the storefront displays)."""
    return (
        db.query(func.count(Product.id))
        .filter(Product.user_id == user_id, Product.is_active.is_(True))
        .scalar()
    ) or 0


def _listable_product_count(db: Session, user_id: int) -> int:
    """Products a shopper can actually see & buy (active + description + photo)."""
    return (
        db.query(func.count(Product.id))
        .filter(Product.user_id == user_id, *_listable_product_conditions())
        .scalar()
    ) or 0


def _storefront_suggestions(db: Session, user) -> list[str]:
    """Prioritised, friendly nudges to help an owner complete their store.

    Gating items (needed to appear in marketplace search) come first, then
    quality boosts (description, location, hours).
    """
    tips: list[str] = []
    active = _product_count(db, user.id)
    listable = _listable_product_count(db, user.id)
    if not user.logo_url:
        tips.append("Add a shop logo — it's needed to show up in marketplace search.")
    if not getattr(user, "paystack_subaccount_active", False):
        tips.append("Turn on online payments so customers can pay on your store.")
    if listable == 0:
        tips.append("Add at least one product with a photo and a description.")
    elif listable < active:
        hidden = active - listable
        tips.append(
            f"{hidden} product{'s' if hidden != 1 else ''} "
            f"{'are' if hidden != 1 else 'is'} hidden — add a photo and description to show "
            f"{'them' if hidden != 1 else 'it'}."
        )
    if not (user.storefront_description or "").strip():
        tips.append("Add a short description of what you sell — it's needed to appear in search.")
    if not user.storefront_state:
        tips.append("Add your location so nearby shoppers can find you — it's needed to appear in search.")
    if not user.storefront_hours:
        tips.append("Set your opening hours.")
    return tips


_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):[0-5]\d$")


def _clean_hours(hours: dict | None) -> dict | None:
    """Validate/normalise weekly hours to {"0".."6": {open, close}} (0=Mon).

    Storefronts may only open between 07:00 and 18:00 — times outside that range
    are clamped, and days where open is not before close are dropped.
    """
    if not hours:
        return None

    def _clamp(v: str) -> str:
        return "07:00" if v < "07:00" else "18:00" if v > "18:00" else v

    cleaned: dict[str, dict] = {}
    for day, val in hours.items():
        key = str(day)
        if key not in {"0", "1", "2", "3", "4", "5", "6"}:
            continue
        if not isinstance(val, dict):
            continue
        opn, cls = str(val.get("open", "")), str(val.get("close", ""))
        if _TIME_RE.match(opn) and _TIME_RE.match(cls):
            opn, cls = _clamp(opn), _clamp(cls)
            if opn < cls:  # ignore zero/negative-length days
                cleaned[key] = {"open": opn, "close": cls}
    return cleaned or None


def _open_now(hours: dict | None) -> tuple[bool, str | None, str | None]:
    """Return (is_open, today_open, today_close) in Africa/Lagos time."""
    if not hours:
        return (False, None, None)
    from datetime import datetime
    from zoneinfo import ZoneInfo

    now = datetime.now(ZoneInfo("Africa/Lagos"))
    today = hours.get(str(now.weekday()))
    if not today:
        return (False, None, None)
    opn, cls = today.get("open"), today.get("close")
    hm = now.strftime("%H:%M")
    is_open = bool(opn and cls and opn <= hm <= cls)
    return (is_open, opn, cls)


@router.post("/storefront/enable", response_model=StorefrontOut)
def enable_storefront(
    payload: StorefrontEnableIn,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontOut:
    """Enable the public storefront and return the shareable link."""
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if payload.slug:
        slug = payload.slug.strip().lower()
        if not _SLUG_RE.match(slug):
            raise HTTPException(
                status_code=400,
                detail="Slug must be 3–60 chars: lowercase letters, numbers, hyphens.",
            )
    else:
        slug = user.storefront_slug or _slugify(user.business_name or user.name or f"shop-{user.id}")

    slug = _unique_slug(db, slug, user.id)

    user.storefront_slug = slug
    user.storefront_enabled = True
    if payload.description is not None:
        user.storefront_description = payload.description.strip() or None
    db.commit()
    logger.info("Storefront enabled for user %s -> %s", user.id, slug)
    return _storefront_out(db, user)


def _apply_storefront_profile(db: Session, user, payload: StorefrontUpdateIn) -> None:
    """Persist the optional storefront profile fields that were provided."""
    if payload.description is not None:
        user.storefront_description = payload.description.strip() or None
    if payload.address is not None:
        user.storefront_address = payload.address.strip() or None
    if payload.city is not None:
        user.storefront_city = payload.city.strip() or None
    if payload.state is not None:
        user.storefront_state = payload.state.strip() or None
    if payload.hours is not None:
        user.storefront_hours = _clean_hours(payload.hours)
    if payload.announcement is not None:
        user.storefront_announcement = payload.announcement.strip() or None
    if payload.social_promotion_opt_in is not None:
        user.social_promotion_opt_in = payload.social_promotion_opt_in


@router.patch("/storefront", response_model=StorefrontOut)
def update_storefront(
    payload: StorefrontUpdateIn,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontOut:
    """Update the storefront profile (description, location, hours, delivery…)."""
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # Optional store-URL change (validated + made unique). Renaming the business
    # does NOT auto-change the slug, so existing links keep working.
    if payload.slug is not None:
        new_slug = _slugify(payload.slug)
        if not new_slug or new_slug == "shop":
            raise HTTPException(
                status_code=400,
                detail="Choose a valid store link — letters and numbers only.",
            )
        if new_slug != user.storefront_slug:
            user.storefront_slug = _unique_slug(db, new_slug, user.id)
    _apply_storefront_profile(db, user, payload)
    db.commit()
    return _storefront_out(db, user)


@router.get("/storefront", response_model=StorefrontOut)
def get_storefront(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontOut:
    """Return the current storefront status + link for the logged-in business."""
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return _storefront_out(db, user)


class StorefrontLocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float | None = None  # metres, from the GPS fix (informational)


@router.post("/storefront/location", response_model=StorefrontOut)
def set_storefront_location(
    payload: StorefrontLocationIn,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontOut:
    """Save the business's GPS location and derive its state on the SERVER.

    The client sends raw GPS coordinates; we reverse-geocode them ourselves so
    the state used for the escrow same/different-state window is trustworthy and
    can't be spoofed by the client.
    """
    from app.services.geocode_service import reverse_geocode

    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    state, city = reverse_geocode(payload.lat, payload.lng)
    user.storefront_lat = payload.lat
    user.storefront_lng = payload.lng
    if state:
        user.storefront_state = state
    if city:
        user.storefront_city = city
    db.commit()
    logger.info(
        "Storefront location set for user %s (state=%s, city=%s)", user.id, state, city
    )
    return _storefront_out(db, user)

@router.post("/storefront/disable", response_model=StorefrontOut)
def disable_storefront(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontOut:
    """Hide the public storefront (keeps the slug for later re-enable)."""
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    user.storefront_enabled = False
    db.commit()
    return StorefrontOut(enabled=False, slug=user.storefront_slug, link=None)


class ScanToPayOut(BaseModel):
    pay_url: str
    qr_png: str  # data:image/png;base64,... — display, print or share
    barcode: str


def _qr_data_url(data: str) -> str:
    """Render a URL as a scannable QR PNG (base64 data URL)."""
    import base64
    import io

    import qrcode

    qr = qrcode.QRCode(version=1, box_size=10, border=2)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


@router.get("/products/{product_id}/scan-to-pay", response_model=ScanToPayOut)
def product_scan_to_pay(
    product_id: int,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> ScanToPayOut:
    """Generate a scan-to-pay QR code for one product.

    Customers scan it to open the product on the business's storefront and pay
    online. The product's barcode is auto-generated on first use, so the
    business never has to type one. Requires the storefront to be enabled —
    that's where the customer actually pays.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    product = (
        db.query(Product)
        .filter(Product.id == product_id, Product.user_id == current_user_id)
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if not (user.storefront_enabled and user.storefront_slug):
        raise HTTPException(
            status_code=400,
            detail="Turn on your storefront first — that's where customers pay after scanning.",
        )

    # Auto-generate the barcode once, on demand (never manual for the user).
    if not (product.barcode or "").strip():
        import secrets

        product.barcode = "".join(secrets.choice("0123456789") for _ in range(12))
        db.commit()

    pay_url = f"{settings.FRONTEND_URL}/store/{user.storefront_slug}?p={product.id}"
    return ScanToPayOut(pay_url=pay_url, qr_png=_qr_data_url(pay_url), barcode=product.barcode)


class StorefrontQrOut(BaseModel):
    link: str
    qr_png: str  # data:image/png;base64,... — print, display or share


@router.get("/storefront/qr", response_model=StorefrontQrOut)
def storefront_qr(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontQrOut:
    """Shareable QR code that opens the whole storefront when scanned.

    Anyone who scans it lands on the business's public catalog and can browse
    and order. Requires the storefront to be enabled.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if not (user.storefront_enabled and user.storefront_slug):
        raise HTTPException(
            status_code=400,
            detail="Turn on your storefront first to get a shareable QR code.",
        )
    link = _link_for(user.storefront_slug)
    return StorefrontQrOut(link=link, qr_png=_qr_data_url(link))


@router.get("/categories/{category_id}/qr", response_model=StorefrontQrOut)
def category_qr(
    category_id: int,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
) -> StorefrontQrOut:
    """Shareable QR code that opens the storefront filtered to ONE category.

    Print it next to a shelf/section (e.g. "Drinks", "Cooked Food") so a customer
    scans straight to those items and orders. Requires the storefront enabled.
    """
    user = db.query(models.User).filter(models.User.id == current_user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    category = (
        db.query(ProductCategory)
        .filter(
            ProductCategory.id == category_id,
            ProductCategory.user_id == current_user_id,
        )
        .first()
    )
    if not category:
        raise HTTPException(status_code=404, detail="Category not found")
    if not (user.storefront_enabled and user.storefront_slug):
        raise HTTPException(
            status_code=400,
            detail="Turn on your storefront first — that's where customers browse after scanning.",
        )
    link = (
        f"{settings.FRONTEND_URL}/store/{user.storefront_slug}"
        f"?category_id={category.id}"
    )
    return StorefrontQrOut(link=link, qr_png=_qr_data_url(link))


# ── Business-facing storefront order (escrow) status + delivery proof ──

def _delivery_status_label(code: str | None) -> str | None:
    """Friendly, buyer-facing label for a normalized courier status code."""
    from app.services.shipping.shipbubble import status_label

    return status_label(code)


def _escrow_summary(escrow: "models.StorefrontOrderEscrow", buyer: "models.Customer | None") -> dict:
    """Business-safe escrow summary. NEVER includes the buyer-only delivery code."""
    return {
        "status": escrow.status,
        "held": escrow.status == "held",
        "release_due_at": escrow.release_due_at.isoformat() if escrow.release_due_at else None,
        "confirmed_at": escrow.confirmed_at.isoformat() if escrow.confirmed_at else None,
        "delivered_at": (
            escrow.seller_marked_delivered_at.isoformat()
            if escrow.seller_marked_delivered_at
            else None
        ),
        "delivery_proof_note": escrow.delivery_proof_note,
        "delivery_proof_url": _presign(escrow.delivery_proof_url),
        "dispatched_at": (
            escrow.seller_dispatched_at.isoformat()
            if escrow.seller_dispatched_at
            else None
        ),
        "dispatch_tracking": escrow.dispatch_tracking,
        "dispatch_note": escrow.dispatch_note,
        "dispatch_carrier": escrow.dispatch_carrier,
        "dispatch_eta": escrow.dispatch_eta.isoformat() if escrow.dispatch_eta else None,
        "dispatch_tracking_url": escrow.shipbubble_tracking_url,
        "delivery_status": escrow.delivery_status,
        "delivery_status_label": _delivery_status_label(escrow.delivery_status),
        "delivery_courier": escrow.delivery_courier,
        "requires_delivery": bool(getattr(escrow, "requires_delivery", True)),
        "delivery_service_type": escrow.delivery_service_type,
        "delivery_dropoff_station": escrow.delivery_dropoff_station,
        "dispatch_proof_url": _presign(escrow.dispatch_proof_url),
        "held_for_review": bool(escrow.held_for_review),
        "gross_naira": round((escrow.gross_kobo or 0) / 100, 2),
        "payout_naira": round((escrow.payout_kobo or 0) / 100, 2),
        "customer_name": buyer.name if buyer else None,
        # For automated courier deliveries the courier handles buyer contact —
        # don't expose the buyer's phone to the seller.
        "customer_phone": (
            None if escrow.delivery_courier else (buyer.phone if buyer else None)
        ),
    }


def _load_owner_escrow(db: Session, user_id: int, invoice_public_id: str):
    """Return (escrow, buyer) for a storefront order owned by this user, or None."""
    row = (
        db.query(models.StorefrontOrderEscrow, models.Customer)
        .join(models.Invoice, models.StorefrontOrderEscrow.invoice_id == models.Invoice.id)
        .outerjoin(models.Customer, models.Invoice.customer_id == models.Customer.id)
        .filter(
            models.Invoice.invoice_id == invoice_public_id,
            models.StorefrontOrderEscrow.seller_id == user_id,
        )
        .first()
    )
    return row  # (escrow, customer) or None


@router.get("/storefront/orders/{invoice_id}")
def get_order_escrow(
    invoice_id: str,
    data_owner_id: Annotated[int, Depends(get_data_owner_id)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Business: buyer-protection status for one of your storefront orders.

    Scoped to the account owner so invited team members (shared workspace) see
    and manage the same orders, mirroring the invoice endpoints.
    """
    row = _load_owner_escrow(db, data_owner_id, invoice_id)
    if not row:
        return {"escrow": None}
    escrow, buyer = row
    summary = _escrow_summary(escrow, buyer)
    # Buyer/system messages the seller hasn't opened yet (for the unread badge).
    summary["unread_messages"] = (
        db.query(func.count(models.OrderMessage.id))
        .filter(
            models.OrderMessage.escrow_id == escrow.id,
            models.OrderMessage.sender_role != "seller",
            models.OrderMessage.blocked.is_(False),
            models.OrderMessage.read_at.is_(None),
        )
        .scalar()
    ) or 0
    return {"escrow": summary}


async def _save_proof_photo(escrow: "models.StorefrontOrderEscrow", file: "UploadFile", *, prefix: str) -> str:
    """Validate + store a seller proof photo (delivery or dispatch) to S3.

    Shared by the mark-delivered and mark-sent endpoints: enforces image type,
    5MB cap and magic-byte check, then uploads under ``{prefix}/escrow_{id}.{ext}``.
    """
    from app.storage.s3_client import s3_client
    from app.utils.file_validation import get_safe_extension, validate_file_magic_bytes

    allowed = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
    if not file.content_type or file.content_type not in allowed:
        raise HTTPException(status_code=400, detail="Proof must be a PNG, JPG or WEBP image.")
    content = await file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Image exceeds the 5MB limit.")
    if not validate_file_magic_bytes(content, file.content_type):
        raise HTTPException(status_code=400, detail="File content does not match its type.")
    ext = get_safe_extension(file.filename, file.content_type)
    key = f"{prefix}/escrow_{escrow.id}.{ext}"
    return await s3_client.upload_file(content, key, content_type=file.content_type)


@router.post("/storefront/orders/{invoice_id}/mark-delivered")
@limiter.limit("30/minute")
async def mark_order_delivered(
    request: Request,
    invoice_id: str,
    data_owner_id: Annotated[int, Depends(get_data_owner_id)],
    db: Annotated[Session, Depends(get_db)],
    note: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict:
    """Business: mark a storefront order delivered, with an optional proof photo.

    This is your evidence if the buyer later falsely claims non-delivery — it
    does NOT release funds (only the buyer's code or the window does that).
    Scoped to the account owner so invited team members can act on shared orders.
    """
    row = _load_owner_escrow(db, data_owner_id, invoice_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found.")
    escrow, buyer = row

    import datetime as dt

    proof_url = escrow.delivery_proof_url
    if file is not None and file.filename:
        proof_url = await _save_proof_photo(escrow, file, prefix="delivery-proof")

    escrow.seller_marked_delivered_at = dt.datetime.now(dt.timezone.utc)
    if note is not None:
        escrow.delivery_proof_note = note.strip()[:255] or None
    escrow.delivery_proof_url = proof_url
    db.commit()
    db.refresh(escrow)
    logger.info("Seller %s marked order %s delivered", data_owner_id, invoice_id)
    return {"escrow": _escrow_summary(escrow, buyer)}


def _book_courier_pickup(escrow_id: int, invoice_id: str) -> None:
    """Book the Shipbubble courier pickup for a dispatched order, out of band.

    Runs as a background task so the seller's “mark sent” request returns
    immediately instead of blocking on Shipbubble's API (which can take several
    seconds). Best-effort: on failure the order stays sent, just without an
    auto-booked courier.
    """
    import datetime as dt

    from app.db.session import SessionLocal
    from app.services.escrow_service import add_business_days
    from app.services.shipping import shipbubble

    try:
        with SessionLocal() as db:
            escrow = (
                db.query(models.StorefrontOrderEscrow)
                .filter(models.StorefrontOrderEscrow.id == escrow_id)
                .first()
            )
            if not escrow or escrow.shipbubble_order_id:
                return
            if not (
                escrow.delivery_request_token
                and escrow.delivery_courier_id
                and escrow.delivery_service_code
            ):
                return
            booking = shipbubble.create_shipment(
                request_token=escrow.delivery_request_token,
                courier_id=escrow.delivery_courier_id,
                service_code=escrow.delivery_service_code,
            )
            if not (booking and booking.get("order_id")):
                return
            escrow.shipbubble_order_id = str(booking["order_id"])[:60]
            escrow.shipbubble_tracking_url = booking.get("tracking_url") or None
            if booking.get("courier") and not escrow.dispatch_carrier:
                escrow.dispatch_carrier = str(booking["courier"])[:80]
            # Show the buyer the courier is booked and a pickup is pending until
            # the first webhook status arrives.
            if not escrow.delivery_status:
                escrow.delivery_status = "booked"
                escrow.delivery_status_at = dt.datetime.now(dt.timezone.utc)
            # Delivery-aware payout: don't auto-release until the courier reports
            # delivery. Cap the hold at the delivery SLA so a lost parcel gets
            # flagged for review instead of hanging forever.
            escrow.delivery_booked_at = dt.datetime.now(dt.timezone.utc)
            escrow.release_due_at = add_business_days(
                dt.datetime.now(dt.timezone.utc),
                settings.ESCROW_MAX_DELIVERY_DAYS,
            )
            db.commit()
            logger.info(
                "Booked Shipbubble shipment %s for order %s (background)",
                booking["order_id"], invoice_id,
            )
    except Exception:  # noqa: BLE001
        logger.exception("Background Shipbubble booking failed for order %s", invoice_id)


@router.post("/storefront/orders/{invoice_id}/mark-sent")
@limiter.limit("30/minute")
async def mark_order_sent(
    request: Request,
    invoice_id: str,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    data_owner_id: Annotated[int, Depends(get_data_owner_id)],
    db: Annotated[Session, Depends(get_db)],
    background_tasks: BackgroundTasks,
    tracking: Annotated[str | None, Form()] = None,
    note: Annotated[str | None, Form()] = None,
    carrier: Annotated[str | None, Form()] = None,
    eta: Annotated[str | None, Form()] = None,
    file: Annotated[UploadFile | None, File()] = None,
) -> dict:
    """Business: mark a storefront order SENT OUT (dispatched), with an optional
    courier/waybill tracking code, courier name, expected delivery date, and a
    photo of the packaged item.

    This is seller protection: timestamped proof you shipped a quality item,
    before the buyer confirms delivery. It also tells the buyer who's bringing
    their order and when to expect it. It does NOT release funds.

    Scoped to the account owner (data_owner_id) so invited team members can act
    on shared orders; current_user_id is still logged as the actual actor.
    """
    row = _load_owner_escrow(db, data_owner_id, invoice_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found.")
    escrow, buyer = row

    import datetime as dt
    import time

    logger.info(
        "mark-sent request: order=%s seller=%s has_file=%s",
        invoice_id, current_user_id, bool(file is not None and file.filename),
    )

    # A photo of the packaged item is REQUIRED — it's the proof of quality and
    # shipment (and the buyer sees it), so a "sent out" mark is never empty.
    if not (file is not None and file.filename) and not escrow.dispatch_proof_url:
        raise HTTPException(
            status_code=400, detail="A photo of the packaged item is required to mark it sent out."
        )

    proof_url = escrow.dispatch_proof_url
    if file is not None and file.filename:
        _t0 = time.monotonic()
        proof_url = await _save_proof_photo(escrow, file, prefix="dispatch-proof")
        logger.info(
            "mark-sent photo stored for %s in %.2fs", invoice_id, time.monotonic() - _t0
        )

    escrow.seller_dispatched_at = dt.datetime.now(dt.timezone.utc)
    if tracking is not None:
        escrow.dispatch_tracking = tracking.strip()[:120] or None
    if note is not None:
        escrow.dispatch_note = note.strip()[:255] or None
    if carrier is not None:
        escrow.dispatch_carrier = carrier.strip()[:80] or None
    if eta is not None:
        # Accept an ISO date (YYYY-MM-DD); ignore anything unparseable.
        raw_eta = eta.strip()
        if raw_eta:
            try:
                escrow.dispatch_eta = dt.date.fromisoformat(raw_eta[:10])
            except ValueError:
                pass
        else:
            escrow.dispatch_eta = None
    escrow.dispatch_proof_url = proof_url
    db.commit()
    db.refresh(escrow)

    # Tell the buyer their order is on the way (system notice in the thread).
    try:
        parts = ["📦 Your order has been sent out."]
        if escrow.dispatch_carrier:
            parts.append(f"Courier: {escrow.dispatch_carrier}.")
        if escrow.dispatch_tracking:
            parts.append(f"Tracking: {escrow.dispatch_tracking}.")
        if escrow.dispatch_eta:
            parts.append(
                f"Expected delivery: {escrow.dispatch_eta.strftime('%a %d %b %Y')}."
            )
        _store_system_message(db, escrow, " ".join(parts))
    except Exception:  # noqa: BLE001
        logger.exception("Failed to post dispatch notice for order %s", invoice_id)

    # Book the courier pickup out of band so this request returns immediately —
    # the Shipbubble API call can take several seconds and must not block the
    # seller's “mark sent” action.
    if (
        settings.SHIPBUBBLE_CHECKOUT_ENABLED
        and escrow.delivery_request_token
        and escrow.delivery_courier_id
        and escrow.delivery_service_code
        and not escrow.shipbubble_order_id
    ):
        background_tasks.add_task(_book_courier_pickup, escrow.id, invoice_id)

    logger.info("Seller %s marked order %s sent out", current_user_id, invoice_id)
    return {"escrow": _escrow_summary(escrow, buyer)}


@public_router.get("/store/{slug}")
@limiter.limit("30/minute")
def get_public_storefront(request: Request, slug: str, db: Annotated[Session, Depends(get_db)]) -> dict:
    """Public: a business's shareable inventory catalog."""
    from sqlalchemy.orm import joinedload

    owner = (
        db.query(models.User)
        .filter(models.User.storefront_slug == slug.lower())
        .first()
    )
    if not owner:
        raise HTTPException(status_code=404, detail="Storefront not found")

    # Slug exists but the store isn't live — the owner hasn't enabled it yet, or
    # it's under moderation. Return a graceful "offline" payload (HTTP 200) so a
    # shared link degrades to a friendly message instead of a dead 404.
    if not owner.storefront_enabled or owner.store_status != "active":
        reason = owner.store_status if owner.store_status in ("suspended", "delisted") else "disabled"
        return {
            "slug": slug.lower(),
            "business_name": owner.business_name or owner.name,
            "offline": True,
            "offline_reason": reason,
        }

    # Discovery analytics: count this view (best-effort, never blocks the page).
    try:
        owner.storefront_views = (owner.storefront_views or 0) + 1
        db.commit()
    except Exception:  # noqa: BLE001
        db.rollback()

    products = (
        db.query(Product)
        .options(joinedload(Product.category))
        .filter(
            Product.user_id == owner.id,
            # Buyer protection: only list items that show a description AND a
            # photo, so buyers (and dispute reviews) can see exactly what was
            # ordered. Same rule as the live-search gate
            # (_listable_product_conditions) so a listed store is never empty.
            *_listable_product_conditions(),
        )
        .order_by(Product.name.asc())
        .all()
    )

    is_open, open_from, open_to = _open_now(owner.storefront_hours)

    reviews = (
        db.query(models.StorefrontReview)
        .filter(
            models.StorefrontReview.user_id == owner.id,
            models.StorefrontReview.approved.is_(True),
        )
        .all()
    )
    review_count = len(reviews)
    review_avg = round(sum(r.rating for r in reviews) / review_count, 1) if review_count else None

    address_parts = [owner.storefront_address, owner.storefront_city, owner.storefront_state]
    full_address = ", ".join(p for p in address_parts if p) or None

    return {
        "slug": slug.lower(),
        "business_name": owner.business_name or owner.name,
        "description": owner.storefront_description,
        "logo_url": _presign(owner.logo_url, expires_in=_PUBLIC_ASSET_TTL),
        "storefront_cover_url": _presign(
            owner.storefront_cover_url, expires_in=_PUBLIC_ASSET_TTL
        ),
        "online_payments_enabled": bool(
            owner.paystack_subaccount_active and owner.paystack_subaccount_code
        ),
        "whatsapp_url": _wa_url(owner),
        "announcement": owner.storefront_announcement,
        "location": {
            "address": full_address,
            "city": owner.storefront_city,
            "state": owner.storefront_state,
            "maps_url": (
                f"https://www.google.com/maps/search/?api=1&query="
                f"{quote_plus(full_address)}"
                if full_address
                else None
            ),
        },
        "hours": owner.storefront_hours,
        "open_now": is_open,
        "open_from": open_from,
        "open_to": open_to,
        "reviews": {"count": review_count, "average": review_avg},
        "products": [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "price": float(p.selling_price) if p.selling_price is not None else None,
                "unit": p.unit,
                "category": p.category.name if p.category else None,
                "category_id": p.category_id,
                "image_url": _presign(p.image_url, expires_in=_PUBLIC_ASSET_TTL),
                "in_stock": (not p.track_stock) or (p.quantity_in_stock > 0),
                "fulfilment_type": getattr(p, "fulfilment_type", "physical"),
                # Category pack fee (₦) — one flat pack is added to an order that
                # contains any packaged item; the frontend shows it in the total.
                "pack_price": (
                    float(p.category.pack_price)
                    if p.category and p.category.pack_price
                    else None
                ),
            }
            for p in products
        ],
    }


def live_storefronts_query(db: Session):
    """Query for storefronts that are actually LIVE in the public directory.

    "Live" means a shopper can find the store via the global marketplace search.
    A store must be opted-in, not suspended/delisted, and have ALL of:
      • a logo
      • online payments (active Paystack subaccount)
      • at least one shopper-visible product (active + photo + description)
      • a store description
      • a location (state, captured via GPS)
    ``list_public_stores`` REUSES this exact query, so the admin "Live in search"
    metric and what customers actually see on the landing page can never differ.
    """
    product_owner_ids = (
        db.query(Product.user_id).filter(*_listable_product_conditions()).distinct().subquery()
    )
    return db.query(models.User).filter(
        models.User.storefront_enabled.is_(True),
        models.User.store_status == "active",
        models.User.storefront_slug.isnot(None),
        models.User.logo_url.isnot(None),
        models.User.paystack_subaccount_active.is_(True),
        models.User.storefront_description.isnot(None),
        models.User.storefront_description != "",
        models.User.storefront_state.isnot(None),
        models.User.storefront_state != "",
        models.User.id.in_(db.query(product_owner_ids)),
    )


def count_live_storefronts(db: Session) -> int:
    """Number of storefronts visible in the public marketplace/global search."""
    return live_storefronts_query(db).with_entities(func.count(models.User.id)).scalar() or 0


@public_router.get("/stores")
@limiter.limit("30/minute")
def list_public_stores(
    request: Request,
    db: Annotated[Session, Depends(get_db)],
    page: int = 1,
    page_size: int = 24,
    q: str | None = None,
) -> dict:
    """Public marketplace directory + global search across ALL stores.

    Trust gate: only businesses that opted in AND have a logo AND verified bank
    (active Paystack subaccount) AND a shopper-visible product AND a description
    AND a location are listed — the SAME gate as the admin "Live in search"
    metric (``live_storefronts_query``). When ``q`` is given it searches business
    name, description, city/state and product names + categories across every
    store, so a shopper can find an item and pick which store to buy it from.
    """
    from sqlalchemy import or_

    from app.models.inventory_models import ProductCategory

    page = max(1, page)
    page_size = min(max(1, page_size), 48)
    term = (q or "").strip()

    # Single source of truth: the exact same gate as the "Live in search" count,
    # so the directory and the admin metric always agree.
    base = live_storefronts_query(db)

    if term:
        like = f"%{term}%"
        product_match_ids = (
            db.query(Product.user_id)
            .outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
            .filter(
                Product.is_active.is_(True),
                or_(Product.name.ilike(like), ProductCategory.name.ilike(like)),
            )
            .distinct()
            .subquery()
        )
        base = base.filter(
            or_(
                models.User.business_name.ilike(like),
                models.User.name.ilike(like),
                models.User.storefront_description.ilike(like),
                models.User.storefront_city.ilike(like),
                models.User.storefront_state.ilike(like),
                models.User.id.in_(db.query(product_match_ids)),
            )
        )

    total = base.with_entities(func.count(models.User.id)).scalar() or 0
    owners = (
        base.order_by(models.User.storefront_slug.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    # For search results, surface up to 3 matching product names per store.
    matched: dict[int, list[str]] = {}
    if term and owners:
        like = f"%{term}%"
        rows = (
            db.query(Product.user_id, Product.name)
            .outerjoin(ProductCategory, Product.category_id == ProductCategory.id)
            .filter(
                Product.user_id.in_([o.id for o in owners]),
                Product.is_active.is_(True),
                or_(Product.name.ilike(like), ProductCategory.name.ilike(like)),
            )
            .all()
        )
        for uid, pname in rows:
            lst = matched.setdefault(uid, [])
            if len(lst) < 3 and pname not in lst:
                lst.append(pname)

    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "query": term or None,
        "stores": [
            {
                "slug": o.storefront_slug,
                "business_name": o.business_name or o.name,
                "logo_url": _presign(o.logo_url),
                "description": o.storefront_description,
                "location": ", ".join(
                    p for p in [o.storefront_city, o.storefront_state] if p
                )
                or None,
                "matched_products": matched.get(o.id, []),
            }
            for o in owners
        ],
    }


@public_router.post("/store/{slug}/delivery-quote")
@limiter.limit("10/minute")
async def store_delivery_quote(
    request: Request,
    slug: str,
    payload: StoreOrderIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: live courier delivery options for a prospective order (buyer pays
    delivery). Returns ``{"enabled": False, "options": []}`` unless the Shipbubble
    integration is switched on with a key + funded wallet — so the manual dispatch
    flow is the default and nothing here can break checkout.

    Cost-abuse hardened: identical quotes are cached briefly and per-store fresh
    fetches are capped daily, so this public endpoint can't be used to burn the
    Shipbubble quota (or as a free address-validation oracle).
    """
    from app.services.shipping import quote_cache, shipbubble

    if not shipbubble.enabled():
        return {"enabled": False, "options": []}

    slug_l = slug.lower()
    cart_sig = ",".join(
        sorted(f"{it.product_id}:{it.quantity}" for it in payload.items)
    )
    # 1) Serve an identical recent quote from cache (no Shipbubble calls).
    cached = quote_cache.get_cached(
        slug_l, payload.customer_lat, payload.customer_lng, cart_sig
    )
    if cached is not None:
        return cached

    owner = (
        db.query(models.User)
        .filter(
            models.User.storefront_slug == slug_l,
            models.User.storefront_enabled.is_(True),
            models.User.store_status == "active",
        )
        .first()
    )
    if not owner:
        raise HTTPException(status_code=404, detail="Storefront not found")

    # 2) Per-store daily cap on fresh (uncached) quotes — abuse ceiling.
    if not quote_cache.store_quota_ok(slug_l):
        logger.warning("Delivery-quote daily cap hit for store %s", slug_l)
        return {"enabled": True, "options": []}

    quote = _shipbubble_quote(db, owner, payload) or {}
    result = {
        "enabled": True,
        "request_token": quote.get("request_token"),
        "options": [o.as_dict() for o in quote.get("options", [])],
    }
    quote_cache.set_cached(
        slug_l, payload.customer_lat, payload.customer_lng, cart_sig, result
    )
    return result


def _shipbubble_quote(db: Session, owner: "models.User", payload: "StoreOrderIn"):
    """Validate both addresses and fetch live courier rates for this order.
    Returns ``{"request_token": str|None, "options": [DeliveryOption]}`` or None
    when the integration is off. Shared by the quote endpoint and checkout so the
    fee charged is always a fresh, server-verified rate (never a client value)."""
    from app.services.shipping import shipbubble

    if not shipbubble.enabled():
        return None

    from app.services.geocode_service import reverse_geocode_address

    # Shipbubble wants a DETAILED address, not "city, state". Use the seller's
    # pinned GPS reverse-geocoded to a street address where possible.
    seller_addr = None
    if owner.storefront_lat is not None and owner.storefront_lng is not None:
        seller_addr = reverse_geocode_address(owner.storefront_lat, owner.storefront_lng)
    if not seller_addr:
        seller_addr = ", ".join(
            p for p in [owner.storefront_city, owner.storefront_state, "Nigeria"] if p
        )
    sender_code = shipbubble.validate_address(
        name=shipbubble.clean_name(owner.business_name or owner.name, pad="Store"),
        email=owner.email or "store@suoops.com",
        phone=owner.phone or "",
        address=seller_addr or "Nigeria",
        latitude=owner.storefront_lat,
        longitude=owner.storefront_lng,
    )
    buyer_digits = "".join(ch for ch in payload.customer_phone if ch.isdigit())
    # Buyer's detailed delivery address: reverse-geocode their GPS pin (the same
    # street address we show the seller), plus any landmark note.
    buyer_addr = None
    if payload.customer_lat is not None and payload.customer_lng is not None:
        buyer_addr = reverse_geocode_address(payload.customer_lat, payload.customer_lng)
    buyer_addr = (
        ", ".join(p for p in [buyer_addr, payload.delivery_note] if p)
        or "customer location"
    )
    receiver_code = shipbubble.validate_address(
        name=shipbubble.clean_name(payload.customer_name, pad="Buyer"),
        email=f"{buyer_digits or 'buyer'}@buyer.suoops.com",
        phone=payload.customer_phone,
        address=buyer_addr,
        latitude=payload.customer_lat,
        longitude=payload.customer_lng,
    )
    if not (sender_code and receiver_code):
        return {"request_token": None, "options": []}

    prods = {
        p.id: p
        for p in db.query(Product)
        .filter(Product.id.in_([it.product_id for it in payload.items]))
        .all()
    }
    package_items = []
    for it in payload.items:
        p = prods.get(it.product_id)
        package_items.append(
            {
                "name": (getattr(p, "name", None) or "Item")[:60],
                "description": "order item",
                "unit_weight": str(getattr(p, "weight_kg", None) or 0.5),
                "unit_amount": str(int(getattr(p, "selling_price", 0) or 0)),
                "quantity": str(it.quantity),
            }
        )

    rates = shipbubble.fetch_rates(
        sender_address_code=sender_code,
        receiver_address_code=receiver_code,
        package_items=package_items,
    )
    if not rates:
        return {"request_token": None, "options": []}

    # Same-state deliveries should be fast: when the buyer is in the seller's
    # state, only offer couriers that deliver within 24h (same-day / N-hour
    # services), hiding multi-'working day' options. Fall back to all couriers
    # if none qualify, so the buyer is never left with zero delivery choices.
    if settings.STOREFRONT_SAME_STATE_FAST_ONLY and rates.get("options"):
        if _quote_is_same_state(owner, payload):
            fast = [o for o in rates["options"] if shipbubble.within_hours(o, 24)]
            if fast:
                rates["options"] = fast
    return rates


def _quote_is_same_state(owner: "models.User", payload: "StoreOrderIn") -> bool:
    """True when the buyer's pinned location is in the seller's storefront state."""
    seller_state = getattr(owner, "storefront_state", None)
    if not seller_state or payload.customer_lat is None or payload.customer_lng is None:
        return False
    from app.services.delivery_zones import same_state
    from app.services.geocode_service import reverse_geocode

    buyer_state, _ = reverse_geocode(payload.customer_lat, payload.customer_lng)
    return same_state(seller_state, buyer_state)


@public_router.post("/store/{slug}/order")
@limiter.limit("20/hour")
async def create_store_order(
    request: Request,
    slug: str,
    payload: StoreOrderIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: place an online order from a storefront.

    Creates a pending, online-only invoice for the business (no invoice balance
    consumed) and returns a Paystack pay link. Storefront orders can only be
    paid online — that is how the platform earns its commission.
    """
    owner = (
        db.query(models.User)
        .filter(
            models.User.storefront_slug == slug.lower(),
            models.User.storefront_enabled.is_(True),
            models.User.store_status == "active",
        )
        .first()
    )
    if not owner:
        raise HTTPException(status_code=404, detail="Storefront not found")

    if not (owner.paystack_subaccount_active and owner.paystack_subaccount_code):
        raise HTTPException(
            status_code=409, detail="This store isn't accepting online orders yet."
        )

    ids = [i.product_id for i in payload.items]
    from sqlalchemy.orm import joinedload as _joinedload

    products = (
        db.query(Product)
        # Eager-load category so the pack_price + fulfilment_type loops below
        # don't fire an N+1 SELECT per item.
        .options(_joinedload(Product.category))
        .filter(
            Product.user_id == owner.id,
            Product.id.in_(ids),
            Product.is_active.is_(True),
            # Only complete (described + photographed) items are orderable.
            Product.description.isnot(None),
            Product.description != "",
            Product.image_url.isnot(None),
            Product.image_url != "",
        )
        .all()
    )
    pmap = {p.id: p for p in products}

    lines: list[dict] = []
    total = Decimal("0")
    for item in payload.items:
        product = pmap.get(item.product_id)
        if not product:
            raise HTTPException(status_code=400, detail="One or more products are unavailable.")
        if product.track_stock and product.quantity_in_stock < item.quantity:
            raise HTTPException(
                status_code=400,
                detail=f"{product.name}: only {product.quantity_in_stock} in stock.",
            )
        price = product.selling_price or Decimal("0")
        lines.append(
            {
                "description": product.name,
                "quantity": item.quantity,
                "unit_price": price,
                "product_id": product.id,
            }
        )
        total += price * item.quantity

    if total <= 0:
        raise HTTPException(status_code=400, detail="Order total must be greater than zero.")

    # Automatic packaging fee: ONE flat pack per order. If the cart contains any
    # product whose category carries a pack price, add a single "Packaging" line
    # using the highest pack price among those items (the biggest pack covers the
    # whole order). It's part of the seller's goods total, so the seller is paid
    # for it — the buyer never has to remember to add it themselves.
    pack_price = Decimal("0")
    for item in payload.items:
        prod = pmap.get(item.product_id)
        cat = getattr(prod, "category", None) if prod else None
        cat_pack = getattr(cat, "pack_price", None) if cat else None
        if cat_pack and Decimal(cat_pack) > pack_price:
            pack_price = Decimal(cat_pack)
    if pack_price > 0:
        lines.append(
            {
                "description": "Packaging",
                "quantity": 1,
                "unit_price": pack_price,
                "product_id": None,
            }
        )
        total += pack_price

    # Service/digital products aren't shipped. An order made up ENTIRELY of them
    # is a "no-delivery" order: no delivery address, no courier, and a faster
    # buyer-protection window. Any physical item makes it a normal delivery order.
    no_delivery = all(
        getattr(pmap.get(i.product_id), "fulfilment_type", "physical") != "physical"
        for i in payload.items
    )

    # Delivery address is REQUIRED for physical orders. The buyer's GPS pin may
    # not be where they want delivery (they could be ordering from elsewhere),
    # so a typed address + landmark is mandatory for the seller/courier to
    # deliver to the right place. Service/digital orders skip this entirely.
    if not no_delivery and (
        not payload.delivery_note or len(payload.delivery_note.strip()) < 4
    ):
        raise HTTPException(
            status_code=400,
            detail="Please add your delivery address and a landmark.",
        )

    from app.core.admin_security import get_client_ip
    from app.services.escrow_service import (
        create_order_escrow,
        detect_order_collusion,
        is_trusted_seller,
        seller_velocity_hold_reason,
    )

    # Untrusted sellers settle via escrow (hold-&-release); trusted sellers
    # settle normally. A physical-courier order overrides this below and ALWAYS
    # holds (so the delivery fee is retained and the courier lifecycle applies).
    untrusted = settings.ESCROW_ENABLED and not is_trusted_seller(db, owner)

    # Blast-radius caps for UNTRUSTED sellers: cap per-order value and the total
    # value held in-flight, so a scam/hijacked store can only ever touch so much.
    if untrusted:
        order_kobo = int(total * 100)
        if order_kobo > settings.ESCROW_MAX_ORDER_NAIRA_UNTRUSTED * 100:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This store can't accept orders above ₦"
                    f"{settings.ESCROW_MAX_ORDER_NAIRA_UNTRUSTED:,} yet. "
                    "Please contact them directly for large orders."
                ),
            )
        inflight_kobo = (
            db.query(func.coalesce(func.sum(models.StorefrontOrderEscrow.gross_kobo), 0))
            .filter(
                models.StorefrontOrderEscrow.seller_id == owner.id,
                # Only PAID/held money counts toward the cap. Counting unpaid
                # "pending" rows would let a buyer spam abandoned orders to fill
                # the cap and block the seller from accepting real orders (DoS).
                models.StorefrontOrderEscrow.status == "held",
            )
            .scalar()
        ) or 0
        if inflight_kobo + order_kobo > settings.ESCROW_MAX_INFLIGHT_NAIRA_UNTRUSTED * 100:
            raise HTTPException(
                status_code=409,
                detail="This store has too many pending orders right now — please try again later.",
            )

    from app.services.invoice_payment_service import (
        PaymentInitError,
        start_invoice_payment,
    )
    from app.services.invoice_service import build_invoice_service

    # Buyer-pays-delivery: re-quote the chosen courier server-side so the fee
    # can't be tampered, and add it to the amount charged. The delivery fee is
    # retained by SuoOps to fund the courier — it is NOT part of the seller's
    # goods total, so the seller is always paid on goods only. This is computed
    # for EVERY seller (trusted or not); a courier order then forces the hold
    # path below so the fee is never split to the seller.
    delivery_fee = Decimal("0")
    delivery_sel: dict | None = None
    if settings.SHIPBUBBLE_CHECKOUT_ENABLED and payload.delivery_courier_id and not no_delivery:
        quote = _shipbubble_quote(db, owner, payload) or {}
        token = quote.get("request_token")
        chosen = next(
            (
                o
                for o in quote.get("options", [])
                if str(o.courier_id) == str(payload.delivery_courier_id)
                and (
                    not payload.delivery_service_code
                    or o.service_code == payload.delivery_service_code
                )
            ),
            None,
        )
        if not (token and chosen):
            raise HTTPException(
                status_code=409,
                detail="That delivery option is no longer available — please pick a courier again.",
            )
        delivery_fee = Decimal(str(chosen.amount))
        station = chosen.dropoff_station or {}
        station_str = None
        if station:
            station_str = " — ".join(
                s for s in [station.get("name"), station.get("address")] if s
            )
            if station.get("phone"):
                station_str = f"{station_str} ({station['phone']})"
        delivery_sel = {
            "token": token,
            "courier_id": str(chosen.courier_id),
            "service_code": str(chosen.service_code),
            "courier": chosen.name,
            "service_type": chosen.service_type,
            "station": (station_str or None),
        }

    # EVERY storefront order settles through escrow (hold-&-release) — there is
    # NO instant payout to sellers on storefront orders, trusted or not. This
    # keeps buyer protection on every order and means the delivery fee is always
    # held (never split to the seller); the Paystack subaccount split is never
    # used for storefront checkout. (`untrusted` above still governs the
    # blast-radius caps — trusted sellers hold too, but keep their higher limits.)
    held = settings.ESCROW_ENABLED

    # The seller's invoice records the GOODS value only (P). The platform service
    # fee and any delivery are charged to the BUYER on top at the payment layer,
    # so the seller's revenue/tax figures (which read invoice.amount) are never
    # inflated by our fee. Buyer's total charge = goods + service fee + delivery.
    from app.utils.feature_gate import platform_fee_kobo

    service_fee_kobo = platform_fee_kobo(total)
    charge_kobo = int(total * 100) + service_fee_kobo + int(delivery_fee * 100)

    svc = build_invoice_service(db)
    invoice = svc.create_invoice(
        owner.id,
        {
            "customer_name": payload.customer_name.strip(),
            "customer_phone": payload.customer_phone.strip(),
            "amount": total,
            "currency": "NGN",
            "lines": lines,
            "channel": "storefront",
        },
        async_pdf=True,
        consume_balance=False,
    )

    # Surface delivery details to the business on the order/invoice. We send the
    # location as readable TEXT (reverse-geocoded address) so the seller can read
    # it in their notification, plus the GPS pin (a Google Maps link) for
    # turn-by-turn navigation. The buyer can add an optional landmark note.
    delivery_lines: list[str] = []
    if payload.customer_lat is not None and payload.customer_lng is not None:
        from app.services.geocode_service import reverse_geocode_address

        address = reverse_geocode_address(payload.customer_lat, payload.customer_lng)
        if address:
            delivery_lines.append(f"📍 Deliver to: {address}")
        delivery_lines.append(
            f"Map: https://www.google.com/maps?q="
            f"{payload.customer_lat},{payload.customer_lng}"
        )
    note = (payload.delivery_note or "").strip()
    if note:
        delivery_lines.append(f"Landmark/note: {note}")
    if delivery_lines:
        header = "Service details" if no_delivery else "Storefront delivery"
        invoice.notes = header + "\n" + "\n".join(delivery_lines)
        db.commit()

    try:
        pay = await start_invoice_payment(
            db, invoice, owner, hold=held, charge_amount_kobo=charge_kobo
        )
    except PaymentInitError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

    # Only held orders get an escrow row (trusted sellers settle normally via
    # the subaccount split). Never let escrow bookkeeping break the order flow.
    delivery_code: str | None = None
    if held:
        try:
            # Self-dealing detection: buyer sharing the seller's IP / sitting on
            # the seller's own location → hold for admin review, never auto-release.
            review_reason = detect_order_collusion(
                owner,
                buyer_ip=get_client_ip(request),
                customer_lat=payload.customer_lat,
                customer_lng=payload.customer_lng,
                buyer_phone=payload.customer_phone,
            )
            # Velocity guard: recent settled volume / dispute rate also holds for
            # review (catches laundering spread across days).
            velocity_reason = seller_velocity_hold_reason(db, owner, total)
            review_reason = ", ".join(
                r for r in (review_reason, velocity_reason) if r
            ) or None
            if review_reason:
                review_reason = review_reason[:120]
            escrow = create_order_escrow(
                db,
                invoice=invoice,
                seller=owner,
                gross_naira=total,
                customer_lat=payload.customer_lat,
                customer_lng=payload.customer_lng,
                review_reason=review_reason,
                no_delivery=no_delivery,
            )
            delivery_code = escrow.confirmation_code
            if delivery_sel:
                # Retain the buyer's delivery fee to fund the courier; store the
                # selection so the shipment can be booked at "mark as sent".
                escrow.delivery_fee_kobo = int(delivery_fee * 100)
                escrow.delivery_courier = delivery_sel["courier"][:80]
                escrow.delivery_service_type = (delivery_sel.get("service_type") or None)
                escrow.delivery_dropoff_station = (
                    (delivery_sel.get("station") or None) and delivery_sel["station"][:300]
                )
                escrow.delivery_request_token = delivery_sel["token"][:200]
                escrow.delivery_courier_id = delivery_sel["courier_id"][:60]
                escrow.delivery_service_code = delivery_sel["service_code"][:60]
                db.commit()
            if review_reason:
                owner.flagged_for_review = True
                db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to create escrow hold for order %s", invoice.invoice_id)

    logger.info(
        "Storefront order %s created for store %s (user %s, held=%s)",
        invoice.invoice_id, slug, owner.id, held,
    )
    resp = {"invoice_id": invoice.invoice_id, **pay}
    if delivery_code:
        resp["delivery_code"] = delivery_code
    if service_fee_kobo > 0:
        resp["service_fee"] = float(service_fee_kobo) / 100
    if delivery_fee > 0:
        resp["delivery_fee"] = float(delivery_fee)
    return resp


def _lookup_store(db: Session, slug: str):
    owner = (
        db.query(models.User)
        .filter(
            models.User.storefront_slug == slug.lower(),
            models.User.storefront_enabled.is_(True),
            models.User.store_status == "active",
        )
        .first()
    )
    if not owner:
        raise HTTPException(status_code=404, detail="Storefront not found")
    return owner


class StockNotifyIn(BaseModel):
    product_id: int
    phone: str = Field(min_length=6, max_length=20)


@public_router.post("/store/{slug}/notify")
@limiter.limit("10/hour")
def notify_when_in_stock(
    request: Request,
    slug: str,
    payload: StockNotifyIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: capture a phone number to alert when a sold-out item returns."""
    owner = _lookup_store(db, slug)
    product = (
        db.query(Product)
        .filter(
            Product.id == payload.product_id,
            Product.user_id == owner.id,
            Product.is_active.is_(True),
        )
        .first()
    )
    if not product:
        raise HTTPException(status_code=404, detail="Product not found")

    if (not product.track_stock) or (product.quantity_in_stock > 0):
        return {"ok": True, "message": "Good news — this item is available now."}

    phone = payload.phone.strip()
    exists = (
        db.query(models.StorefrontStockNotification)
        .filter(
            models.StorefrontStockNotification.product_id == product.id,
            models.StorefrontStockNotification.phone == phone,
            models.StorefrontStockNotification.notified.is_(False),
        )
        .first()
    )
    if not exists:
        db.add(
            models.StorefrontStockNotification(
                user_id=owner.id, product_id=product.id, phone=phone
            )
        )
        db.commit()
    return {"ok": True, "message": "We'll text you when it's back in stock."}


class ReviewIn(BaseModel):
    phone: str = Field(min_length=6, max_length=20)
    rating: int = Field(ge=1, le=5)
    text: str | None = Field(default=None, max_length=500)


@public_router.post("/store/{slug}/review")
@limiter.limit("10/hour")
def submit_review(
    request: Request,
    slug: str,
    payload: ReviewIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: leave a review — gated to customers who actually paid this store."""
    from app.utils.phone import normalize_phone

    owner = _lookup_store(db, slug)
    normalized = normalize_phone(payload.phone.strip())
    candidates = {payload.phone.strip(), normalized}

    paid = (
        db.query(models.Invoice)
        .join(models.Customer, models.Invoice.customer_id == models.Customer.id)
        .filter(
            models.Invoice.issuer_id == owner.id,
            models.Invoice.status == "paid",
            models.Customer.phone.in_(candidates),
        )
        .order_by(models.Invoice.id.desc())
        .first()
    )
    if not paid:
        raise HTTPException(
            status_code=403,
            detail="Only customers who've completed a paid order here can leave a review.",
        )

    customer = paid.customer
    existing = (
        db.query(models.StorefrontReview)
        .filter(
            models.StorefrontReview.user_id == owner.id,
            models.StorefrontReview.customer_id == customer.id,
        )
        .first()
    )
    text = (payload.text or "").strip() or None
    if existing:
        existing.rating = payload.rating
        existing.text = text
    else:
        db.add(
            models.StorefrontReview(
                user_id=owner.id,
                customer_id=customer.id,
                rating=payload.rating,
                text=text,
                reviewer_name=(customer.name or "Customer")[:100],
            )
        )
    db.commit()
    return {"ok": True, "message": "Thanks for your review!"}


@public_router.get("/store/{slug}/reviews")
@limiter.limit("30/minute")
def list_reviews(
    request: Request,
    slug: str,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: approved reviews for a storefront."""
    owner = _lookup_store(db, slug)
    rows = (
        db.query(models.StorefrontReview)
        .filter(
            models.StorefrontReview.user_id == owner.id,
            models.StorefrontReview.approved.is_(True),
        )
        .order_by(models.StorefrontReview.created_at.desc())
        .limit(50)
        .all()
    )
    count = len(rows)
    average = round(sum(r.rating for r in rows) / count, 1) if count else None
    return {
        "count": count,
        "average": average,
        "reviews": [
            {
                "rating": r.rating,
                "text": r.text,
                "name": r.reviewer_name or "Customer",
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


class ConfirmDeliveryIn(BaseModel):
    # The buyer-only delivery code (sent to the buyer, shown at checkout). Only
    # someone who physically received the order should know it — so a hijacked
    # store can't self-confirm delivery to release funds early.
    code: str = Field(min_length=4, max_length=12)


@public_router.post("/store/{slug}/confirm-delivery")
@limiter.limit("20/hour")
def confirm_delivery(
    request: Request,
    slug: str,
    payload: ConfirmDeliveryIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: confirm delivery with the buyer's delivery code → ends the
    buyer-protection window early. The seller is paid on our T+1 settlement
    cadence (the next daily settlement run), never same-day.

    The code is only ever shown to the buyer, so the seller can't self-release.
    """
    import datetime as dt

    from app.services.escrow_code_guard import (
        clear_code_failures,
        is_code_locked,
        register_code_failure,
    )

    owner = _lookup_store(db, slug)
    if is_code_locked(owner.id):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts on this store. Please try again later.",
        )
    code = payload.code.strip()

    escrow = (
        db.query(models.StorefrontOrderEscrow)
        .filter(
            models.StorefrontOrderEscrow.seller_id == owner.id,
            models.StorefrontOrderEscrow.confirmation_code == code,
        )
        .order_by(models.StorefrontOrderEscrow.id.desc())
        .first()
    )

    if not escrow:
        register_code_failure(owner.id)
        logger.warning("Invalid delivery-code attempt on store %s", slug)
        raise HTTPException(status_code=404, detail="That delivery code isn't valid.")
    clear_code_failures(owner.id)

    if escrow.status == "released":
        return {"ok": True, "message": "This order was already completed — thank you!"}
    if escrow.status == "refunded":
        return {"ok": True, "message": "You've already been refunded for this order."}
    if escrow.status != "held":
        raise HTTPException(
            status_code=409,
            detail="This order can't be confirmed right now.",
        )
    if escrow.held_for_review:
        # A flagged order shouldn't release on a code — it's under review.
        raise HTTPException(
            status_code=409,
            detail="This order is under review. Our team will be in touch.",
        )

    escrow.confirmed_at = dt.datetime.now(dt.timezone.utc)
    db.commit()

    # Confirmation ENDS buyer protection, but the payout follows our T+1
    # settlement cadence — the daily settlement run pays the seller (never
    # same-day), funded by settled collections. No transfer is initiated here.
    return {
        "ok": True,
        "message": "Thank you for confirming! The seller will be settled in our next payout run.",
    }


class OrderProblemIn(BaseModel):
    # The buyer proves ownership with their release code (a secret only the buyer
    # has), so a third party who merely knows a phone number can't file a dispute
    # or get the seller flagged.
    code: str = Field(min_length=4, max_length=12)
    reason: str = Field(min_length=3, max_length=255)


@public_router.post("/store/{slug}/report-problem")
@limiter.limit("10/hour")
def report_order_problem(
    request: Request,
    slug: str,
    payload: OrderProblemIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: the buyer reports a problem with a held order (e.g. never
    delivered, wrong item). Puts the hold into ``disputed`` so no auto-payout
    happens. Gated by the buyer's RELEASE CODE (a secret only the buyer has), so
    a third party who knows a phone number can't dispute someone else's order.
    """
    import datetime as dt

    from app.services.escrow_code_guard import (
        clear_code_failures,
        is_code_locked,
        register_code_failure,
    )

    owner = _lookup_store(db, slug)
    # Brute-force guard: a wrong release code counts against the store's shared
    # failure budget (same lockout as delivery confirmation).
    if is_code_locked(owner.id):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts on this store. Please try again later.",
        )
    escrow = _escrow_by_code(db, owner.id, payload.code.strip())
    if not escrow:
        register_code_failure(owner.id)
        logger.warning("Invalid report-problem code on store %s", slug)
        raise HTTPException(
            status_code=404,
            detail="That release code isn't valid.",
        )
    clear_code_failures(owner.id)

    if escrow.status == "refunded":
        return {"ok": True, "message": "You've already been refunded for this order."}
    if escrow.status == "released":
        raise HTTPException(
            status_code=409,
            detail="This order was already completed. Please contact support@suoops.com.",
        )
    if escrow.status == "disputed":
        return {"ok": True, "message": "We've already got your report — our team is on it."}
    if escrow.status != "held":
        raise HTTPException(status_code=409, detail="This order can't be reported right now.")

    escrow.status = "disputed"
    escrow.disputed_at = dt.datetime.now(dt.timezone.utc)
    escrow.dispute_reason = payload.reason.strip()[:255]
    # We deliberately DON'T set owner.flagged_for_review here. The disputed order
    # already freezes the payout, blocks the seller from trusted status
    # (is_trusted_seller treats any disputed order as disqualifying) and surfaces
    # in the Trust & Safety queue. A single dispute shouldn't also force ALL the
    # seller's other new orders into manual review — the dispute-velocity guard
    # escalates repeat disputes instead.
    db.commit()

    # Track the buyer's dispute history (deters serial false "not delivered"
    # claims). Use the order's own customer phone.
    try:
        from app.services.escrow_service import record_buyer_dispute

        cust = (
            db.query(models.Customer)
            .join(models.Invoice, models.Invoice.customer_id == models.Customer.id)
            .filter(models.Invoice.id == escrow.invoice_id)
            .first()
        )
        if cust and cust.phone:
            record_buyer_dispute(db, cust.phone)
    except Exception:  # noqa: BLE001
        logger.exception("Failed to record buyer dispute for %s", slug)

    logger.info(
        "Escrow %s disputed by buyer for store %s (seller %s)",
        escrow.id, slug, owner.id,
    )
    return {
        "ok": True,
        "message": "Thanks for letting us know. Your payment is safe and our team will review this.",
    }


# ── Order-scoped messaging (guarded buyer/seller chat) ─────────────────────────
# Delivery-coordination only, and only while an order is live (held). Every
# message is scanned: leak vectors (contact/account/links + the delivery code)
# are masked, off-platform pushes are blocked, and seller circumvention attempts
# flag the store. This keeps the escrow + commission on-platform.

_MSG_MAX = 1000


class BuyerMessageIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)  # buyer-only delivery code
    body: str = Field(min_length=1, max_length=_MSG_MAX)


class BuyerThreadIn(BaseModel):
    code: str = Field(min_length=4, max_length=12)


class SellerMessageIn(BaseModel):
    body: str = Field(min_length=1, max_length=_MSG_MAX)


def _escrow_by_code(db: Session, owner_id: int, code: str):
    return (
        db.query(models.StorefrontOrderEscrow)
        .filter(
            models.StorefrontOrderEscrow.seller_id == owner_id,
            models.StorefrontOrderEscrow.confirmation_code == code,
        )
        .order_by(models.StorefrontOrderEscrow.id.desc())
        .first()
    )


def _messaging_open(escrow: "models.StorefrontOrderEscrow") -> bool:
    # Only for a live (held) order — not before payment or after it closes.
    return escrow.status == "held"


def _msg_out(m: "models.OrderMessage", viewer_role: str) -> dict:
    return {
        "id": m.id,
        "sender_role": m.sender_role,
        "mine": m.sender_role == viewer_role,
        "body": m.body_redacted,
        "flagged": bool(m.flagged),
        "created_at": m.created_at.isoformat() if m.created_at else None,
    }


def _buyer_order_view(escrow: "models.StorefrontOrderEscrow") -> dict:
    """Buyer-safe order status for the thread modal (dispatch/delivery updates).

    Lets the buyer see 'sent out' with the courier tracking + packaged-item
    photo, so they know their order is on the way.
    """
    return {
        "status": escrow.status,
        "dispatched_at": (
            escrow.seller_dispatched_at.isoformat() if escrow.seller_dispatched_at else None
        ),
        "dispatch_tracking": escrow.dispatch_tracking,
        "dispatch_carrier": escrow.dispatch_carrier,
        "dispatch_eta": escrow.dispatch_eta.isoformat() if escrow.dispatch_eta else None,
        "dispatch_tracking_url": escrow.shipbubble_tracking_url,
        "dispatch_proof_url": _presign(escrow.dispatch_proof_url),
        "delivery_status": escrow.delivery_status,
        "delivery_status_label": _delivery_status_label(escrow.delivery_status),
        "delivered_at": (
            escrow.seller_marked_delivered_at.isoformat()
            if escrow.seller_marked_delivered_at
            else None
        ),
    }


def _thread(db: Session, escrow_id: int) -> list["models.OrderMessage"]:
    return (
        db.query(models.OrderMessage)
        .filter(
            models.OrderMessage.escrow_id == escrow_id,
            models.OrderMessage.blocked.is_(False),  # blocked = stored for audit, never delivered
        )
        .order_by(models.OrderMessage.id.asc())
        .all()
    )


def _mark_read(db: Session, escrow_id: int, sender_role: str) -> None:
    import datetime as dt

    (
        db.query(models.OrderMessage)
        .filter(
            models.OrderMessage.escrow_id == escrow_id,
            models.OrderMessage.sender_role == sender_role,
            models.OrderMessage.read_at.is_(None),
        )
        .update({models.OrderMessage.read_at: dt.datetime.now(dt.timezone.utc)}, synchronize_session=False)
    )
    db.commit()


def _store_message(db: Session, escrow, *, sender_role: str, sender_user_id: int | None, body: str):
    from app.services.message_guard import (
        encoded_contact_score,
        is_number_fragment,
        mask_encoded_numbers,
        mask_number_words,
        scan_message,
    )

    result = scan_message(body)

    # Cross-message evasion: a phone number split across several short messages —
    # spelled out ("six seven eight" … "zero zero"), in digit groups ("080" …
    # "312"), binary ("001" … "110") or roman — slips past the single-message
    # filter. Sum the number 'positions' this sender used across their recent
    # messages; a phone-length total (>=7) is a shared contact number.
    current_score = encoded_contact_score(body)
    if "spelled_contact" not in result.reasons and current_score > 0:
        recent_score = sum(
            encoded_contact_score(b or "")
            for (b,) in (
                db.query(models.OrderMessage.body_raw)
                .filter(
                    models.OrderMessage.escrow_id == escrow.id,
                    models.OrderMessage.sender_role == sender_role,
                )
                .order_by(models.OrderMessage.id.desc())
                .limit(5)
                .all()
            )
        )
        if recent_score + current_score >= 7:
            result.reasons.append("spelled_contact")
            if not result.blocked:
                result.redacted = (
                    mask_encoded_numbers(result.redacted)
                    if is_number_fragment(body)
                    else mask_number_words(result.redacted)
                )

    m = models.OrderMessage(
        escrow_id=escrow.id,
        sender_role=sender_role,
        sender_user_id=sender_user_id,
        body_raw=body,
        body_redacted=("" if result.blocked else result.redacted),
        flagged=result.flagged,
        flag_reasons=(",".join(result.reasons) or None),
        blocked=result.blocked,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    # First time a thread trips the leak filter, drop a one-time system nudge so
    # both parties know contact/payment must stay on SuoOps (deterrent).
    if result.flagged:
        _maybe_warn_circumvention(db, escrow)
    return m, result


def _store_system_message(db: Session, escrow, body: str):
    """Insert a system notice into the order thread (never guard-redacted).

    Used for platform-generated updates (e.g. "order sent out") so tracking codes
    and links aren't masked the way buyer/seller messages are.
    """
    m = models.OrderMessage(
        escrow_id=escrow.id,
        sender_role="system",
        sender_user_id=None,
        body_raw=body,
        body_redacted=body,
        flagged=False,
        flag_reasons=None,
        blocked=False,
    )
    db.add(m)
    db.commit()
    db.refresh(m)
    return m


_CIRCUMVENTION_WARNING = (
    "🔒 For everyone's protection, keep contact and payment on SuoOps. Sharing "
    "phone numbers or arranging payment off-platform voids buyer protection and "
    "can flag the seller."
)


def _maybe_warn_circumvention(db: Session, escrow) -> None:
    """Post the anti-circumvention nudge into a thread once (the first time it
    trips the leak filter), so both parties are warned without spamming."""
    already = (
        db.query(models.OrderMessage.id)
        .filter(
            models.OrderMessage.escrow_id == escrow.id,
            models.OrderMessage.sender_role == "system",
            models.OrderMessage.body_raw == _CIRCUMVENTION_WARNING,
        )
        .first()
    )
    if not already:
        _store_system_message(db, escrow, _CIRCUMVENTION_WARNING)


@public_router.post("/store/{slug}/messages")
@limiter.limit("20/hour")
def buyer_send_message(
    request: Request,
    slug: str,
    payload: BuyerMessageIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: buyer sends a message on their order, authenticated by the
    buyer-only delivery code."""
    from app.services.escrow_code_guard import (
        clear_code_failures,
        is_code_locked,
        register_code_failure,
    )

    owner = _lookup_store(db, slug)
    if is_code_locked(owner.id):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts on this store. Please try again later.",
        )
    escrow = _escrow_by_code(db, owner.id, payload.code.strip())
    if not escrow:
        register_code_failure(owner.id)
        logger.warning("Invalid delivery-code attempt (message) on store %s", slug)
        raise HTTPException(status_code=404, detail="That delivery code isn't valid.")
    clear_code_failures(owner.id)
    if not _messaging_open(escrow):
        raise HTTPException(status_code=409, detail="Messaging is closed for this order.")

    m, result = _store_message(db, escrow, sender_role="buyer", sender_user_id=None, body=payload.body)
    if result.blocked:
        return {
            "ok": False,
            "blocked": True,
            "message": "Keep payments and contact on SuoOps so you stay protected — that message wasn't sent.",
        }
    return {
        "ok": True,
        "message": _msg_out(m, "buyer"),
        "warning": "Some details were hidden to keep you protected." if result.flagged else None,
    }


@public_router.post("/store/{slug}/messages/list")
@limiter.limit("20/hour")
def buyer_list_messages(
    request: Request,
    slug: str,
    payload: BuyerThreadIn,
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Public: buyer reads their order thread (delivery code = access)."""
    from app.services.escrow_code_guard import (
        clear_code_failures,
        is_code_locked,
        register_code_failure,
    )

    owner = _lookup_store(db, slug)
    if is_code_locked(owner.id):
        raise HTTPException(
            status_code=429,
            detail="Too many attempts on this store. Please try again later.",
        )
    escrow = _escrow_by_code(db, owner.id, payload.code.strip())
    if not escrow:
        register_code_failure(owner.id)
        logger.warning("Invalid delivery-code attempt (thread) on store %s", slug)
        raise HTTPException(status_code=404, detail="That delivery code isn't valid.")
    clear_code_failures(owner.id)
    _mark_read(db, escrow.id, "seller")  # buyer has now seen the seller's messages
    return {
        "messages": [_msg_out(m, "buyer") for m in _thread(db, escrow.id)],
        "order": _buyer_order_view(escrow),
    }


@router.get("/storefront/orders/{invoice_id}/messages")
def seller_list_messages(
    invoice_id: str,
    data_owner_id: Annotated[int, Depends(get_data_owner_id)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Seller reads the thread for one of their storefront orders."""
    row = _load_owner_escrow(db, data_owner_id, invoice_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    escrow, _buyer = row
    _mark_read(db, escrow.id, "buyer")
    return {"messages": [_msg_out(m, "seller") for m in _thread(db, escrow.id)]}


@router.post("/storefront/orders/{invoice_id}/messages")
def seller_send_message(
    invoice_id: str,
    payload: SellerMessageIn,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    data_owner_id: Annotated[int, Depends(get_data_owner_id)],
    db: Annotated[Session, Depends(get_db)],
) -> dict:
    """Seller replies on one of their storefront orders. Circumvention attempts
    (masked contact/account or off-platform pushes) flag the store. Scoped to the
    account owner so team members can reply; current_user_id records who sent it."""
    row = _load_owner_escrow(db, data_owner_id, invoice_id)
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    escrow, _buyer = row
    if not _messaging_open(escrow):
        raise HTTPException(status_code=409, detail="Messaging is closed for this order.")

    m, result = _store_message(
        db, escrow, sender_role="seller", sender_user_id=current_user_id, body=payload.body
    )
    if result.flagged:
        try:
            from app.services.escrow_service import record_seller_circumvention

            seller = db.query(models.User).filter(models.User.id == current_user_id).first()
            if seller:
                record_seller_circumvention(db, seller)
        except Exception:  # noqa: BLE001
            logger.exception("Failed to record seller circumvention for order %s", invoice_id)

    if result.blocked:
        return {
            "ok": False,
            "blocked": True,
            "message": "Payments and contact must stay on SuoOps. That message wasn't sent — repeated attempts flag your store.",
        }
    return {
        "ok": True,
        "message": _msg_out(m, "seller"),
        "warning": "Sharing contact or payment details off-platform is not allowed and was hidden." if result.flagged else None,
    }
