#!/usr/bin/env python3
"""
Promote a user to admin role.

Usage:
    python scripts/promote_admin.py --email ikemikeayibatonye94@gmail.com
    python scripts/promote_admin.py --user-id 34
"""

import argparse
import sys

from app.db.session import SessionLocal
from app.models.models import User


def promote_to_admin(email: str = None, user_id: int = None):
    """Promote a user to admin role."""
    db = SessionLocal()
    try:
        # Find user
        if email:
            user = db.query(User).filter(User.email == email).first()
            identifier = f"email={email}"
        elif user_id:
            user = db.query(User).filter(User.id == user_id).first()
            identifier = f"id={user_id}"
        else:
            print("❌ Must provide either --email or --user-id")
            return False

        if not user:
            print(f"❌ User not found: {identifier}")
            return False

        # Check current role
        if user.role == "admin":
            print(f"✅ User already admin: {user.email or user.phone} (ID: {user.id})")
            return True

        # Promote to admin
        old_role = user.role
        user.role = "admin"
        db.commit()

        print("✅ Promoted user to admin:")
        print(f"   ID: {user.id}")
        print(f"   Email: {user.email}")
        print(f"   Phone: {user.phone}")
        print(f"   Old role: {old_role}")
        print("   New role: admin")
        return True

    except Exception as e:
        db.rollback()
        print(f"❌ Error: {e}")
        return False
    finally:
        db.close()


def list_admins():
    """List all admin and staff users."""
    db = SessionLocal()
    try:
        admins = db.query(User).filter(User.role == "admin").all()
        staff = db.query(User).filter(User.role == "staff").all()

        print("\n=== ADMIN USERS ===")
        if admins:
            for u in admins:
                print(f"  ID: {u.id}, Email: {u.email}, Phone: {u.phone}")
        else:
            print("  (none)")

        print("\n=== STAFF USERS ===")
        if staff:
            for u in staff:
                print(f"  ID: {u.id}, Email: {u.email}, Phone: {u.phone}")
        else:
            print("  (none)")
        print()

    finally:
        db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Promote user to admin role")
    parser.add_argument("--email", help="User email address")
    parser.add_argument("--user-id", type=int, help="User ID")
    parser.add_argument("--list", action="store_true", help="List current admins and staff")

    args = parser.parse_args()

    if args.list:
        list_admins()
    elif args.email or args.user_id:
        success = promote_to_admin(email=args.email, user_id=args.user_id)
        sys.exit(0 if success else 1)
    else:
        parser.print_help()
        sys.exit(1)
