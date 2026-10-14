"""
Product-based invoice creation flow for WhatsApp bot.

Enables businesses to browse their inventory and create invoices
in just 3 messages — fast and simple, the SuoOps way.

Flow:
    1. Business sends "products" → bot shows product list as text
    2. Business replies "3 wig, 2 shoe" → bot fuzzy-matches against catalog,
       shows matched cart and asks for customer
    3. Business replies "Joy 08012345678" → invoice created with product_ids

Drafts are shared through Redis with a 15-minute TTL. No DB writes until invoice creation.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy.orm import Session

from app.bot.whatsapp_client import WhatsAppClient
from app.db.redis_client import get_redis_client
from app.models.inventory_models import Product
from app.services.inventory.product_service import ProductService
from app.utils.currency_fmt import fmt_money, get_user_currency

logger = logging.getLogger(__name__)

# Conversation state TTL (15 minutes)
CART_TTL_SECONDS = 900


@dataclass
class CartItem:
    """A product in the user's draft invoice cart."""

    product_id: int
    product_name: str
    quantity: int
    unit_price: Decimal

    @property
    def line_total(self) -> Decimal:
        return self.unit_price * self.quantity


@dataclass
class CartSession:
    """Ephemeral cart state for a WhatsApp user."""

    user_id: int  # DB user_id (issuer)
    items: list[CartItem] = field(default_factory=list)
    # Conversation step: "awaiting_items" | "awaiting_customer"
    step: str = "awaiting_items"
    product_ids: list[int] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)

    @property
    def is_expired(self) -> bool:
        return (time.time() - self.created_at) > CART_TTL_SECONDS

    @property
    def total(self) -> Decimal:
        return sum(item.line_total for item in self.items)

    def cart_summary_fmt(self, currency: str = "NGN") -> str:
        """Return a formatted cart summary respecting currency preference."""
        if not self.items:
            return "🛒 Cart is empty"
        lines = []
        for item in self.items:
            lines.append(f"  {item.quantity}x {item.product_name} — {fmt_money(item.line_total, currency)}")
        lines.append(f"\n💰 *Total: {fmt_money(self.total, currency)}*")
        return "🛒 *Your Cart:*\n" + "\n".join(lines)

    @property
    def cart_summary(self) -> str:
        return self.cart_summary_fmt("NGN")


_cart_adapter = TypeAdapter(CartSession)


def _cart_key(phone: str) -> str:
    return f"bot:product_cart:{phone.lstrip('+')}"


def save_cart(phone: str, session: CartSession) -> None:
    remaining = int(CART_TTL_SECONDS - (time.time() - session.created_at))
    if remaining <= 0:
        clear_cart(phone)
        return
    get_redis_client().setex(_cart_key(phone), remaining, _cart_adapter.dump_json(session))


def get_cart(phone: str) -> CartSession | None:
    """Get active cart for phone, or None if expired/missing."""
    raw = get_redis_client().get(_cart_key(phone))
    if raw is None:
        return None
    session = _cart_adapter.validate_json(raw)
    if session.is_expired:
        clear_cart(phone)
        return None
    return session


def clear_cart(phone: str) -> None:
    """Remove cart for phone."""
    get_redis_client().delete(_cart_key(phone))


def consume_cart(phone: str, session: CartSession) -> bool:
    """Only one worker can submit the exact draft that was reviewed."""
    return bool(get_redis_client().eval(
        "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end",
        1, _cart_key(phone), _cart_adapter.dump_json(session),
    ))


def _fuzzy_match_product(
    query: str,
    products: list[tuple[str, Product]],
) -> Product | None:
    """
    Fuzzy-match a query string against the user's product catalog.

    Matching strategy (in order of priority):
        1. Exact name match (case-insensitive)
        2. Query is a substring of product name ("wig" matches "Brazilian Wig")
        3. Product name starts with query ("sh" matches "Shoe")
        4. Any word in product name starts with query ("br" matches "Brazilian Wig")

    Returns the best-matching Product, or None.
    """
    query_lower = query.lower().strip()
    if not query_lower:
        return None

    # 1. Exact match
    for name_lower, product in products:
        if name_lower == query_lower:
            return product

    # 2. Substring match (prefer shorter names = more specific)
    substring_matches = [(name_lower, product) for name_lower, product in products if query_lower in name_lower]
    if substring_matches:
        return min(substring_matches, key=lambda x: len(x[0]))[1]

    # 3. Starts-with
    for name_lower, product in products:
        if name_lower.startswith(query_lower):
            return product

    # 4. Any word starts with query
    for name_lower, product in products:
        words = name_lower.split()
        if any(w.startswith(query_lower) for w in words):
            return product

    return None


def _parse_item_entries(text: str) -> list[tuple[int, str]]:
    """
    Parse user input into (quantity, description) pairs.

    Supports formats:
        "3 wig, 2 shoe"              → [(3, "wig"), (2, "shoe")]
        "3 wig 2 shoe"               → [(3, "wig"), (2, "shoe")]
        "wig 3, shoe 2"              → [(3, "wig"), (2, "shoe")]
        "wig, shoe"                  → [(1, "wig"), (1, "shoe")]
        "3 brazilian wig, 2 red shoe" → [(3, "brazilian wig"), (2, "red shoe")]
        "5 wig"                      → [(5, "wig")]
        "wig"                        → [(1, "wig")]
    """
    entries: list[tuple[int, str]] = []

    # Split by comma first
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) == 1 and "," not in text:
        # No commas — try space-separated: "3 wig 2 shoe"
        # Pattern: number followed by words until next number
        pattern = re.compile(r"(\d+)\s+([a-zA-Z][a-zA-Z\s]*?)(?=\s+\d+\s+[a-zA-Z]|$)")
        matches = pattern.findall(text.strip())
        if matches:
            for qty_str, desc in matches:
                qty = int(qty_str)
                if qty <= 100:
                    entries.append((qty, desc.strip()))
                else:
                    entries.append((1, f"{qty_str} {desc.strip()}"))
            return entries
        # Could be just "wig" or "3 wig"
        parts = [text.strip()]

    for part in parts:
        part = part.strip()
        if not part:
            continue

        # Try "3 wig" or "3 brazilian wig" — number first
        m = re.match(r"^(\d+)\s+(.+)$", part)
        if m:
            qty = int(m.group(1))
            desc = m.group(2).strip()
            if qty > 100:
                # Likely an amount, not quantity — treat as name
                entries.append((1, part))
            else:
                entries.append((qty, desc))
            continue

        # Try "wig 3" — number last
        m = re.match(r"^(.+?)\s+(\d+)$", part)
        if m:
            desc = m.group(1).strip()
            qty = int(m.group(2))
            if qty <= 100:
                entries.append((qty, desc))
                continue

        # Just a name with no quantity — default to 1
        if part and not part.isdigit():
            entries.append((1, part))

    return entries


class ProductInvoiceFlow:
    """
    Handles the product-browsing → invoice creation flow.

    3-message flow:
        1. "products" → show catalog
        2. "3 wig, 2 shoe" → match & build cart
        3. "Joy 08012345678" → create invoice
    """

    # Keywords that trigger the product browsing flow
    TRIGGER_KEYWORDS = {"products", "my products", "stock", "catalog", "inventory", "shop"}

    def __init__(self, db: Session, client: WhatsAppClient):
        self.db = db
        self.client = client

    @staticmethod
    def is_trigger(text: str) -> bool:
        """Check if text triggers the product browsing flow."""
        return text.lower().strip() in ProductInvoiceFlow.TRIGGER_KEYWORDS

    def _require_pro(self, phone: str, user_id: int) -> bool:
        """Product catalog is free under the commission model; always allowed."""
        return True

    def start_browsing(self, phone: str, user_id: int, search: str | None = None) -> None:
        """
        Show the user's product catalog as a text list.

        Then wait for them to reply with items + quantities in one message.
        Does NOT reset existing cart items (safe for "Add More").
        """
        if not self._require_pro(phone, user_id):
            return

        product_svc = ProductService(self.db, user_id)
        products, total = product_svc.list_products(
            page=1,
            page_size=10,
            search=search,
        )

        if not products:
            if search:
                self.client.send_text(
                    phone,
                    f'🔍 No products found for "{search}".\n\n'
                    "Try a different keyword, or type *products* to see all.",
                )
            else:
                self.client.send_text(
                    phone,
                    "📦 You have no products in your inventory yet.\n\n"
                    "Add products at suoops.com/dashboard/inventory\n"
                    "Then come back and type *products* to invoice from stock!",
                )
            return

        # Preserve existing cart items if session exists (for "Add More")
        existing_session = get_cart(phone)
        if existing_session and existing_session.user_id == user_id:
            existing_session.product_ids = [p.id for p in products]
            existing_session.step = "awaiting_items"
            session = existing_session
        else:
            session = CartSession(user_id=user_id, product_ids=[p.id for p in products])
        save_cart(phone, session)

        # Resolve user's preferred display currency
        currency = get_user_currency(self.db, user_id)

        # Build readable product list
        product_lines = []
        for product in products[:10]:
            price = fmt_money(product.selling_price, currency) if product.selling_price else "—"
            stock = f" ({product.quantity_in_stock})" if product.track_stock else ""
            product_lines.append(f"• *{product.name}* — {price}{stock}")

        product_list = "\n".join(product_lines)

        # Show existing cart if items present
        cart_text = ""
        if session.items:
            cart_text = f"\n\n{session.cart_summary_fmt(currency)}\n"

        more_text = ""
        if total > 10:
            more_text = f"\n_Showing 10 of {total} — type *search <name>* for more_\n"

        self.client.send_text(
            phone,
            f"📦 *Your Products:*\n\n"
            f"{product_list}\n"
            f"{more_text}{cart_text}\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📝 *Reply with items to invoice:*\n"
            "e.g. `3 wig, 2 shoe`\n\n"
            "Or just the name for 1 unit:\n"
            "e.g. `wig, shoe, belt`",
        )

    def handle_items_reply(self, phone: str, text: str) -> bool:
        """
        User replied with items + quantities — fuzzy-match and build cart.

        Returns True if items were matched, False if nothing matched.
        """
        session = get_cart(phone)
        if not session or session.step != "awaiting_items":
            return False

        products = (
            self.db.query(Product)
            .filter(
                Product.user_id == session.user_id,
                Product.id.in_(session.product_ids),
                Product.is_active.is_(True),
            )
            .populate_existing()
            .all()
        )
        product_cache = [(p.name.lower(), p) for p in products]

        entries = _parse_item_entries(text)
        if not entries:
            self.client.send_text(
                phone, "Please include a product name and quantity, e.g. *2 wig*.\n"
                "Type *products* to browse or *cancel draft* to stop.",
            )
            return False

        matched: list[CartItem] = []
        unmatched: list[str] = []
        stock_warnings: list[str] = []

        for qty, desc in entries:
            product = _fuzzy_match_product(desc, product_cache)
            if product:
                already_added = sum(
                    item.quantity for item in [*session.items, *matched] if item.product_id == product.id
                )
                # Check stock
                if qty < 1:
                    stock_warnings.append(f"⚠️ *{product.name}*: quantity must be at least 1.")
                    continue
                if product.track_stock and qty + already_added > product.quantity_in_stock:
                    stock_warnings.append(
                        f"⚠️ *{product.name}*: only {product.quantity_in_stock} in stock (you asked for {qty})"
                    )
                    continue

                price = product.selling_price or Decimal("0")
                if price <= 0:
                    stock_warnings.append(f"⚠️ *{product.name}*: set a selling price in inventory first.")
                    continue
                matched.append(
                    CartItem(
                        product_id=product.id,
                        product_name=product.name,
                        quantity=qty,
                        unit_price=price,
                    )
                )
            else:
                unmatched.append(desc)

        if not matched and unmatched:
            self.client.send_text(
                phone,
                f"❌ Couldn't find: {', '.join(unmatched)}\n\n"
                "Check the spelling and try again, or type *products* to see your catalog.",
            )
            return True

        if not matched and stock_warnings:
            self.client.send_text(phone, "\n".join(stock_warnings) + "\n\nTry again with lower quantities.")
            return True

        # Merge matched items into cart (handle duplicates)
        for new_item in matched:
            existing = next((i for i in session.items if i.product_id == new_item.product_id), None)
            if existing:
                existing.quantity += new_item.quantity
            else:
                session.items.append(new_item)

        # Resolve user's preferred display currency
        currency = get_user_currency(self.db, session.user_id)

        # Build response
        added_lines = []
        for item in matched:
            added_lines.append(f"  ✅ {item.quantity}x {item.product_name} — {fmt_money(item.line_total, currency)}")
        added_text = "\n".join(added_lines)

        warn_text = ""
        if unmatched:
            warn_text = f"\n⚠️ Not found: {', '.join(unmatched)}"
        if stock_warnings:
            warn_text += "\n" + "\n".join(stock_warnings)

        session.step = "awaiting_customer"
        save_cart(phone, session)

        self.client.send_text(
            phone,
            f"{added_text}{warn_text}\n\n"
            f"{session.cart_summary_fmt(currency)}\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "👤 *Who is this invoice for?*\n"
            "e.g. `Joy 08012345678`\n\n"
            "Or type *more* to add more items\n"
            "Or type *clear* to start over",
        )
        return True

    def handle_customer_reply(self, phone: str, text: str) -> dict[str, Any] | None:
        """
        User replied with customer details — build invoice data dict.

        Returns the invoice data dict ready for InvoiceIntentProcessor,
        or None if user chose to add more or clear.
        """
        session = get_cart(phone)
        if not session or session.step != "awaiting_customer":
            return None

        text_lower = text.strip().lower()

        # Check for "more" / "add more" — go back to browsing
        if text_lower in ("more", "add more", "add"):
            self.start_browsing(phone, session.user_id)
            return None

        # Check for "clear" — reset cart
        if text_lower in ("clear", "cancel", "reset"):
            clear_cart(phone)
            self.client.send_text(phone, "🗑️ Cart cleared.\n\nType *products* to start again.")
            return None

        products = {
            p.id: p for p in self.db.query(Product).filter(
                Product.user_id == session.user_id,
                Product.id.in_([item.product_id for item in session.items]),
                Product.is_active.is_(True),
            ).populate_existing().all()
        }
        changed_price = False
        for item in session.items:
            product = products.get(item.product_id)
            if (
                product is None
                or not product.selling_price
                or product.selling_price <= 0
                or (product.track_stock and product.quantity_in_stock < item.quantity)
            ):
                self.client.send_text(
                    phone,
                    f"*{item.product_name}* is no longer available at this quantity or has no selling price. "
                    "Nothing has been created. Type *clear* to choose items again.",
                )
                return None
            if item.unit_price != product.selling_price:
                item.unit_price = product.selling_price
                changed_price = True
        if changed_price:
            save_cart(phone, session)
            currency = get_user_currency(self.db, session.user_id)
            self.client.send_text(
                phone,
                "Prices changed since you selected these items. Please review:\n\n"
                f"{session.cart_summary_fmt(currency)}\n\n"
                "To accept these prices, send the customer details again. Or type *clear* to start over.",
            )
            return None

        # Parse customer name and phone from text
        from app.bot.nlp_service import NLPService

        nlp = NLPService()
        customer_phone = nlp._extract_phone(text)
        customer_email = nlp._extract_email(text)

        # Extract name: first word that isn't a phone number
        tokens = text.strip().split()
        customer_name: str | None = None
        for token in tokens:
            clean = token.replace("+", "").replace("-", "").replace(",", "")
            if not clean.isdigit() and len(token) >= 2:
                customer_name = token.capitalize()
                break
        if customer_name is None or (tokens[0].isdigit() and not customer_phone):
            self.client.send_text(
                phone, "Please include the customer's name, for example *Joy 08012345678*. Your draft is still saved.",
            )
            return None

        # Build line items with product_id for inventory integration
        lines = []
        for item in session.items:
            lines.append(
                {
                    "description": item.product_name,
                    "quantity": item.quantity,
                    "unit_price": item.unit_price,
                    "product_id": item.product_id,
                }
            )

        invoice_data = {
            "customer_name": customer_name,
            "customer_phone": customer_phone,
            "customer_email": customer_email,
            "amount": session.total,
            "currency": get_user_currency(self.db, session.user_id),
            "lines": lines,
            # due_date omitted — no due date is set (no auto-default), so this
            # invoice won't trigger overdue reminders.
        }

        if not consume_cart(phone, session):
            self.client.send_text(
                phone,
                "This draft changed or is already being processed. Check your invoices before trying again.",
            )
            return None

        return invoice_data

    def handle_add_more(self, phone: str) -> None:
        """User tapped 'Add More' or typed 'more' — show products, KEEP cart."""
        session = get_cart(phone)
        if not session:
            self.client.send_text(phone, "⏰ Session expired. Type *products* to start again.")
            return

        # Show product list again WITHOUT resetting the cart
        self.start_browsing(phone, session.user_id)

    def handle_clear_cart(self, phone: str) -> None:
        """User tapped 'Clear Cart' — reset everything."""
        clear_cart(phone)
        self.client.send_text(phone, "🗑️ Cart cleared.\n\nType *products* to start again.")

    def handle_search(self, phone: str, user_id: int, query: str) -> None:
        """User typed 'search <query>' — filter products."""
        self.start_browsing(phone, user_id, search=query)
