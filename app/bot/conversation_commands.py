"""Conservative command matching; invoice values are never rewritten."""

from __future__ import annotations

import re
from difflib import get_close_matches

_ALIASES = {
    "status": {
        "status", "my invoices", "my invoice", "balance", "my balance", "how many invoices", "invoice balance",
        "how much have i collected today", "cash today", "cash in today",
    },
    "owed": {
        "owed", "pending", "unpaid", "outstanding", "who owes me", "who owes me money", "owes me",
        "who hasn't paid me", "who has not paid me", "unpaid invoices", "my unpaid invoices", "outstanding invoices",
    },
    "orders": {"orders", "my orders", "order status", "what needs dispatch"},
    "report": {"report", "analytics", "insights", "dashboard", "stats", "my stats", "my report", "business report"},
    "help": {"help", "menu", "guide", "how", "instructions", "commands", "help me", "what can you do"},
    "full guide": {"full guide", "invoice guide", "full help"},
    "account": {"account", "me", "whoami", "my account", "profile", "my profile"},
    "wallet": {"wallet", "my wallet", "wallet balance"},
    "storefront": {"storefront", "my storefront", "store link", "my store link", "send my store link"},
    "tax report": {"tax report", "tax", "my tax", "download tax", "tax pdf"},
    "products": {"products", "my products", "stock", "catalog", "inventory", "shop", "my stock"},
    "continue": {"continue", "resume", "my draft", "resume draft", "where was i", "what next", "what do i do next"},
    "cancel draft": {"cancel", "cancel draft", "discard draft"},
    "invoice": {
        "invoice", "new invoice", "create invoice", "create an invoice", "make invoice", "make an invoice",
        "raise invoice", "raise an invoice", "issue invoice", "issue an invoice", "send invoice", "send an invoice",
        "bill", "bill someone", "bill a customer", "send bill", "send a bill",
    },
    "support": {"support", "human support", "talk to a person", "i need support", "i need help"},
    "setup": {"setup", "get started"},
    "invoice actions": {"mark paid", "mark as paid", "remind", "send reminder", "cancel invoice", "delete invoice"},
}
_LOOKUP = {alias: command for command, aliases in _ALIASES.items() for alias in aliases}
_TYPO_SAFE = (
    "status", "report", "orders", "help", "account", "wallet", "storefront", "products", "inventory", "unpaid",
    "balance", "analytics",
)
_POLITE_PREFIX = re.compile(r"^(?:please|pls|can you|could you|would you|show me|show|i want to|i would like to)\s+")


def command_text(text: str) -> str:
    return " ".join(text.lower().replace("’", "'").split()).strip(" .!?,")


def match_command(text: str) -> str | None:
    if len(text) > 500:
        return None
    normalized = command_text(text)
    for _ in range(4):
        normalized = _POLITE_PREFIX.sub("", normalized, count=1)
    normalized = re.sub(r",?\s+(?:please|pls)$", "", normalized).strip(" .!?,")
    if normalized in _LOOKUP:
        return _LOOKUP[normalized]
    # Only a single unambiguous read command may be typo-corrected. Never money,
    # customer data, payment confirmations, deletions, or arbitrary sentences.
    if re.fullmatch(r"[a-z]{4,20}", normalized):
        candidates = get_close_matches(normalized, _TYPO_SAFE, n=2, cutoff=0.8)
        if len(candidates) == 1:
            return _LOOKUP[candidates[0]]
    return None
