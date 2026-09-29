"""Test SMTP configuration in production."""

import asyncio

from app.core.config import settings
from app.services.notification.service import NotificationService


async def test_smtp_config():
    """Test SMTP configuration."""
    print("\n=== Testing SMTP Configuration ===")
    print(f"EMAIL_PROVIDER: {settings.EMAIL_PROVIDER}")
    print(f"BREVO_SMTP_LOGIN: {settings.BREVO_SMTP_LOGIN}")
    print(f"BREVO_API_KEY: {'Set' if settings.BREVO_API_KEY else 'Not set'}")
    print(f"FROM_EMAIL: {settings.FROM_EMAIL}")

    service = NotificationService()
    config = service._get_smtp_config()

    if config:
        print("\n✅ SMTP Configuration Valid:")
        print(f"   Provider: {config.get('provider')}")
        print(f"   Host: {config.get('host')}")
        print(f"   Port: {config.get('port')}")
        print(f"   User: {config.get('user')}")
        print(f"   Password: {'*' * 20}")

        # Try sending a test email
        print("\n=== Sending Test Email ===")
        try:
            result = await service.send_email(
                to_email=settings.FROM_EMAIL,
                subject="SuoOps SMTP Test",
                body="This is a test email to verify Brevo SMTP is working correctly.",
            )
            if result:
                print("✅ Test email sent successfully!")
            else:
                print("❌ Email sending returned False")
        except Exception as e:
            print(f"❌ Error sending email: {e}")
            import traceback

            traceback.print_exc()
    else:
        print("\n❌ SMTP Configuration Invalid")


if __name__ == "__main__":
    asyncio.run(test_smtp_config())
