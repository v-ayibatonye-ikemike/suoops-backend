"""Helpers for masking PII in logs (emails, phone numbers).

Full emails/phone numbers must never land in application logs — mask them so logs
stay useful for debugging without exposing customer PII.
"""

from __future__ import annotations


def mask_email(email: str | None) -> str:
    if not email:
        return "-"
    local, sep, domain = email.partition("@")
    if not sep:
        return "***"
    head = local[0] if local else ""
    return f"{head}***@{domain}"


def mask_phone(phone: str | None) -> str:
    if not phone:
        return "-"
    digits = "".join(ch for ch in phone if ch.isdigit())
    return f"***{digits[-4:]}" if len(digits) >= 4 else "***"
