"""Shared phone normalization utilities.

Single source of truth for phone number formatting and variant generation.
Used across auth, bot, invoicing, and notification modules.
"""

from __future__ import annotations


def normalize_phone(phone: str) -> str:
    """Normalize a phone number to E.164 format (+234...).

    Handles Nigerian formats: 08012345678, 2348012345678, +2348012345678, 8012345678.
    For international numbers, adds '+' prefix if missing.
    Returns the original input for empty/unparseable values.
    """
    if not phone:
        return phone

    sanitized = phone.strip().replace(" ", "").replace("-", "")
    if not sanitized:
        return phone

    if sanitized.startswith("+"):
        return sanitized

    # Remove all non-digit characters for analysis
    digits = "".join(ch for ch in sanitized if ch.isdigit())
    if not digits:
        return phone

    # Nigerian: 234XXXXXXXXXX (13 digits)
    if digits.startswith("234") and len(digits) == 13:
        return "+" + digits

    # Nigerian local: 0[789]XXXXXXXXX (11 digits)
    if digits.startswith("0") and len(digits) == 11 and digits[1] in "789":
        return "+234" + digits[1:]

    # Nigerian without leading zero: [789]XXXXXXXXX (10 digits)
    if len(digits) == 10 and digits[0] in "789":
        return "+234" + digits

    # Generic international: just add '+' (E.164 country codes never start with 0,
    # so a leading-0 string here is malformed junk — leave it for the caller to
    # reject rather than fabricating an invalid '+0…' number).
    if len(digits) >= 7 and not digits.startswith("0"):
        return "+" + digits

    return phone


def get_phone_variants(phone: str) -> set[str]:
    """Return all plausible format variants of a Nigerian phone number.

    Used for database lookups where the stored format may differ from the
    incoming format (e.g., +2348012345678 vs 08012345678).
    """
    if not phone:
        return set()

    clean_digits = "".join(ch for ch in phone if ch.isdigit())
    candidates: set[str] = {phone}

    if phone.startswith("+"):
        candidates.add(phone[1:])

    if clean_digits:
        candidates.add(clean_digits)
        if clean_digits.startswith("234"):
            candidates.add(f"+{clean_digits}")
            # Local format: 0XXXXXXXXXX
            candidates.add("0" + clean_digits[3:])
        elif clean_digits.startswith("0"):
            intl = "234" + clean_digits[1:]
            candidates.add(intl)
            candidates.add(f"+{intl}")

    return {c for c in candidates if c}
