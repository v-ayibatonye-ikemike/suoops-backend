#!/usr/bin/env python3
"""List all registered users from the database."""

from sqlalchemy import desc

from app.db.session import SessionLocal
from app.models.models import User


def main():
    db = SessionLocal()
    try:
        users = db.query(User).order_by(desc(User.created_at)).all()

        print("\n" + "=" * 80)
        print("REGISTERED USERS")
        print("=" * 80 + "\n")

        if not users:
            print("No users found in database.\n")
            return

        for i, user in enumerate(users, 1):
            print(f'{i}. {user.business_name or "N/A"}')
            print(f"   Email: {user.email}")
            print(f'   Phone: {user.phone or "N/A"}')
            print(f"   Plan: {user.subscription_plan}")
            print(f"   Usage: {user.invoices_this_month}/{user.invoice_limit} invoices this month")
            print(f"   Created: {user.created_at}")
            print(f"   ID: {user.id}")
            print("-" * 80)

        print(f"\nTotal Users: {len(users)}\n")
        print("NOTE: Passwords are hashed and cannot be displayed.")
        print("To login, use the email and the password you set during registration.\n")

    finally:
        db.close()


if __name__ == "__main__":
    main()
