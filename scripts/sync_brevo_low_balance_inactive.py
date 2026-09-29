#!/usr/bin/env python3
"""
Sync Low Balance and Inactive users to Brevo segment lists.

Run from suoops-backend directory:
    python scripts/sync_brevo_low_balance_inactive.py

Lists:
- #5: Inactive Users (no activity in 30+ days)
- #6: Low Balance Users (≤2 invoices remaining)
"""

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Configuration
DATABASE_URL = os.getenv("DATABASE_URL", "").replace("postgres://", "postgresql://", 1)
BREVO_API_KEY = os.getenv("BREVO_CONTACTS_API_KEY")

# Brevo List IDs
BREVO_LIST_INACTIVE = 5
BREVO_LIST_LOW_BALANCE = 6

# Thresholds
LOW_BALANCE_THRESHOLD = 2  # ≤2 invoices = low balance
INACTIVE_DAYS = 30  # No activity in 30+ days = inactive


async def add_contact_to_list(email: str, list_id: int) -> bool:
    """Add a contact to a Brevo list."""
    if not BREVO_API_KEY or not email:
        return False

    async with httpx.AsyncClient() as client:
        # First, try to add to list (contact may already exist)
        response = await client.post(
            f"https://api.brevo.com/v3/contacts/lists/{list_id}/contacts/add",
            headers={
                "api-key": BREVO_API_KEY,
                "Content-Type": "application/json",
            },
            json={"emails": [email]},
            timeout=10.0,
        )

        if response.status_code in (200, 201, 204):
            return True

        # If contact doesn't exist, create it first
        if response.status_code == 400:
            create_response = await client.post(
                "https://api.brevo.com/v3/contacts",
                headers={
                    "api-key": BREVO_API_KEY,
                    "Content-Type": "application/json",
                },
                json={
                    "email": email,
                    "listIds": [list_id],
                    "updateEnabled": True,
                },
                timeout=10.0,
            )
            return create_response.status_code in (200, 201, 204)

        return False


async def sync_low_balance_users(session) -> dict:
    """Find and sync users with low invoice balance."""
    from app.models.models import User

    # Find users with balance <= threshold
    low_balance_users = (
        session.query(User)
        .filter(
            User.email.isnot(None),
            User.email != "",
            User.invoice_balance <= LOW_BALANCE_THRESHOLD,
        )
        .all()
    )

    print(f"\n📊 Found {len(low_balance_users)} users with low balance (≤{LOW_BALANCE_THRESHOLD} invoices)")

    synced = 0
    failed = 0

    for user in low_balance_users:
        try:
            success = await add_contact_to_list(user.email, BREVO_LIST_LOW_BALANCE)
            if success:
                synced += 1
                print(f"  ✅ {user.email} (balance: {user.invoice_balance})")
            else:
                failed += 1
                print(f"  ❌ {user.email} - failed to sync")
        except Exception as e:
            failed += 1
            print(f"  ❌ {user.email} - error: {e}")

    return {"synced": synced, "failed": failed, "total": len(low_balance_users)}


async def sync_inactive_users(session) -> dict:
    """Find and sync inactive users (no invoices in 30+ days)."""
    from sqlalchemy import func

    from app.models.models import Invoice, User

    cutoff_date = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)

    # Find users who:
    # 1. Have created invoices before (were active)
    # 2. Haven't created any invoices in the last 30 days

    # Get users with their last invoice date
    subquery = (
        session.query(Invoice.issuer_id, func.max(Invoice.created_at).label("last_invoice_date"))
        .group_by(Invoice.issuer_id)
        .subquery()
    )

    inactive_users = (
        session.query(User)
        .join(subquery, User.id == subquery.c.issuer_id)
        .filter(
            User.email.isnot(None),
            User.email != "",
            subquery.c.last_invoice_date < cutoff_date,
        )
        .all()
    )

    print(f"\n📊 Found {len(inactive_users)} inactive users (no activity in {INACTIVE_DAYS}+ days)")

    synced = 0
    failed = 0

    for user in inactive_users:
        try:
            success = await add_contact_to_list(user.email, BREVO_LIST_INACTIVE)
            if success:
                synced += 1
                print(f"  ✅ {user.email}")
            else:
                failed += 1
                print(f"  ❌ {user.email} - failed to sync")
        except Exception as e:
            failed += 1
            print(f"  ❌ {user.email} - error: {e}")

    return {"synced": synced, "failed": failed, "total": len(inactive_users)}


async def main():
    print("=" * 60)
    print("🔄 Syncing Low Balance & Inactive Users to Brevo")
    print("=" * 60)

    if not DATABASE_URL:
        print("❌ DATABASE_URL not set")
        sys.exit(1)

    if not BREVO_API_KEY:
        print("❌ BREVO_CONTACTS_API_KEY not set")
        sys.exit(1)

    # Connect to database
    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Sync low balance users
        low_balance_result = await sync_low_balance_users(session)

        # Sync inactive users
        inactive_result = await sync_inactive_users(session)

        # Summary
        print("\n" + "=" * 60)
        print("📋 SYNC SUMMARY")
        print("=" * 60)
        print("\n🔋 Low Balance Users (List #6):")
        print(f"   Total found: {low_balance_result['total']}")
        print(f"   Synced: {low_balance_result['synced']}")
        print(f"   Failed: {low_balance_result['failed']}")

        print("\n😴 Inactive Users (List #5):")
        print(f"   Total found: {inactive_result['total']}")
        print(f"   Synced: {inactive_result['synced']}")
        print(f"   Failed: {inactive_result['failed']}")

        print("\n✅ Sync complete!")
        print("\n📧 You can now create campaigns targeting these lists in Brevo")

    finally:
        session.close()


if __name__ == "__main__":
    asyncio.run(main())
