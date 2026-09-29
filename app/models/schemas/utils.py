"""Common utility functions for schemas."""

from decimal import Decimal


def format_amount(value: Decimal | None) -> str | None:
    """Format Decimal values without trailing zeros for API responses."""
    if value is None:
        return None

    normalized = value.normalize()
    if normalized == normalized.to_integral():
        normalized = normalized.quantize(Decimal("1"))

    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
