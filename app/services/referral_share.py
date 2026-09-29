"""Helpers for sharing referral codes via WhatsApp / email."""

from __future__ import annotations

from urllib.parse import quote

from app.core.config import settings


def build_referral_link(code: str) -> str:
    """Build the public signup URL with the referral code pre-filled."""
    base = (getattr(settings, "FRONTEND_URL", None) or "https://suoops.com").rstrip("/")
    return f"{base}/register?ref={code}"


def build_referral_share_text(name: str, code: str) -> str:
    """Pre-written invite a user can forward to friends.

    Used both for the in-app "Share via WhatsApp" button and for the
    plain-language invitation included in the bot's referral card.
    """
    link = build_referral_link(code)
    sender = (name or "I").split()[0] if name else "I"
    return (
        f"Hey! {sender} uses SuoOps to send invoices and get paid faster. "
        f"You get 2 free invoices to try it — no card needed. "
        f"Sign up with my link and we both win:\n{link}"
    )


def build_whatsapp_share_url(name: str, code: str) -> str:
    """Build a wa.me deep link that opens WhatsApp with the invite pre-typed.

    Tapping this on mobile opens the chooser to pick a contact; on desktop it
    opens WhatsApp Web. The friend just hits send.
    """
    text = build_referral_share_text(name, code)
    return f"https://wa.me/?text={quote(text)}"


def build_referral_whatsapp_message(name: str, code: str) -> str:
    """Friendly WhatsApp message a user receives showing their referral code.

    Sent shortly after signup so users have their code without hunting for it,
    and any time the user types ``referral`` / ``my code`` in the bot.
    """
    link = build_referral_link(code)
    share_url = build_whatsapp_share_url(name, code)
    first_name = (name or "there").split()[0]
    return (
        f"🎁 *Earn ₦488 per friend, {first_name}!*\n\n"
        f"Share SuoOps with other business owners. When a friend you invite "
        f"upgrades to *Pro*, you instantly earn *₦488* — paid out monthly in cash.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"*Your referral code:* `{code}`\n"
        f"*Your invite link:*\n{link}\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📲 *Share in 1 tap:*\n{share_url}\n\n"
        f"_(Opens WhatsApp with your invite already typed — pick a friend and hit send.)_\n\n"
        f"💡 Track your earnings anytime — just type *referral*."
    )
