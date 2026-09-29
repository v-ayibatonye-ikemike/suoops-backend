"""Test Brevo email configuration."""

import asyncio

from app.core.config import settings
from app.services.notification.service import NotificationService


async def test_brevo_config():
    """Test that Brevo SMTP configuration is set up correctly."""
    service = NotificationService()

    # Test _get_smtp_config method
    smtp_config = service._get_smtp_config()

    if not smtp_config:
        print("❌ SMTP configuration is not set up!")
        print(f"EMAIL_PROVIDER: {settings.EMAIL_PROVIDER}")
        print(f"FROM_EMAIL: {settings.FROM_EMAIL}")
        print(f"BREVO_API_KEY: {'Set' if settings.BREVO_API_KEY else 'Not set'}")
        return

    print("✅ SMTP Configuration:")
    print(f"   Provider: {smtp_config.get('provider')}")
    print(f"   Host: {smtp_config.get('host')}")
    print(f"   Port: {smtp_config.get('port')}")
    print(f"   User: {smtp_config.get('user')}")
    print(f"   Password: {'*' * 20} (hidden)")
    print()

    # Try sending a test email
    print("Sending test email...")
    try:
        result = await service.send_email(
            to_email=settings.FROM_EMAIL or "test@example.com",  # Send to self
            subject="Brevo SMTP Test from SuoOps",
            body="This is a test email to verify Brevo SMTP configuration is working correctly.",
        )

        if result:
            print("✅ Test email sent successfully!")
        else:
            print("❌ Failed to send test email (returned False)")
    except Exception as e:
        print(f"❌ Error sending test email: {e}")


if __name__ == "__main__":
    asyncio.run(test_brevo_config())
