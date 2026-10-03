from __future__ import annotations

import hashlib
import re

from .types import AIMessage

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?234|0)[789]\d{9}(?!\d)")
_ACCOUNT_RE = re.compile(r"(?i)\b(?:account|acct|bank)\s*(?:number|no\.?)?\s*[:#-]?\s*(\d{10})\b")


def redact_text(text: str) -> str:
    """Remove common direct identifiers while preserving commerce amounts."""
    redacted = _EMAIL_RE.sub("[EMAIL_REDACTED]", text)
    redacted = _PHONE_RE.sub("[PHONE_REDACTED]", redacted)
    redacted = _ACCOUNT_RE.sub("[BANK_ACCOUNT_REDACTED]", redacted)
    return redacted


def redact_messages(messages: list[AIMessage]) -> list[AIMessage]:
    return [AIMessage(role=message.role, content=redact_text(message.content)) for message in messages]


def hash_messages(messages: list[AIMessage]) -> str:
    canonical = "\n".join(f"{message.role}:{message.content}" for message in messages)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
