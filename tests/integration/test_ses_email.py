#!/usr/bin/env python3
"""Integration test for Amazon SES email sending (gated by INTEGRATION env)."""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path  # noqa: F401 - kept for potential future path usage

import pytest

from app.core.config import get_settings


@pytest.mark.integration
def test_ses_email():
    """Test SES email sending (integration gated)."""
    if not os.getenv("INTEGRATION"):
        pytest.skip("Skipping SES email test (set INTEGRATION=1 to run)")
    print("📧 Testing Amazon SES Email Configuration...")
    print("=" * 60)

    settings = get_settings()

    # Get email configuration (you'll need to add these to config.py)
    smtp_host = getattr(settings, "SES_SMTP_HOST", None)
    smtp_port = getattr(settings, "SES_SMTP_PORT", 587)
    smtp_user = getattr(settings, "SES_SMTP_USER", None)
    smtp_password = getattr(settings, "SES_SMTP_PASSWORD", None)
    from_email = getattr(settings, "FROM_EMAIL", None)

    # Display configuration
    print("\n📋 Email Configuration:")
    print(f"  SMTP Host: {smtp_host or 'NOT SET'}")
    print(f"  SMTP Port: {smtp_port}")
    print(f"  SMTP User: {smtp_user[:20] + '...' if smtp_user else 'NOT SET'}")
    print(f"  From Email: {from_email or 'NOT SET'}")

    if not all([smtp_host, smtp_user, smtp_password, from_email]):
        pytest.skip("SES email configuration incomplete; skipping")

    # Get recipient email
    print("\n" + "=" * 60)
    # Loopback send to FROM_EMAIL to avoid interactive input
    to_email = from_email

    # Create test email
    print(f"\n📤 Sending test email to {to_email}...")

    msg = MIMEMultipart()
    msg["From"] = from_email
    msg["To"] = to_email
    msg["Subject"] = "Test Email from SuoOps - Amazon SES Configuration"

    body = f"""
Hello!

This is a test email from SuoOps to verify Amazon SES configuration.

Configuration Details:
- SMTP Host: {smtp_host}
- SMTP Port: {smtp_port}
- From Email: {from_email}
- Timestamp: {__import__('datetime').datetime.now().isoformat()}

If you received this email, your Amazon SES setup is working correctly! 🎉

Next steps:
1. Request production access in SES console (if in sandbox mode)
2. Configure email templates for invoices and notifications
3. Set up bounce and complaint handling

Best regards,
SuoOps Team

---
This is an automated test email. Please do not reply.
"""

    msg.attach(MIMEText(body, "plain"))

    try:
        server = smtplib.SMTP(smtp_host, smtp_port, timeout=5)
        server.starttls()
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
        server.quit()
    except Exception as e:  # noqa: BLE001
        pytest.fail(f"SES send failed: {e}")
    assert True


if __name__ == "__main__":  # pragma: no cover - manual invocation
    _ = test_ses_email()
