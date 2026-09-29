from __future__ import annotations


def validate_phone(phone: str) -> bool:
    digits = "".join(ch for ch in phone if ch.isdigit())
    return 8 < len(digits) < 16
