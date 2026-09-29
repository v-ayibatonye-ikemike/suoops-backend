#!/usr/bin/env python3
"""Check which user has a specific phone number."""

import os
import sys

# Add parent to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.models.models import User

PHONE_TO_CHECK = "+2348078557662"


def main():
    db = SessionLocal()
    try:
        # Find user with this phone
        user = db.query(User).filter(User.phone == PHONE_TO_CHECK).first()

        if user:
            print(f"\n📱 Phone {PHONE_TO_CHECK} is linked to:")
            print(f"   User ID: {user.id}")
            print(f"   Email: {user.email}")
            print(f"   Name: {user.first_name} {user.last_name}")
            print(f"   Business: {user.business_name}")
            print(f"   Phone Verified: {user.phone_verified}")
            print(f"   Created: {user.created_at}")
            print()

            # Option to clear the phone
            if not user.phone_verified:
                print("⚠️  Phone is NOT verified on this account.")
                print("   This is why WhatsApp bot can't identify the business.")
                print()
                print("   To free up this phone number, run:")
                print(f"   python scripts/clear_phone.py {user.id}")
        else:
            print(f"\n❌ Phone {PHONE_TO_CHECK} not found in database")

    finally:
        db.close()


if __name__ == "__main__":
    main()
