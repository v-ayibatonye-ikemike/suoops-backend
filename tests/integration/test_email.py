"""Test email sending with Brevo SMTP.

Marked integration; skipped unless INTEGRATION=1 and SMTP settings present.
"""

from __future__ import annotations

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import pytest

from app.core.config import settings


@pytest.mark.integration
def test_email():
    """Connectivity smoke test for SMTP settings.

    Skipped automatically if SMTP credentials are not configured to avoid hanging in CI.
    """
    if not os.getenv("INTEGRATION"):
        pytest.skip("Skipping SMTP connectivity test (set INTEGRATION=1 to run)")
    if not (settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD and settings.FROM_EMAIL):
        pytest.skip("SMTP credentials not configured; skipping")

    msg = MIMEMultipart()
    msg["From"] = settings.FROM_EMAIL
    test_recipient = settings.FROM_EMAIL  # loopback
    msg["To"] = test_recipient
    msg["Subject"] = "SMTP Loopback Test"

    body = "SMTP connectivity loopback test from SuoOps backend."
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=5) as server:
            server.starttls()
            server.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            server.send_message(msg)
    except Exception as e:  # noqa: BLE001
        raise AssertionError(f"SMTP send failed: {e}") from e


if __name__ == "__main__":  # pragma: no cover - manual run helper
    test_email()
