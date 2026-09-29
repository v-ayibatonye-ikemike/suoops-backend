#!/usr/bin/env python3
"""
One-time script to sync all existing users to their Brevo segment lists.

Run this once to populate the segment lists for existing users.
New users will be synced automatically via the updated brevo_service.py

Usage:
    python scripts/sync_brevo_segments.py
"""

import asyncio
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

# Brevo list IDs
BREVO_LIST_ALL_USERS = 12
BREVO_LIST_STARTER = 9
BREVO_LIST_PRO = 10
BREVO_LIST_ACTIVE_FREE = 7

# Get API key from env
BREVO_API_KEY = os.environ.get("BREVO_CONTACTS_API_KEY")
DATABASE_URL = os.environ.get("DATABASE_URL")


async def add_contacts_to_list(emails: list[str], list_id: int, list_name: str) -> int:
    """Add multiple contacts to a Brevo list (in batches of 150)."""
    if not emails:
        print(f"  No contacts for {list_name}")
        return 0

    print(f"  Adding {len(emails)} contacts to {list_name} (list #{list_id})...")

    BATCH_SIZE = 150
    total_added = 0

    async with httpx.AsyncClient() as client:
        for i in range(0, len(emails), BATCH_SIZE):
            batch = emails[i : i + BATCH_SIZE]
            batch_num = (i // BATCH_SIZE) + 1
            total_batches = (len(emails) + BATCH_SIZE - 1) // BATCH_SIZE

            response = await client.post(
                f"https://api.brevo.com/v3/contacts/lists/{list_id}/contacts/add",
                headers={"api-key": BREVO_API_KEY, "Content-Type": "application/json"},
                json={"emails": batch},
                timeout=30.0,
            )

            if response.status_code in (200, 201, 204):
                total_added += len(batch)
                if total_batches > 1:
                    print(f"    Batch {batch_num}/{total_batches}: Added {len(batch)} contacts")
            else:
                print(f"    ❌ Batch {batch_num} failed: {response.status_code} - {response.text}")

    print(f"  ✅ Successfully added {total_added} contacts to {list_name}")
    return total_added


async def main():
    print("=" * 60)
    print("Syncing existing users to Brevo segment lists")
    print("=" * 60)

    if not BREVO_API_KEY:
        print("❌ BREVO_CONTACTS_API_KEY not set!")
        print("Export it: export BREVO_CONTACTS_API_KEY='your-key'")
        sys.exit(1)

    if not DATABASE_URL:
        print("❌ DATABASE_URL not set!")
        print("Export it: export DATABASE_URL='postgresql://...'")
        sys.exit(1)

    # Install psycopg if not present
    try:
        import psycopg
    except ImportError:
        print("Installing psycopg...")
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "psycopg[binary]",
            "-q",
        )
        return_code = await process.wait()
        if return_code:
            raise RuntimeError(f"psycopg installation failed with exit code {return_code}")
        import psycopg

    # Convert postgres:// to postgresql:// if needed
    db_url = DATABASE_URL.replace("postgres://", "postgresql://")

    try:
        conn = psycopg.connect(db_url)
        cursor = conn.cursor()

        # Get FREE users with email
        cursor.execute("SELECT email FROM \"user\" WHERE plan = 'FREE' AND email IS NOT NULL AND email != ''")
        free_emails = [r[0] for r in cursor.fetchall()]

        # Get STARTER users with email
        cursor.execute("SELECT email FROM \"user\" WHERE plan = 'STARTER' AND email IS NOT NULL AND email != ''")
        starter_emails = [r[0] for r in cursor.fetchall()]

        # Get PRO users with email
        cursor.execute("SELECT email FROM \"user\" WHERE plan = 'PRO' AND email IS NOT NULL AND email != ''")
        pro_emails = [r[0] for r in cursor.fetchall()]

        cursor.close()
        conn.close()

        print("\nFound:")
        print(f"  - {len(free_emails)} FREE users")
        print(f"  - {len(starter_emails)} STARTER users")
        print(f"  - {len(pro_emails)} PRO users")
        print()

        # Sync to segment lists
        total = 0

        print("Syncing FREE users to 'Active Free Users' list...")
        total += await add_contacts_to_list(free_emails, BREVO_LIST_ACTIVE_FREE, "Active Free Users")

        print("\nSyncing STARTER users to 'Starter Users Pro Upsell' list...")
        total += await add_contacts_to_list(starter_emails, BREVO_LIST_STARTER, "Starter Users Pro Upsell")

        print("\nSyncing PRO users to 'Pro Users Retention' list...")
        total += await add_contacts_to_list(pro_emails, BREVO_LIST_PRO, "Pro Users Retention")

        print("\n" + "=" * 60)
        print(f"✅ Complete! Synced {total} contacts to segment lists.")
        print("=" * 60)

    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
