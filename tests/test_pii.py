"""PII masking helpers for logs."""

from app.utils.pii import mask_email, mask_phone


def test_mask_email():
    assert mask_email("jane.doe@example.com") == "j***@example.com"
    assert mask_email(None) == "-"
    assert mask_email("noatsign") == "***"


def test_mask_phone():
    assert mask_phone("+2348031234567") == "***4567"
    assert mask_phone("0803") == "***0803"
    assert mask_phone("12") == "***"
    assert mask_phone(None) == "-"
