"""
One-time backfill: set channel on historical invoices that have NULL channel.

For revenue invoices created via the dashboard (no channel set), we default to 'dashboard'.
For expense invoices, the channel was already being tracked correctly.

Run: python -m scripts.backfill_invoice_channel
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.db.session import SessionLocal


def backfill():
    db = SessionLocal()
    try:
        # Count NULL-channel invoices
        result = db.execute(text("SELECT COUNT(*) FROM invoice WHERE channel IS NULL"))
        null_count = result.scalar()
        print(f"Found {null_count} invoices with NULL channel")

        if null_count == 0:
            print("Nothing to backfill.")
            return

        # Set all NULL-channel invoices to 'dashboard'
        # We can't reliably distinguish WhatsApp vs dashboard for historical data
        # since the channel field was never set. Going forward, new invoices
        # will have the correct channel.
        result = db.execute(text("UPDATE invoice SET channel = 'dashboard' WHERE channel IS NULL"))
        db.commit()
        print(f"Updated {result.rowcount} invoices to channel='dashboard'")

    finally:
        db.close()


if __name__ == "__main__":
    backfill()
