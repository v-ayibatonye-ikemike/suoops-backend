"""
Daily Morning Business Insight.

Celery task that sends a short, conversational business tip to every user
each morning via WhatsApp + email.  Tips rotate through a pool of 30 and
cycle back to the beginning once exhausted.

Schedule: 07:00 UTC (08:00 WAT) daily.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.core.config import settings
from app.db.session import session_scope
from app.utils.smtp import send_smtp_email as _send_smtp_email
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# ── Jinja2 ────────────────────────────────────────────────────────────
_template_dir = Path(__file__).parent.parent.parent.parent / "templates" / "email"
_jinja_env = Environment(
    loader=FileSystemLoader(str(_template_dir)),
    autoescape=select_autoescape(["html", "xml"]),
)

# ── Email type prefix ─────────────────────────────────────────────────
INSIGHT_PREFIX = "morning_insight_"

# ── Tips Pool (30 rotating tips) ──────────────────────────────────────
# Each tip is a dict with subject (email), headline, body, tip (actionable),
# and optional cta_url / cta_label.
MORNING_TIPS: list[dict[str, Any]] = [
    # ── Cash Flow & Payments ──────────────────────────────────────────
    {
        "subject": "Cash is king — here's how to speed it up",
        "headline": "Speed Up Your Cash Flow 💰",
        "body": (
            "Most small businesses don't fail because of low sales — they fail because of slow cash. "
            "Send invoices the same day you deliver, add clear due dates, and follow up within 48 hours. "
            "The faster you invoice, the faster you get paid."
        ),
        "tip": (
            "Set a rule: invoice before the end of the day, every time. "
            "SuoOps even auto-sets a 3-day due date for you."
        ),
    },
    {
        "subject": "Stop chasing payments — automate reminders",
        "headline": "Let Reminders Do the Chasing 🔔",
        "body": (
            "Chasing customers for money is awkward and time-consuming. "
            "SuoOps automatically sends payment reminders to your customers before and after the due date. "
            "You focus on your business, we handle the follow-ups."
        ),
        "tip": "Make sure your customers' phone numbers are on their invoices — reminders go out automatically.",
    },
    {
        "subject": "The ₦500K mistake — not separating business and personal money",
        "headline": "Separate Your Business Money 🏦",
        "body": (
            "Mixing business and personal money is the #1 accounting mistake Nigerian entrepreneurs make. "
            "Open a dedicated business account and route all business transactions through it. "
            "Your future self (and your accountant) will thank you."
        ),
        "tip": "Add your business bank details to your SuoOps profile so every invoice shows the right account.",
    },
    {
        "subject": "Offer multiple payment options to get paid faster",
        "headline": "Make It Easy to Pay You 💳",
        "body": (
            "The more payment options you offer, the faster customers pay. "
            "Bank transfer, POS, mobile money — give them choices. "
            "Include your bank details right on the invoice so there's zero friction."
        ),
        "tip": (
            "Go to Settings → Business Profile and add your bank name + account number. "
            "It shows up on every invoice."
        ),
    },
    {
        "subject": "Why you should always set a due date",
        "headline": "Due Dates Change Everything 📅",
        "body": (
            "An invoice without a due date is a suggestion, not a bill. "
            "Research shows that invoices with clear due dates get paid 30% faster. "
            "Keep it short — 3 to 7 days works best for small businesses."
        ),
        "tip": "SuoOps automatically adds a 3-day due date to every invoice. Your customers see it clearly.",
    },
    # ── Customer Relationships ────────────────────────────────────────
    {
        "subject": "Your best customer is the one you already have",
        "headline": "Keep Your Best Customers Close 🤝",
        "body": (
            "It costs 5x more to get a new customer than to keep an existing one. "
            "Check in with your top 3 customers this week — a simple 'how's business?' "
            "goes a long way. Loyalty is built on relationships, not just transactions."
        ),
        "tip": "Look at your SuoOps dashboard — who are your top customers by revenue? Reach out to them today.",
    },
    {
        "subject": "The power of saying 'thank you'",
        "headline": "A Simple Thank You Goes Far 🙏",
        "body": (
            "After every payment, send a quick thank you. It takes 10 seconds and "
            "makes your business feel personal and professional. "
            "Customers remember how you made them feel — not just what you sold them."
        ),
        "tip": (
            "SuoOps sends payment receipts automatically. "
            "Add a personal touch by following up with a thank-you message."
        ),
    },
    {
        "subject": "Handle complaints before they become bad reviews",
        "headline": "Turn Complaints Into Loyalty ⚡",
        "body": (
            "A customer who complains and gets a fast resolution becomes more loyal than one who never had a problem. "
            "Respond within 24 hours, acknowledge the issue, and fix it. "
            "That's how small businesses build unshakeable reputations."
        ),
        "tip": "Keep track of difficult orders and follow up after resolution. A quick check-in shows you care.",
    },
    {
        "subject": "Why referrals are your best marketing channel",
        "headline": "Your Customers Are Your Best Marketers 📣",
        "body": (
            "91% of people trust recommendations from friends over any advertisement. "
            "Ask your happy customers to refer you. Offer a small discount or bonus for referrals. "
            "Word-of-mouth is free and powerful — especially in Nigeria."
        ),
        "tip": (
            "After a successful delivery, simply ask: "
            "'Do you know anyone else who might need this?' You'll be surprised."
        ),
    },
    {
        "subject": "Know your customer like family",
        "headline": "Build a Customer Database 📋",
        "body": (
            "Every customer interaction is data. Track their names, purchase history, and preferences. "
            "When you remember that Ade always orders 50 units or that Bimpe prefers delivery on Thursdays, "
            "you become irreplaceable."
        ),
        "tip": "SuoOps automatically builds your customer database from invoices. Check Dashboard → Customers.",
    },
    # ── Pricing & Profitability ───────────────────────────────────────
    {
        "subject": "Are you charging enough?",
        "headline": "Check Your Pricing 🏷️",
        "body": (
            "Most small business owners undercharge. Add up your costs — materials, transport, time, overhead — "
            "then add at least 30% margin. If customers never complain about your price, "
            "you're probably too cheap."
        ),
        "tip": (
            "Track both revenue and expenses in SuoOps to see your actual profit margin. "
            "If it's under 20%, review your pricing."
        ),
    },
    {
        "subject": "The hidden cost that kills profit",
        "headline": "Track Every Expense 📝",
        "body": (
            "Small expenses add up fast — fuel, data, packaging, bank charges. "
            "If you don't track them, you'll think you're making profit when you're actually losing money. "
            "Record every business expense, no matter how small."
        ),
        "tip": "SuoOps lets you record expenses alongside your invoices. One dashboard shows your true profit.",
    },
    {
        "subject": "Price anchoring — a simple trick that works",
        "headline": "Use Price Anchoring 🎯",
        "body": (
            "When you show a higher-priced option first, the regular price feels like a better deal. "
            "Offer a premium package alongside your standard one. "
            "Many customers will choose the standard — but some will choose premium, increasing your average sale."
        ),
        "tip": "Try offering 3 tiers: Basic, Standard, Premium. Most people pick the middle one.",
    },
    {
        "subject": "Bundle products to increase your average sale",
        "headline": "The Power of Bundling 📦",
        "body": (
            "Instead of selling items individually, bundle related products together at a slight discount. "
            "Customers feel they're getting a deal, and your average sale goes up. "
            "A ₦3,000 item + a ₦2,000 item bundled at ₦4,500 moves more volume."
        ),
        "tip": "Look at your most sold items in SuoOps. Which ones are frequently bought together? Create a bundle.",
    },
    # ── Record Keeping & Tax ──────────────────────────────────────────
    {
        "subject": "FIRS won't accept 'I forgot' as an excuse",
        "headline": "Keep Records Like a Pro 🗂️",
        "body": (
            "The Federal Inland Revenue Service expects proper records of all business income and expenses. "
            "Don't wait until tax season to organize your books. "
            "Record transactions daily and you'll never scramble when FIRS comes calling."
        ),
        "tip": (
            "Every invoice you send through SuoOps is automatically stored. "
            "Your tax report generates with one click."
        ),
    },
    {
        "subject": "VAT registration: do you need it?",
        "headline": "Know Your VAT Obligations 📊",
        "body": (
            "In Nigeria, businesses with annual turnover above ₦25 million must register for VAT. "
            "Even if you're below the threshold, voluntary registration can make you look more professional "
            "and let you reclaim input VAT on purchases."
        ),
        "tip": (
            "SuoOps tracks your monthly revenue automatically. "
            "Check your dashboard to see if you're approaching the threshold."
        ),
    },
    {
        "subject": "End-of-month ritual that saves hours at tax time",
        "headline": "Monthly Reconciliation ✅",
        "body": (
            "On the last day of every month, spend 15 minutes reviewing your invoices and expenses. "
            "Mark any unpaid invoices, check for missing expenses, and verify your bank balance matches your records. "
            "This one habit saves hours during tax season."
        ),
        "tip": "SuoOps generates monthly tax reports automatically on the 1st. Review them before they pile up.",
    },
    {
        "subject": "Keep receipts for everything you buy for business",
        "headline": "Receipt Culture Saves Money 🧾",
        "body": (
            "Every business expense receipt is a potential tax deduction. "
            "Fuel, office supplies, internet, phone airtime — if it's for business, keep the receipt. "
            "Digital records are just as valid as paper ones."
        ),
        "tip": "Snap a photo of receipts and record them as expenses in SuoOps. They're stored safely in the cloud.",
    },
    # ── Growth & Strategy ─────────────────────────────────────────────
    {
        "subject": "The 80/20 rule: focus on what matters",
        "headline": "Focus on Your Top 20% 🎯",
        "body": (
            "80% of your revenue probably comes from 20% of your customers or products. "
            "Identify your top performers and double down on them. "
            "Stop spending equal energy on everything — focus creates growth."
        ),
        "tip": "Check your SuoOps dashboard analytics to see your top customers and highest-value products.",
    },
    {
        "subject": "Start small, but start today",
        "headline": "Action Beats Perfection 🚀",
        "body": (
            "Don't wait for the perfect website, the perfect logo, or the perfect plan. "
            "Start with what you have and improve as you go. "
            "The businesses that win are the ones that start — not the ones that plan forever."
        ),
        "tip": "What's one thing you've been postponing for your business? Do it today, even if it's imperfect.",
    },
    {
        "subject": "Your business needs an online presence — here's why",
        "headline": "Get Found Online 🌐",
        "body": (
            "Even if you sell offline, your customers search Google before buying. "
            "A simple Google Business Profile is free and puts you on the map — literally. "
            "Add your business name, location, phone number, and WhatsApp link."
        ),
        "tip": "Go to google.com/business and set up your profile in 10 minutes. It's free.",
    },
    {
        "subject": "WhatsApp is your free marketing channel",
        "headline": "WhatsApp for Business Growth 💬",
        "body": (
            "You already use WhatsApp every day. Use it for business too. "
            "Share product updates, delivery confirmations, and payment reminders. "
            "Your customers are already on WhatsApp — meet them where they are."
        ),
        "tip": (
            "You can create and send invoices directly on WhatsApp with SuoOps. "
            "Just message us your invoice details!"
        ),
    },
    {
        "subject": "Consistency beats intensity — every time",
        "headline": "Show Up Every Day 📆",
        "body": (
            "The businesses that grow steadily are the ones that show up consistently. "
            "Post regularly, respond quickly, deliver on time, invoice promptly. "
            "You don't need to go viral — you need to be reliable."
        ),
        "tip": "Set a daily 10-minute business routine: check invoices, respond to messages, record expenses.",
    },
    # ── Operations & Efficiency ───────────────────────────────────────
    {
        "subject": "Automate the boring stuff",
        "headline": "Work Smarter, Not Harder ⚙️",
        "body": (
            "Every minute you spend on admin is a minute you're not selling. "
            "Automate invoicing, payment reminders, and record-keeping. "
            "Let technology handle the repetitive work while you focus on growth."
        ),
        "tip": "SuoOps handles invoicing, reminders, receipts, and reports automatically. What else can you automate?",
    },
    {
        "subject": "Stock management: don't sleep on it",
        "headline": "Know Your Stock Levels 📦",
        "body": (
            "Running out of stock means lost sales. Overstocking ties up cash. "
            "Track what's selling fast and what's gathering dust. "
            "Set reorder points for your best sellers so you never run dry."
        ),
        "tip": "SuoOps has inventory tracking built in. Link products to invoices and stock updates automatically.",
    },
    {
        "subject": "Delegate to grow — you can't do everything alone",
        "headline": "Learn to Delegate 🤲",
        "body": (
            "If you're doing everything yourself, your business can only grow as fast as you can work. "
            "Hire help for tasks that don't need your personal touch — deliveries, packaging, data entry. "
            "Your time is worth more on strategy and sales."
        ),
        "tip": "Start by delegating one task this week. Even a small one. See how it frees up your energy.",
    },
    # ── Mindset & Leadership ──────────────────────────────────────────
    {
        "subject": "Every successful business owner was once a beginner",
        "headline": "Keep Learning, Keep Growing 📚",
        "body": (
            "Dangote started with a ₦500,000 loan from his uncle. "
            "Every empire starts small. The difference is that winners keep learning — "
            "from mistakes, from mentors, from books, from their own numbers."
        ),
        "tip": "Read one article or watch one business video this week. Small learning habits compound over time.",
    },
    {
        "subject": "Data tells the truth — always check your numbers",
        "headline": "Trust the Numbers 📈",
        "body": (
            "Gut feeling is great, but numbers don't lie. "
            "How much did you make last month? What's your biggest expense? Which product sells most? "
            "If you can't answer these questions, it's time to start tracking."
        ),
        "tip": (
            "Your SuoOps dashboard shows revenue, expenses, and profit at a glance. "
            "Check it daily — it takes 30 seconds."
        ),
    },
    {
        "subject": "Protect your business from the unexpected",
        "headline": "Build an Emergency Fund 🛡️",
        "body": (
            "Unexpected expenses will come — broken equipment, late payments, market changes. "
            "Set aside 10% of every payment you receive into a business emergency fund. "
            "When trouble comes, you'll be ready instead of scrambling."
        ),
        "tip": "Open a separate savings account for your business reserve. Move 10% into it after every payment.",
    },
    {
        "subject": "Your first hour sets the tone for the whole day",
        "headline": "Win the Morning, Win the Day ☀️",
        "body": (
            "Start each business day with a plan. Check your pending invoices, review today's deliveries, "
            "and identify your top 3 priorities. A focused morning means a productive day. "
            "That's why we send this tip early — to start your day right!"
        ),
        "tip": "Before you check social media, check your SuoOps dashboard. Know your numbers first, scroll later.",
    },
]

# Total tips available
TOTAL_TIPS = len(MORNING_TIPS)


# ── Helpers ───────────────────────────────────────────────────────────
def _get_next_tip_index(db, user_id: int) -> int:
    """Return the index of the next unsent morning insight for this user.

    Cycles back to 0 after all tips have been sent.
    """
    from app.models.models import UserEmailLog

    sent_count = (
        db.query(UserEmailLog.id)
        .filter(
            UserEmailLog.user_id == user_id,
            UserEmailLog.email_type.like(f"{INSIGHT_PREFIX}%"),
        )
        .count()
    )
    return sent_count % TOTAL_TIPS


def _was_sent_today(db, user_id: int) -> bool:
    """Check if the user already received a morning insight today."""
    from app.models.models import UserEmailLog

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return (
        db.query(UserEmailLog.id)
        .filter(
            UserEmailLog.user_id == user_id,
            UserEmailLog.email_type.like(f"{INSIGHT_PREFIX}%"),
            UserEmailLog.sent_at >= today_start,
        )
        .first()
        is not None
    )


def _record_sent(db, user_id: int, tip_index: int) -> None:
    """Record that a morning insight was sent."""
    from app.models.models import UserEmailLog

    db.add(
        UserEmailLog(
            user_id=user_id,
            email_type=f"{INSIGHT_PREFIX}{tip_index}",
        )
    )
    db.flush()


def _is_valid_phone(phone: str | None) -> bool:
    """Return True if phone looks like real digits (not an OAuth placeholder)."""
    if not phone:
        return False
    digits = phone.lstrip("+")
    return digits.isdigit() and len(digits) >= 10


# ── Main Task ─────────────────────────────────────────────────────────


@celery_app.task(
    name="insights.send_morning_insights",
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_kwargs={"max_retries": 2},
    soft_time_limit=1740,
    time_limit=1800,
)
def send_morning_insights() -> dict[str, Any]:
    """Send a morning business insight to all users every 3 days.

    Runs at 07:00 UTC (08:00 WAT) on Mon/Thu (every ~3 days).

    Sends via:
    1. WhatsApp template (if user has phone + template configured)
    2. Email (all users with email)

    Tips rotate through a pool of 30 and cycle after exhaustion.
    Each user gets the next tip they haven't received yet.
    Per-user dedup prevents double-sends if the task runs twice.
    """
    from app.models.models import User

    stats: dict[str, int] = {
        "whatsapp_sent": 0,
        "email_sent": 0,
        "skipped": 0,
        "failed": 0,
    }

    try:
        with session_scope() as db:
            # All users with email or phone
            users = (
                db.query(User)
                .filter(
                    (User.email.isnot(None)) | (User.phone.isnot(None)),
                )
                .all()
            )

            logger.info("Morning insights: %d eligible users", len(users))

            template_name = getattr(settings, "WHATSAPP_TEMPLATE_MORNING_TIP", None)
            template_lang = getattr(settings, "WHATSAPP_TEMPLATE_LANGUAGE", "en")

            for user in users:
                try:
                    # Skip if already sent today
                    if _was_sent_today(db, user.id):
                        stats["skipped"] += 1
                        continue

                    # Get next tip for this user
                    tip_index = _get_next_tip_index(db, user.id)
                    tip = MORNING_TIPS[tip_index]

                    name = (user.name or "").split()[0] if user.name else "there"
                    has_phone = _is_valid_phone(user.phone)

                    delivered = False

                    # ── Email first (free primary channel) ──
                    if user.email:
                        try:
                            template = _jinja_env.get_template("morning_insight.html")
                            html = template.render(
                                name=name,
                                headline=tip["headline"],
                                body_text=tip["body"],
                                tip_text=tip["tip"],
                            )
                            plain = (
                                f"Good morning {name}! ☀️\n\n"
                                f"{tip['headline']}\n\n"
                                f"{tip['body']}\n\n"
                                f"💡 {tip['tip']}\n\n"
                                "Have a great day!\n"
                                "— Your SuoOps Team"
                            )
                            if _send_smtp_email(user.email, tip["subject"], html, plain):
                                stats["email_sent"] += 1
                                delivered = True
                        except Exception as e:
                            logger.warning("Morning insight email failed for user %s: %s", user.id, e)

                    # ── WhatsApp fallback: only when there's no email on file ──
                    if not delivered and not user.email and has_phone and template_name:
                        try:
                            from app.core.whatsapp import get_whatsapp_client

                            client = get_whatsapp_client()

                            # Template params: {{1}} = name, {{2}} = headline,
                            # {{3}} = tip body
                            components = [
                                {
                                    "type": "body",
                                    "parameters": [
                                        {"type": "text", "text": name},
                                        {"type": "text", "text": tip["headline"]},
                                        {"type": "text", "text": tip["body"]},
                                    ],
                                }
                            ]
                            ok = client.send_template(user.phone, template_name, template_lang, components)
                            if ok:
                                stats["whatsapp_sent"] += 1
                                delivered = True
                        except Exception as e:
                            logger.warning("Morning insight WA failed for user %s: %s", user.id, e)

                    if delivered:
                        _record_sent(db, user.id, tip_index)
                    else:
                        stats["failed"] += 1

                except Exception as e:
                    logger.warning("Morning insight failed for user %s: %s", user.id, e)
                    stats["failed"] += 1

            db.commit()

        logger.info(
            "Morning insights complete: wa=%d email=%d skipped=%d failed=%d",
            stats["whatsapp_sent"],
            stats["email_sent"],
            stats["skipped"],
            stats["failed"],
        )
        return {"success": True, **stats}

    except Exception as exc:
        logger.error("Morning insights task failed: %s", exc)
        raise
