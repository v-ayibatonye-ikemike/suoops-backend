#!/usr/bin/env python3
"""Clear all data from the database."""

from sqlalchemy import text

from app.db.session import SessionLocal
from app.models.models import Invoice, InvoiceLine, User, WebhookEvent


def main():
    db = SessionLocal()
    try:
        print("\n" + "=" * 80)
        print("CLEARING DATABASE")
        print("=" * 80 + "\n")

        # Get counts before deletion
        user_count = db.query(User).count()
        invoice_count = db.query(Invoice).count()
        invoice_line_count = db.query(InvoiceLine).count()

        print(f"Found {user_count} users")
        print(f"Found {invoice_count} invoices")
        print(f"Found {invoice_line_count} invoice lines")

        if user_count == 0 and invoice_count == 0:
            print("\n✓ Database is already empty!\n")
            return

        print("\nDeleting all data...")

        # Use TRUNCATE CASCADE for faster deletion and automatic foreign key handling
        try:
            # Truncate all tables in one go with CASCADE (using actual table names)
            db.execute(text('TRUNCATE TABLE invoiceline, invoice, "user", webhookevent RESTART IDENTITY CASCADE'))
            db.commit()
            print("  ✓ Deleted all data from all tables")
            print("  ✓ Reset ID sequences")
        except Exception as e:
            # Fallback to individual deletes if TRUNCATE fails
            db.rollback()
            print(f"  ⚠ TRUNCATE failed: {e}")
            print("  Using individual DELETE statements...")

            # Start a fresh transaction
            db.begin()

            # Delete in correct order (respecting foreign keys)
            # 1. Delete invoice lines first
            deleted = db.query(InvoiceLine).delete(synchronize_session=False)
            db.commit()
            print(f"  ✓ Deleted {deleted} invoice lines")

            # 2. Delete invoices
            db.begin()
            deleted = db.query(Invoice).delete(synchronize_session=False)
            db.commit()
            print(f"  ✓ Deleted {deleted} invoices")

            # 3. Delete webhook events if table exists
            try:
                db.begin()
                deleted = db.query(WebhookEvent).delete(synchronize_session=False)
                db.commit()
                print(f"  ✓ Deleted {deleted} webhook events")
            except Exception as ex:
                db.rollback()
                print(f"  ⚠ Could not delete webhook events: {ex}")

            # 4. Delete users
            db.begin()
            deleted = db.query(User).delete(synchronize_session=False)
            db.commit()
            print(f"  ✓ Deleted {deleted} users")

            # Reset auto-increment sequences (PostgreSQL)
            try:
                db.execute(text("ALTER SEQUENCE user_id_seq RESTART WITH 1"))
                db.execute(text("ALTER SEQUENCE invoice_id_seq RESTART WITH 1"))
                db.execute(text("ALTER SEQUENCE invoiceline_id_seq RESTART WITH 1"))
                db.commit()
                print("  ✓ Reset ID sequences")
            except Exception as e:
                db.rollback()
                print(f"  ⚠ Could not reset sequences: {e}")

        print("\n" + "=" * 80)
        print("✓ DATABASE CLEARED SUCCESSFULLY")
        print("=" * 80 + "\n")

    except Exception as e:
        db.rollback()
        print(f"\n✗ Error clearing database: {e}\n")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
