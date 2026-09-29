#!/usr/bin/env python3
"""
Sync all users to Brevo List #12 (All Users).

This script fetches all users from the database and ensures they're
synced to the master Brevo list.

Run: python scripts/sync_brevo_all_users.py
"""

import os
import sys
import time

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.models import User

# Brevo configuration
BREVO_API_KEY = os.getenv("BREVO_CONTACTS_API_KEY") or getattr(settings, "BREVO_CONTACTS_API_KEY", None)
BREVO_LIST_ALL_USERS = 12

# Plan to segment list mapping
PLAN_TO_LIST = {
    "FREE": 7,  # Active Free Users
    "STARTER": 9,  # Starter Users
    "PRO": 10,  # Pro Users
}


def sync_user_to_brevo(user: User) -> bool:
    """Sync a single user to Brevo."""
    if not user.email:
        return False

    plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan)
    segment_list_id = PLAN_TO_LIST.get(plan_value.upper())

    # Always add to All Users, plus their plan-specific list
    list_ids = [BREVO_LIST_ALL_USERS]
    if segment_list_id:
        list_ids.append(segment_list_id)

    contact_data = {
        "email": user.email,
        "attributes": {
            "FIRSTNAME": user.name or "Customer",
            "PHONE": user.phone or "",
            "PLAN": plan_value,
            "INVOICE_BALANCE": getattr(user, "invoice_balance", 5),
            "BUSINESS_NAME": user.business_name or "",
        },
        "listIds": list_ids,
        "updateEnabled": True,
    }

    try:
        response = requests.post(
            "https://api.brevo.com/v3/contacts",
            headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
            json=contact_data,
            timeout=10.0,
        )

        if response.status_code in (200, 201, 204):
            return True
        elif response.status_code == 400 and "Contact already exist" in response.text:
            # Update existing contact
            encoded_email = user.email.replace("@", "%40")
            update_response = requests.put(
                f"https://api.brevo.com/v3/contacts/{encoded_email}",
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                json={"attributes": contact_data["attributes"], "listIds": list_ids},
                timeout=10.0,
            )
            return update_response.status_code in (200, 201, 204)
        else:
            print(f"  Failed {user.email}: {response.status_code} - {response.text[:100]}")
            return False

    except Exception as e:
        print(f"  Error {user.email}: {e}")
        return False


def main():
    """Main sync function."""
    if not BREVO_API_KEY:
        print("❌ BREVO_CONTACTS_API_KEY not configured")
        return

    print("🔄 Syncing all users to Brevo List #12 (All Users)...")
    print("=" * 50)

    # Create sync database connection
    database_url = os.getenv("DATABASE_URL") or settings.DATABASE_URL
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)

    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        # Get all users with email
        users = session.query(User).filter(User.email.isnot(None)).all()

        total = len(users)
        print(f"Found {total} users with email addresses")
        print()

        # Sync each user
        synced = 0
        failed = 0

        # Track by plan for summary
        plan_counts = {"FREE": 0, "STARTER": 0, "PRO": 0}

        for i, user in enumerate(users, 1):
            plan_value = user.plan.value if hasattr(user.plan, "value") else str(user.plan)

            success = sync_user_to_brevo(user)
            if success:
                synced += 1
                plan_counts[plan_value.upper()] = plan_counts.get(plan_value.upper(), 0) + 1
                print(f"  ✅ [{i}/{total}] {user.email} ({plan_value})")
            else:
                failed += 1
                print(f"  ❌ [{i}/{total}] {user.email}")

            # Rate limiting - Brevo allows ~10 requests/second
            if i % 5 == 0:
                time.sleep(0.5)

    engine.dispose()

    print()
    print("=" * 50)
    print(f"✅ Synced: {synced} users")
    print(f"❌ Failed: {failed} users")
    print()
    print("📊 By Plan:")
    print(f"   FREE (List #7):    {plan_counts.get('FREE', 0)}")
    print(f"   STARTER (List #9): {plan_counts.get('STARTER', 0)}")
    print(f"   PRO (List #10):    {plan_counts.get('PRO', 0)}")
    print()
    print(f"📋 All Users (List #12): {synced} total")


if __name__ == "__main__":
    main()
