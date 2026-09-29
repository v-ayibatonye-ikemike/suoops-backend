"""Payment history endpoints."""

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.routes_auth import get_current_user_id
from app.db.session import get_db
from app.models.payment_models import PaymentStatus, PaymentTransaction

from .schemas import PaymentDetailOut, PaymentHistoryOut

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/history", response_model=PaymentHistoryOut)
def get_payment_history(
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
    limit: int = 50,
    offset: int = 0,
    status_filter: str | None = None,
):
    """
    Get payment history for the current user.

    **Parameters:**
    - limit: Max number of records to return (default: 50, max: 100)
    - offset: Number of records to skip for pagination (default: 0)
    - status_filter: Filter by payment status (pending, success, failed, cancelled, refunded)

    **Returns:**
    - payments: List of payment transactions
    - total: Total count of payments matching filter
    - summary: Aggregated stats (total_paid, successful_count, etc.)
    """
    # Validate limit
    limit = min(limit, 100)

    # Build query
    query = db.query(PaymentTransaction).filter(PaymentTransaction.user_id == current_user_id)

    # Apply status filter if provided
    if status_filter:
        try:
            status_enum = PaymentStatus(status_filter.lower())
            query = query.filter(PaymentTransaction.status == status_enum)
        except ValueError:
            raise HTTPException(
                status_code=400, detail="Invalid status filter. Choose: pending, success, failed, cancelled, refunded"
            )

    # Get total count
    total = query.count()

    # Get paginated results, ordered by created_at descending (newest first)
    payments = query.order_by(PaymentTransaction.created_at.desc()).offset(offset).limit(limit).all()

    # Calculate summary stats
    successful_payments = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.user_id == current_user_id, PaymentTransaction.status == PaymentStatus.SUCCESS)
        .all()
    )

    total_paid_kobo = sum(p.amount for p in successful_payments)

    summary = {
        "total_paid": total_paid_kobo / 100,  # Convert to Naira
        "successful_count": len(successful_payments),
        "pending_count": db.query(PaymentTransaction)
        .filter(PaymentTransaction.user_id == current_user_id, PaymentTransaction.status == PaymentStatus.PENDING)
        .count(),
        "failed_count": db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.user_id == current_user_id,
            PaymentTransaction.status.in_([PaymentStatus.FAILED, PaymentStatus.CANCELLED]),
        )
        .count(),
    }

    # Format payments for response
    payments_list = [
        {
            "id": p.id,
            "reference": p.reference,
            "amount": p.amount_naira,
            "currency": p.currency,
            "status": p.status.value,
            "plan_before": p.plan_before,
            "plan_after": p.plan_after,
            "payment_method": p.payment_method,
            "card_last4": p.card_last4,
            "card_brand": p.card_brand,
            "bank_name": p.bank_name,
            "created_at": p.created_at.isoformat(),
            "paid_at": p.paid_at.isoformat() if p.paid_at else None,
            "billing_start_date": p.billing_start_date.isoformat() if p.billing_start_date else None,
            "billing_end_date": p.billing_end_date.isoformat() if p.billing_end_date else None,
            "failure_reason": p.failure_reason,
        }
        for p in payments
    ]

    return {
        "payments": payments_list,
        "total": total,
        "limit": limit,
        "offset": offset,
        "summary": summary,
    }


@router.get("/history/{payment_id}", response_model=PaymentDetailOut)
def get_payment_detail(
    payment_id: int,
    current_user_id: Annotated[int, Depends(get_current_user_id)],
    db: Annotated[Session, Depends(get_db)],
):
    """
    Get detailed information about a specific payment transaction.

    Note: Paystack internal fields (transaction_id, metadata, ip_address)
    are excluded from the response to prevent data leakage.
    """
    payment = (
        db.query(PaymentTransaction)
        .filter(
            PaymentTransaction.id == payment_id,
            PaymentTransaction.user_id == current_user_id,  # Ensure user owns this payment
        )
        .one_or_none()
    )

    if not payment:
        raise HTTPException(status_code=404, detail="Payment not found")

    return {
        "id": payment.id,
        "reference": payment.reference,
        "amount": payment.amount_naira,
        "currency": payment.currency,
        "status": payment.status.value,
        "provider": payment.provider.value,
        "plan_before": payment.plan_before,
        "plan_after": payment.plan_after,
        "payment_method": payment.payment_method,
        "card_last4": payment.card_last4,
        "card_brand": payment.card_brand,
        "bank_name": payment.bank_name,
        "customer_email": payment.customer_email,
        "created_at": payment.created_at.isoformat(),
        "updated_at": payment.updated_at.isoformat(),
        "paid_at": payment.paid_at.isoformat() if payment.paid_at else None,
        "billing_start_date": payment.billing_start_date.isoformat() if payment.billing_start_date else None,
        "billing_end_date": payment.billing_end_date.isoformat() if payment.billing_end_date else None,
        "failure_reason": payment.failure_reason,
    }
