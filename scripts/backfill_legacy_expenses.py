"""One-time, idempotent backfill: copy legacy `expenses` rows into the unified
`invoice` model (invoice_type='expense'), so expenses recorded via WhatsApp/OCR
before the unification show up on the dashboard alongside everything else.

Idempotent: each created expense-invoice is tagged in its notes with a
``[legacy-expense-id:<id>]`` marker, and rows already backfilled are skipped, so
re-running is safe.

This does NOT delete the legacy `expenses` rows — dropping the table is a
separate, later step once the backfill is verified in production.

Run: python -m scripts.backfill_legacy_expenses            (dry-run summary)
     python -m scripts.backfill_legacy_expenses --apply    (perform the backfill)
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.session import SessionLocal
from app.models import models
from app.services.expense_service import record_expense_invoice


def _marker(expense_id: int) -> str:
    return f"[legacy-expense-id:{expense_id}]"


def backfill(apply: bool = False) -> None:
    db = SessionLocal()
    created = skipped = errors = 0
    try:
        from app.models.expense import Expense

        legacy = db.query(Expense).order_by(Expense.id).all()
        print(f"Found {len(legacy)} legacy expense rows.")

        for exp in legacy:
            marker = _marker(exp.id)
            already = (
                db.query(models.Invoice.id)
                .filter(
                    models.Invoice.issuer_id == exp.user_id,
                    models.Invoice.invoice_type == "expense",
                    models.Invoice.notes.like(f"%{marker}%"),
                )
                .first()
            )
            if already:
                skipped += 1
                continue

            if not apply:
                created += 1  # would-create count for the dry run
                continue

            try:
                notes = " ".join(p for p in [exp.notes, marker] if p)
                record_expense_invoice(
                    db,
                    user_id=exp.user_id,
                    amount=exp.amount,
                    category=exp.category,
                    description=exp.description,
                    merchant=exp.merchant,
                    expense_date=exp.date,
                    input_method=exp.input_method or "manual",
                    channel=exp.channel or "whatsapp",
                    verified=bool(exp.verified),
                    receipt_url=exp.receipt_url,
                    receipt_text=exp.receipt_text,
                    notes=notes,
                )
                created += 1
            except Exception as e:  # noqa: BLE001
                db.rollback()
                errors += 1
                print(f"  ! expense #{exp.id} failed: {e}")

        verb = "Backfilled" if apply else "Would backfill"
        print(f"{verb}: {created} | skipped (already done): {skipped} | errors: {errors}")
        if not apply:
            print("Dry run — re-run with --apply to perform the backfill.")
    finally:
        db.close()


if __name__ == "__main__":
    backfill(apply="--apply" in sys.argv)
