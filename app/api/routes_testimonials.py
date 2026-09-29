"""Testimonial and Top Users endpoints.

Public (no auth):
- GET  /public/testimonials       — approved testimonials for landing page
- GET  /public/top-users          — showcase top active businesses
- POST /public/feedback           — submit feedback via email token link

Authenticated:
- POST /testimonials              — submit a testimonial
"""

import logging
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.api.dependencies import CurrentUserDep, DbDep
from app.api.rate_limit import limiter
from app.core.config import settings
from app.db.session import get_db
from app.models.models import Invoice, Testimonial, User

logger = logging.getLogger(__name__)

_FEEDBACK_TOKEN_ALG = "HS256"
_FEEDBACK_TOKEN_EXPIRY_DAYS = 14


def create_feedback_token(user_id: int) -> str:
    """Create a signed token for the feedback form link."""
    payload = {
        "sub": str(user_id),
        "type": "feedback",
        "exp": datetime.now(timezone.utc) + timedelta(days=_FEEDBACK_TOKEN_EXPIRY_DAYS),
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=_FEEDBACK_TOKEN_ALG)


def _decode_feedback_token(token: str) -> int:
    """Decode and validate a feedback token. Returns user_id."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=[_FEEDBACK_TOKEN_ALG])
        if payload.get("type") != "feedback":
            raise ValueError("Invalid token type")
        return int(payload["sub"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, ValueError, KeyError) as e:
        raise HTTPException(status_code=400, detail="Invalid or expired feedback link") from e


# ── Public router (no auth) ─────────────────────────────────────────

public_router = APIRouter(tags=["public"])


class TestimonialOut(BaseModel):
    id: int
    text: str
    rating: int
    user_name: str
    business_name: str | None
    logo_url: str | None
    created_at: datetime


class TopUserOut(BaseModel):
    business_name: str
    logo_url: str | None
    what_they_sell: str | None
    invoices_sent: int
    member_since: str


@public_router.get("/testimonials", response_model=list[TestimonialOut])
def get_public_testimonials(db: Session = Depends(get_db)):
    """Return approved testimonials for the landing page."""
    testimonials = (
        db.query(Testimonial)
        .filter(Testimonial.approved.is_(True))
        .order_by(Testimonial.featured.desc(), Testimonial.created_at.desc())
        .limit(12)
        .all()
    )
    return [
        TestimonialOut(
            id=t.id,
            text=t.text,
            rating=t.rating,
            user_name=t.user.name.split()[0] if t.user.name else "User",
            business_name=t.user.business_name,
            logo_url=t.user.logo_url,
            created_at=t.created_at,
        )
        for t in testimonials
    ]


@public_router.get("/top-users", response_model=list[TopUserOut])
def get_top_users(db: Session = Depends(get_db)):
    """Return top active businesses for the landing page showcase.

    Criteria: users with a business name, at least 5 invoices in the last
    90 days, and who have opted in to showcase (showcase_opted_in=True on
    their Testimonial, or have an approved testimonial).
    Shows anonymized data — only business name, logo, and category.
    """
    ninety_days_ago = datetime.now(tz=timezone.utc) - timedelta(days=90)

    # Subquery: users with approved testimonials (opted in to public visibility)
    opted_in_users = db.query(Testimonial.user_id).filter(Testimonial.approved.is_(True)).subquery()

    results = (
        db.query(
            User.business_name,
            User.logo_url,
            User.created_at,
            func.count(Invoice.id).label("invoice_count"),
        )
        .join(Invoice, Invoice.issuer_id == User.id)
        .filter(
            User.id.in_(db.query(opted_in_users.c.user_id)),
            User.business_name.isnot(None),
            Invoice.invoice_type == "revenue",
            Invoice.created_at >= ninety_days_ago,
        )
        .group_by(User.id, User.business_name, User.logo_url, User.created_at)
        .having(func.count(Invoice.id) >= 5)
        .order_by(func.count(Invoice.id).desc())
        .limit(8)
        .all()
    )

    # Get top product categories per user for "what they sell"
    top_users = []
    for row in results:
        # Derive what they sell from most common invoice line descriptions
        top_items = (
            db.query(Invoice)
            .join(User, Invoice.issuer_id == User.id)
            .filter(
                User.business_name == row.business_name,
                Invoice.invoice_type == "revenue",
            )
            .order_by(Invoice.created_at.desc())
            .limit(5)
            .all()
        )
        descriptions = []
        for inv in top_items:
            if inv.lines:
                descriptions.extend(line.description for line in inv.lines[:2])
        what_they_sell = ", ".join(list(dict.fromkeys(descriptions))[:3]) if descriptions else None

        top_users.append(
            TopUserOut(
                business_name=row.business_name,
                logo_url=row.logo_url,
                what_they_sell=what_they_sell,
                invoices_sent=row.invoice_count,
                member_since=row.created_at.strftime("%b %Y"),
            )
        )

    return top_users


class FeedbackSubmit(BaseModel):
    token: str
    text: str = Field(..., min_length=10, max_length=500)


@public_router.post("/feedback")
@limiter.limit("5/hour")
def submit_feedback_via_token(
    request: Request,
    body: FeedbackSubmit,
    db: Session = Depends(get_db),
):
    """Submit feedback via a signed email token (no login required)."""
    user_id = _decode_feedback_token(body.token)

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = db.query(Testimonial).filter(Testimonial.user_id == user_id).first()
    if existing:
        raise HTTPException(status_code=409, detail="You already submitted feedback. Thank you!")

    testimonial = Testimonial(
        user_id=user_id,
        text=body.text,
        rating=5,
    )
    db.add(testimonial)
    db.commit()

    logger.info("Feedback submitted via email link by user %d", user_id)
    return {"message": "Thank you! Your feedback will appear on our website after review."}


# ── Authenticated router ─────────────────────────────────────────────

auth_router = APIRouter(tags=["testimonials"])


class TestimonialCreate(BaseModel):
    text: str = Field(..., min_length=10, max_length=500)
    rating: int = Field(default=5, ge=1, le=5)


class TestimonialSubmitOut(BaseModel):
    id: int
    text: str
    rating: int
    message: str


@auth_router.post("/testimonials", response_model=TestimonialSubmitOut)
@limiter.limit("3/hour")
def submit_testimonial(
    request: Request,
    body: TestimonialCreate,
    current_user_id: CurrentUserDep,
    db: DbDep,
):
    """Submit a testimonial. Requires admin approval before appearing publicly."""
    # Check if user already has a pending/approved testimonial
    existing = db.query(Testimonial).filter(Testimonial.user_id == current_user_id).first()
    if existing:
        raise HTTPException(
            status_code=409,
            detail="You already submitted a testimonial. Contact support to update it.",
        )

    testimonial = Testimonial(
        user_id=current_user_id,
        text=body.text,
        rating=body.rating,
    )
    db.add(testimonial)
    db.commit()
    db.refresh(testimonial)

    logger.info("Testimonial %d submitted by user %d", testimonial.id, current_user_id)

    return TestimonialSubmitOut(
        id=testimonial.id,
        text=testimonial.text,
        rating=testimonial.rating,
        message="Thank you! Your testimonial will appear on our website after review.",
    )
