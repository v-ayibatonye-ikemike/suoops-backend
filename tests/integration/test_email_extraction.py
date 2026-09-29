"""Tests for email extraction from WhatsApp-like free text.

Focused unit assertions; avoids printing progress in normal test runs.
"""

from __future__ import annotations

from app.bot.nlp_service import NLPService


def test_email_extraction():
    nlp = NLPService()

    test_cases = [
        {
            "text": "Invoice Jane 50000 for logo design email jane@example.com",
            "expected_email": "jane@example.com",
            "description": "Email at end",
        },
        {
            "text": "Invoice john@company.co.uk John Smith 75000 for consulting",
            "expected_email": "john@company.co.uk",
            "description": "Email in middle",
        },
        {
            "text": "Invoice Sarah 30k email sarah.doe@business.ng phone +2348087654321",
            "expected_email": "sarah.doe@business.ng",
            "description": "Email with phone",
        },
        {
            "text": "Invoice Mike 100000 for website development",
            "expected_email": None,
            "description": "No email provided",
        },
        {
            "text": "Invoice Peter info@suoops.com 45000 for marketing",
            "expected_email": "info@suoops.com",
            "description": "Business email",
        },
    ]

    passed = 0
    for case in test_cases:
        text = case["text"]
        expected = case["expected_email"]
        result = nlp.parse_text(text)
        extracted_email = result.entities.get("customer_email")
        assert (
            extracted_email == expected
        ), f"Email extraction mismatch for '{text}' (expected {expected}, got {extracted_email})"
        passed += 1

    assert passed == len(test_cases)


if __name__ == "__main__":  # pragma: no cover - manual invocation helper
    test_email_extraction()
