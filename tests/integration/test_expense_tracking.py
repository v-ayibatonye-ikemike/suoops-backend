"""
Comprehensive test for expense tracking system.

Tests:
1. Manual expense creation via API
2. NLP text extraction service
3. Expense summaries by category
4. Profit calculation (Revenue - Expenses)
5. Tax reporting integration
"""

from datetime import date
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.expense import Expense
from app.models.models import User
from app.services.expense_nlp_service import ExpenseNLPService
from app.services.tax_reporting_service import (
    compute_actual_profit_by_date_range,
    compute_expenses_by_date_range,
    compute_revenue_by_date_range,
)

# Create test database session
engine = create_engine(settings.DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)


def test_nlp_service():
    """Test NLP extraction of expense details from text"""
    print("\n" + "=" * 60)
    print("TEST 1: NLP Text Extraction")
    print("=" * 60)

    service = ExpenseNLPService()

    test_cases = [
        "Expense: ₦2,000 for internet data on Nov 10",
        "₦5,000 market rent",
        "Paid ₦15,000 for shop rent today",
        "Spent 3000 naira on transport yesterday",
        "₦50k for office equipment",
        "2500 utilities bill",
    ]

    for text in test_cases:
        try:
            result = service.extract_expense(text)
            print(f"\n📝 Input: {text}")
            print(f"   Amount: ₦{result['amount']}")
            print(f"   Category: {result['category']}")
            print(f"   Date: {result['date']}")
            print(f"   Description: {result['description']}")
            print("   ✅ Success")
        except Exception as e:
            print(f"\n❌ Failed: {text}")
            print(f"   Error: {e}")

    print("\n" + "=" * 60)


def test_manual_expense_creation():
    """Test creating expenses manually via database"""
    print("\n" + "=" * 60)
    print("TEST 2: Manual Expense Creation")
    print("=" * 60)

    db = SessionLocal()
    try:
        # Get first user
        user = db.query(User).first()
        if not user:
            print("❌ No users found. Please create a user first.")
            return

        print(f"\n👤 Testing with user: {user.email or user.phone}")

        # Create test expenses
        test_expenses = [
            {
                "amount": Decimal("5000.00"),
                "date": date(2025, 11, 1),
                "category": "rent",
                "description": "November shop rent",
                "merchant": "Landlord",
            },
            {
                "amount": Decimal("2500.00"),
                "date": date(2025, 11, 5),
                "category": "utilities",
                "description": "Electricity bill",
                "merchant": "NEPA",
            },
            {
                "amount": Decimal("1500.00"),
                "date": date(2025, 11, 8),
                "category": "data_internet",
                "description": "MTN data bundle",
                "merchant": "MTN",
            },
            {
                "amount": Decimal("3000.00"),
                "date": date(2025, 11, 10),
                "category": "transport",
                "description": "Fuel for deliveries",
                "merchant": "Total",
            },
        ]

        created_count = 0
        for exp_data in test_expenses:
            expense = Expense(
                user_id=user.id,
                input_method="manual",
                channel="dashboard",
                verified=True,
                **exp_data,
            )
            db.add(expense)
            created_count += 1
            print(f"\n✅ Created: ₦{exp_data['amount']} - {exp_data['category']} - {exp_data['description']}")

        db.commit()
        print(f"\n🎉 Successfully created {created_count} test expenses")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        db.rollback()
    finally:
        db.close()

    print("\n" + "=" * 60)


def test_expense_summaries():
    """Test expense summary aggregation by category"""
    print("\n" + "=" * 60)
    print("TEST 3: Expense Summaries by Category")
    print("=" * 60)

    db = SessionLocal()
    try:
        # Get first user
        user = db.query(User).first()
        if not user:
            print("❌ No users found.")
            return

        # Get November 2025 expenses
        start_date = date(2025, 11, 1)
        end_date = date(2025, 11, 30)

        expenses = (
            db.query(Expense)
            .filter(
                Expense.user_id == user.id,
                Expense.date >= start_date,
                Expense.date <= end_date,
            )
            .all()
        )

        if not expenses:
            print("❌ No expenses found for November 2025")
            return

        # Aggregate by category
        by_category = {}
        total = Decimal("0")

        for expense in expenses:
            cat = expense.category
            by_category[cat] = by_category.get(cat, Decimal("0")) + expense.amount
            total += expense.amount

        print("\n📊 November 2025 Expense Summary")
        print(f"   Period: {start_date} to {end_date}")
        print(f"   Total Expenses: ₦{total:,.2f}")
        print(f"   Number of Expenses: {len(expenses)}")
        print("\n   By Category:")

        for category, amount in sorted(by_category.items(), key=lambda x: x[1], reverse=True):
            percentage = (amount / total * 100) if total > 0 else 0
            print(f"   • {category:20s} ₦{amount:>10,.2f} ({percentage:5.1f}%)")

        print("\n✅ Summary generated successfully")

    except Exception as e:
        print(f"\n❌ Error: {e}")
    finally:
        db.close()

    print("\n" + "=" * 60)


def test_profit_calculation():
    """Test actual profit calculation (Revenue - Expenses)"""
    print("\n" + "=" * 60)
    print("TEST 4: Profit Calculation (Revenue - Expenses)")
    print("=" * 60)

    db = SessionLocal()
    try:
        # Get first user
        user = db.query(User).first()
        if not user:
            print("❌ No users found.")
            return

        # November 2025
        start_date = date(2025, 11, 1)
        end_date = date(2025, 11, 30)

        # Get revenue
        revenue = compute_revenue_by_date_range(
            db=db,
            user_id=user.id,
            start_date=start_date,
            end_date=end_date,
            basis="paid",
        )

        # Get expenses
        expenses = compute_expenses_by_date_range(
            db=db,
            user_id=user.id,
            start_date=start_date,
            end_date=end_date,
        )

        # Calculate profit
        actual_profit = compute_actual_profit_by_date_range(
            db=db,
            user_id=user.id,
            start_date=start_date,
            end_date=end_date,
            basis="paid",
        )

        print("\n💰 November 2025 Financial Summary")
        print(f"   Period: {start_date} to {end_date}")
        print(f"   Total Revenue:  ₦{revenue:>12,.2f}")
        print(f"   Total Expenses: ₦{expenses:>12,.2f}")
        print(f"   {'─' * 40}")
        print(f"   Actual Profit:  ₦{actual_profit:>12,.2f}")

        if expenses > 0:
            expense_ratio = (expenses / revenue * 100) if revenue > 0 else 0
            print(f"\n   Expense to Revenue Ratio: {expense_ratio:.1f}%")

        if actual_profit >= 0:
            print("\n   ✅ Profitable business!")
        else:
            print("\n   ⚠️  Operating at a loss")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        db.close()

    print("\n" + "=" * 60)


def test_expense_stats():
    """Test comprehensive expense statistics"""
    print("\n" + "=" * 60)
    print("TEST 5: Comprehensive Expense Statistics")
    print("=" * 60)

    db = SessionLocal()
    try:
        user = db.query(User).first()
        if not user:
            print("❌ No users found.")
            return

        start_date = date(2025, 11, 1)
        end_date = date(2025, 11, 30)

        # Get all expenses
        expenses = (
            db.query(Expense)
            .filter(
                Expense.user_id == user.id,
                Expense.date >= start_date,
                Expense.date <= end_date,
            )
            .all()
        )

        if not expenses:
            print("❌ No expenses found")
            return

        # Calculate stats
        total_expenses = sum(e.amount for e in expenses)
        revenue = compute_revenue_by_date_range(db, user.id, start_date, end_date, "paid")
        profit = revenue - total_expenses

        # Category breakdown
        by_category = {}
        for e in expenses:
            by_category[e.category] = by_category.get(e.category, Decimal("0")) + e.amount

        # Channel breakdown
        by_channel = {}
        for e in expenses:
            ch = e.channel or "unknown"
            by_channel[ch] = by_channel.get(ch, 0) + 1

        # Verification status
        verified = sum(1 for e in expenses if e.verified)
        unverified = len(expenses) - verified

        print("\n📈 November 2025 Expense Statistics")
        print("\n   Financial Overview:")
        print(f"   • Total Revenue:    ₦{revenue:,.2f}")
        print(f"   • Total Expenses:   ₦{total_expenses:,.2f}")
        print(f"   • Actual Profit:    ₦{profit:,.2f}")

        print("\n   Expense Details:")
        print(f"   • Number of Expenses: {len(expenses)}")
        print(f"   • Average Expense:    ₦{total_expenses / len(expenses):,.2f}")
        print(f"   • Verified:           {verified}")
        print(f"   • Unverified:         {unverified}")

        print("\n   Top 5 Categories:")
        for i, (cat, amt) in enumerate(sorted(by_category.items(), key=lambda x: x[1], reverse=True)[:5], 1):
            print(f"   {i}. {cat:20s} ₦{amt:,.2f}")

        print("\n   By Channel:")
        for channel, count in sorted(by_channel.items(), key=lambda x: x[1], reverse=True):
            print(f"   • {channel:15s} {count} expenses")

        print("\n✅ Statistics calculated successfully")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback

        traceback.print_exc()
    finally:
        db.close()

    print("\n" + "=" * 60)


def main():
    """Run all tests"""
    print("\n" + "🧪" * 30)
    print("EXPENSE TRACKING SYSTEM - COMPREHENSIVE TEST SUITE")
    print("🧪" * 30)

    try:
        # Test 1: NLP Service
        test_nlp_service()

        # Test 2: Manual Creation
        test_manual_expense_creation()

        # Test 3: Summaries
        test_expense_summaries()

        # Test 4: Profit Calculation
        test_profit_calculation()

        # Test 5: Comprehensive Stats
        test_expense_stats()

        print("\n" + "✅" * 30)
        print("ALL TESTS COMPLETED!")
        print("✅" * 30 + "\n")

    except Exception as e:
        print(f"\n❌ Test suite failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()
