"""Render a mobile-optimized "cash-first" dashboard as a PNG for WhatsApp.

WhatsApp can't display the web dashboard's responsive cards, so we rasterise a
compact, phone-friendly snapshot (1080px wide) that a merchant can glance at to
understand their liquidity: money in, net, outstanding, overdue and expected
inflow. Pure Pillow — no external services.
"""

from __future__ import annotations

import io
import logging

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)

# ── Palette ──────────────────────────────────────────────────────────
TEAL = "#0F766E"
INK = "#1F2937"
MUTED = "#6B7280"
CARD_BG = "#F9FAFB"
BORDER = "#E5E7EB"
WHITE = "#FFFFFF"

# Canvas geometry
W = 1080
PAD = 56
HEADER_H = 190
GAP = 32
HL_H = 170
CARD_H = 150

_FONT_PATHS_REGULAR = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
]
_FONT_PATHS_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
]


def _font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Load a scalable font, falling back to Pillow's bundled default."""
    for path in _FONT_PATHS_BOLD if bold else _FONT_PATHS_REGULAR:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)  # Pillow >= 10.1 (DejaVuSans)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _truncate(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    ellipsis = "…"
    while text and draw.textlength(text + ellipsis, font=font) > max_w:
        text = text[:-1]
    return (text + ellipsis) if text else ""


def _card(draw, x, y, w, h, label, value, sub, accent, value_font, label_font, sub_font):
    draw.rounded_rectangle([x, y, x + w, y + h], radius=18, fill=CARD_BG, outline=BORDER, width=2)
    # accent bar on the left edge
    draw.rounded_rectangle([x, y + 8, x + 10, y + h - 8], radius=5, fill=accent)
    tx = x + 34
    inner_w = w - (tx - x) - 24
    draw.text((tx, y + 20), _truncate(draw, label, label_font, inner_w), font=label_font, fill=MUTED)
    draw.text((tx, y + 56), _truncate(draw, value, value_font, inner_w), font=value_font, fill=accent)
    if sub:
        draw.text((tx, y + h - 42), _truncate(draw, sub, sub_font, inner_w), font=sub_font, fill=MUTED)


def render_cash_dashboard_png(
    *,
    title: str,
    subtitle: str,
    highlights: list[dict],
    cards: list[dict],
    footer: str,
) -> bytes:
    """Render the cash snapshot and return PNG bytes.

    highlights / cards items: {"label", "value", "sub" (optional), "accent" (hex)}.
    """
    f_title = _font(52, bold=True)
    f_subtitle = _font(28)
    f_hl_label = _font(28)
    f_hl_value = _font(58, bold=True)
    f_label = _font(26)
    f_value = _font(40, bold=True)
    f_sub = _font(24)
    f_footer = _font(24)

    col_w = (W - 2 * PAD - GAP) // 2
    x_left, x_right = PAD, PAD + col_w + GAP

    content_top = HEADER_H + 44
    after_hl = content_top + HL_H + GAP
    rows = (len(cards) + 1) // 2
    grid_h = rows * CARD_H + max(rows - 1, 0) * GAP
    footer_y = after_hl + grid_h + 40
    height = footer_y + 34 + 40

    img = Image.new("RGB", (W, height), WHITE)
    draw = ImageDraw.Draw(img)

    # Header band
    draw.rectangle([0, 0, W, HEADER_H], fill=TEAL)
    draw.text((PAD, 44), _truncate(draw, title, f_title, W - 2 * PAD), font=f_title, fill=WHITE)
    draw.text((PAD, 116), _truncate(draw, subtitle, f_subtitle, W - 2 * PAD), font=f_subtitle, fill="#D1FAE5")

    # Highlight row (up to two big cards)
    for i, hl in enumerate(highlights[:2]):
        x = x_left if i == 0 else x_right
        _card(
            draw,
            x,
            content_top,
            col_w,
            HL_H,
            hl.get("label", ""),
            hl.get("value", ""),
            hl.get("sub"),
            hl.get("accent", TEAL),
            f_hl_value,
            f_hl_label,
            f_sub,
        )

    # Card grid
    for idx, card in enumerate(cards):
        r, c = divmod(idx, 2)
        x = x_left if c == 0 else x_right
        y = after_hl + r * (CARD_H + GAP)
        _card(
            draw,
            x,
            y,
            col_w,
            CARD_H,
            card.get("label", ""),
            card.get("value", ""),
            card.get("sub"),
            card.get("accent", TEAL),
            f_value,
            f_label,
            f_sub,
        )

    # Footer
    draw.line([PAD, footer_y - 12, W - PAD, footer_y - 12], fill=BORDER, width=2)
    draw.text((PAD, footer_y), _truncate(draw, footer, f_footer, W - 2 * PAD), font=f_footer, fill=MUTED)

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def build_cash_snapshot_png(cash: dict, business_name: str, currency: str = "NGN") -> bytes:
    """Build the standard cash-snapshot PNG from a `calculate_cash_position` dict.

    Shared by the WhatsApp `report` command and the daily-summary task so the
    layout and copy stay consistent.
    """
    from datetime import date

    from app.utils.currency_fmt import fmt_money

    def fmt(value) -> str:
        return fmt_money(float(value or 0), currency, compact=True)

    net = cash.get("net_today", 0) or 0
    net_accent = "#047857" if net >= 0 else "#B91C1C"
    overdue_count = cash.get("overdue_count", 0) or 0
    overdue_sub = f"{overdue_count} invoice(s)" if overdue_count else "None overdue"

    highlights = [
        {"label": "Cash in today", "value": fmt(cash.get("cash_collected_today")), "accent": TEAL},
        {"label": "Net today (in − out)", "value": fmt(net), "accent": net_accent},
    ]
    cards = [
        {
            "label": "Outstanding (unpaid)",
            "value": fmt(cash.get("total_outstanding")),
            "sub": "Money owed to you",
            "accent": "#B45309",
        },
        {"label": "Overdue", "value": fmt(cash.get("total_overdue")), "sub": overdue_sub, "accent": "#B91C1C"},
        {
            "label": "Expected in 7 days",
            "value": fmt(cash.get("expected_inflow_7_days")),
            "sub": "Invoices due soon",
            "accent": TEAL,
        },
        {
            "label": "Collected this week",
            "value": fmt(cash.get("cash_collected_this_week")),
            "sub": "Last 7 days",
            "accent": "#047857",
        },
        {
            "label": "Expenses today",
            "value": fmt(cash.get("expenses_today")),
            "sub": f"{cash.get('invoices_created_today', 0)} invoice(s) created today",
            "accent": "#6B7280",
        },
    ]
    return render_cash_dashboard_png(
        title=str(business_name or "Your Business"),
        subtitle="Cash Snapshot • " + date.today().strftime("%b %d, %Y"),
        highlights=highlights,
        cards=cards,
        footer="SuoOps • suoops.com/dashboard",
    )
