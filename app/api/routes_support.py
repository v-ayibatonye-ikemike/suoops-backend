"""Support routes for contact form, tickets, and admin dashboard."""

import logging
import smtplib
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, EmailStr
from sqlalchemy import desc, func
from sqlalchemy.orm import Session

from app.api.rate_limit import limiter
from app.api.routes_admin_auth import get_current_admin
from app.core.config import settings
from app.db.session import get_db
from app.models import models
from app.models.admin_models import AdminUser
from app.models.support_models import SupportTicket, TicketCategory, TicketPriority, TicketStatus

router = APIRouter(prefix="/support", tags=["support"])
logger = logging.getLogger(__name__)


# ============================================================================
# Schemas
# ============================================================================


class ContactRequest(BaseModel):
    """Contact form request schema."""

    name: str
    email: EmailStr
    category: str
    subject: str
    message: str


class ContactResponse(BaseModel):
    """Contact form response schema."""

    success: bool
    message: str
    ticket_id: int | None = None


class TicketOut(BaseModel):
    """Ticket response schema."""

    id: int
    name: str
    email: str
    subject: str
    message: str
    category: str
    status: str
    priority: str
    internal_notes: str | None
    response: str | None
    responded_at: datetime | None
    created_at: datetime
    updated_at: datetime
    resolved_at: datetime | None
    assigned_to_name: str | None = None
    responded_by_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class TicketUpdate(BaseModel):
    """Schema for updating a ticket."""

    status: str | None = None
    priority: str | None = None
    internal_notes: str | None = None
    response: str | None = None
    assigned_to_id: int | None = None


class TicketStats(BaseModel):
    """Support ticket statistics."""

    total_tickets: int
    open_tickets: int
    in_progress_tickets: int
    resolved_tickets: int
    tickets_today: int
    tickets_this_week: int
    avg_response_time_hours: float | None


class AdminDashboardStats(BaseModel):
    """Combined stats for admin dashboard."""

    users: dict[str, Any]
    tickets: TicketStats
    invoices: dict[str, Any]
    revenue: dict[str, Any]


# ============================================================================
# Email Helpers
# ============================================================================


def _get_smtp_config() -> dict | None:
    """Get SMTP configuration from settings."""
    if settings.SMTP_HOST and settings.SMTP_USER and settings.SMTP_PASSWORD:
        return {
            "host": settings.SMTP_HOST,
            "port": int(settings.SMTP_PORT or 587),
            "user": settings.SMTP_USER,
            "password": settings.SMTP_PASSWORD,
        }
    return None


def _send_contact_email(contact: ContactRequest, ticket_id: int) -> bool:
    """Send contact form email to support."""
    try:
        smtp_config = _get_smtp_config()
        if not smtp_config:
            logger.warning("No email provider configured for support contact")
            return False

        smtp_host = smtp_config["host"]
        smtp_port = smtp_config["port"]
        smtp_user = smtp_config["user"]
        smtp_password = smtp_config["password"]
        from_email = getattr(settings, "FROM_EMAIL", smtp_user)
        support_email = getattr(settings, "SUPPORT_EMAIL", "support@suoops.com")

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = support_email
        msg["Reply-To"] = contact.email
        msg["Subject"] = f"[Ticket #{ticket_id}] [{contact.category.upper()}] {contact.subject}"

        body = f"""
New Support Request - Ticket #{ticket_id}
==========================================

From: {contact.name}
Email: {contact.email}
Category: {contact.category}
Subject: {contact.subject}
Time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}

Message:
{'-' * 40}
{contact.message}
{'-' * 40}

View in Admin Dashboard:
https://support.suoops.com/admin/tickets/{ticket_id}

---
This email was sent from the SuoOps Help Center contact form.
"""
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        logger.info("Support contact email sent for ticket #%d", ticket_id)
        return True
    except Exception as e:
        logger.error("Failed to send support contact email: %s", e)
        return False


def _send_confirmation_email(contact: ContactRequest, ticket_id: int, smtp_config: dict, from_email: str) -> None:
    """Send confirmation email to the user."""
    try:
        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = contact.email
        msg["Subject"] = f"[Ticket #{ticket_id}] We received your message"

        body = f"""
Hello {contact.name},

Thank you for contacting SuoOps Support. We have received your message and will respond within 24 hours.

Your Ticket ID: #{ticket_id}

Request Summary:
- Category: {contact.category}
- Subject: {contact.subject}

Your Message:
{contact.message}

In the meantime, you may find answers to common questions in our Help Center:
https://support.suoops.com/faq

Thank you for using SuoOps!

Best regards,
The SuoOps Support Team

---
This is an automated confirmation. Please do not reply to this email.
For additional help, visit: https://support.suoops.com
"""
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as server:
            server.starttls()
            server.login(smtp_config["user"], smtp_config["password"])
            server.send_message(msg)

        logger.info("Confirmation email sent to %s for ticket #%d", contact.email, ticket_id)
    except Exception as e:
        logger.warning("Failed to send confirmation email to %s: %s", contact.email, e)


def _send_ticket_response_email(ticket: SupportTicket, response: str, responder_name: str) -> bool:
    """Send response email to the customer."""
    try:
        smtp_config = _get_smtp_config()
        if not smtp_config:
            return False

        from_email = getattr(settings, "FROM_EMAIL", smtp_config["user"])

        msg = MIMEMultipart()
        msg["From"] = from_email
        msg["To"] = ticket.email
        msg["Subject"] = f"Re: [Ticket #{ticket.id}] {ticket.subject}"

        body = f"""
Hello {ticket.name},

Thank you for contacting SuoOps Support. Here is our response to your inquiry:

{'-' * 40}
{response}
{'-' * 40}

Original Message:
{ticket.message}

If you have any further questions, please reply to this email or submit a new request at:
https://support.suoops.com/contact

Thank you for using SuoOps!

Best regards,
{responder_name}
SuoOps Support Team
"""
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as server:
            server.starttls()
            server.login(smtp_config["user"], smtp_config["password"])
            server.send_message(msg)

        logger.info("Response email sent to %s for ticket #%d", ticket.email, ticket.id)
        return True
    except Exception as e:
        logger.error("Failed to send response email: %s", e)
        return False


# ============================================================================
# Public Endpoints
# ============================================================================


@router.post("/contact", response_model=ContactResponse)
@limiter.limit("5/minute")
def submit_contact_form(
    request: Request,
    contact: ContactRequest,
    db: Session = Depends(get_db),
) -> ContactResponse:
    """
    Submit a contact form from the support portal.
    Creates a ticket in the database and sends email notifications.
    """
    # Basic validation
    if not contact.name.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Name is required")

    if not contact.message.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Message is required")

    if len(contact.message) > 10000:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Message is too long (max 10,000 characters)"
        )

    # Map category string to enum
    try:
        category = TicketCategory(contact.category.lower())
    except ValueError:
        category = TicketCategory.OTHER

    # Create ticket in database
    ticket = SupportTicket(
        name=contact.name.strip(),
        email=contact.email.lower(),
        subject=contact.subject.strip(),
        message=contact.message.strip(),
        category=category,
        status=TicketStatus.OPEN,
        priority=TicketPriority.MEDIUM,
    )
    db.add(ticket)
    db.commit()
    db.refresh(ticket)

    # Send emails (don't fail if email fails)
    try:
        _send_contact_email(contact, ticket.id)

        smtp_config = _get_smtp_config()
        if smtp_config:
            from_email = getattr(settings, "FROM_EMAIL", smtp_config["user"])
            _send_confirmation_email(contact, ticket.id, smtp_config, from_email)
    except Exception as e:
        logger.error("Failed to send contact/confirmation emails: %s", e)
        # Continue anyway - ticket is already created

    return ContactResponse(
        success=True, message="Your message has been sent. We'll respond within 24 hours.", ticket_id=ticket.id
    )


# ============================================================================
# Admin Endpoints
# ============================================================================


@router.get("/admin/stats", response_model=TicketStats)
def get_ticket_stats(
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
) -> TicketStats:
    """Get support ticket statistics."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())

    total = db.query(SupportTicket).count()
    open_tickets = db.query(SupportTicket).filter(SupportTicket.status == TicketStatus.OPEN).count()
    in_progress = db.query(SupportTicket).filter(SupportTicket.status == TicketStatus.IN_PROGRESS).count()
    resolved = (
        db.query(SupportTicket).filter(SupportTicket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED])).count()
    )
    tickets_today = db.query(SupportTicket).filter(SupportTicket.created_at >= today_start).count()
    tickets_week = db.query(SupportTicket).filter(SupportTicket.created_at >= week_start).count()

    # Average response time for resolved tickets
    avg_response = None
    responded_tickets = db.query(SupportTicket).filter(SupportTicket.responded_at.isnot(None)).all()
    if responded_tickets:
        total_hours = sum((t.responded_at - t.created_at).total_seconds() / 3600 for t in responded_tickets)
        avg_response = round(total_hours / len(responded_tickets), 1)

    return TicketStats(
        total_tickets=total,
        open_tickets=open_tickets,
        in_progress_tickets=in_progress,
        resolved_tickets=resolved,
        tickets_today=tickets_today,
        tickets_this_week=tickets_week,
        avg_response_time_hours=avg_response,
    )


@router.get("/admin/tickets", response_model=list[TicketOut])
def list_tickets(
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=100),
    ticket_status: str | None = Query(None, alias="status", description="Filter by status"),
    category: str | None = Query(None, description="Filter by category"),
    search: str | None = Query(None, description="Search in email or subject"),
) -> list[TicketOut]:
    """List support tickets with filtering and pagination."""
    query = db.query(SupportTicket)

    if ticket_status:
        try:
            status_enum = TicketStatus(ticket_status.lower())
            query = query.filter(SupportTicket.status == status_enum)
        except ValueError:
            pass

    if category:
        try:
            category_enum = TicketCategory(category.lower())
            query = query.filter(SupportTicket.category == category_enum)
        except ValueError:
            pass

    if search:
        search_term = f"%{search}%"
        query = query.filter(
            (SupportTicket.email.ilike(search_term))
            | (SupportTicket.subject.ilike(search_term))
            | (SupportTicket.name.ilike(search_term))
        )

    tickets = query.order_by(desc(SupportTicket.created_at)).offset(skip).limit(limit).all()

    result = []
    for ticket in tickets:
        # Note: assigned_to_id and responded_by_id are just IDs, not relationships
        # Could look up admin names if needed, but keeping it simple for now
        result.append(
            TicketOut(
                id=ticket.id,
                name=ticket.name,
                email=ticket.email,
                subject=ticket.subject,
                message=ticket.message,
                category=ticket.category.value,
                status=ticket.status.value,
                priority=ticket.priority.value,
                internal_notes=ticket.internal_notes,
                response=ticket.response,
                responded_at=ticket.responded_at,
                created_at=ticket.created_at,
                updated_at=ticket.updated_at,
                resolved_at=ticket.resolved_at,
                assigned_to_name=None,
                responded_by_name=None,
            )
        )

    return result


@router.get("/admin/tickets/{ticket_id}", response_model=TicketOut)
def get_ticket(
    ticket_id: int,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
) -> TicketOut:
    """Get a specific ticket by ID."""
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    return TicketOut(
        id=ticket.id,
        name=ticket.name,
        email=ticket.email,
        subject=ticket.subject,
        message=ticket.message,
        category=ticket.category.value,
        status=ticket.status.value,
        priority=ticket.priority.value,
        internal_notes=ticket.internal_notes,
        response=ticket.response,
        responded_at=ticket.responded_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        resolved_at=ticket.resolved_at,
        assigned_to_name=None,
        responded_by_name=None,
    )


@router.patch("/admin/tickets/{ticket_id}", response_model=TicketOut)
def update_ticket(
    ticket_id: int,
    update: TicketUpdate,
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
) -> TicketOut:
    """Update a support ticket (status, priority, notes, response)."""
    ticket = db.query(SupportTicket).filter(SupportTicket.id == ticket_id).first()
    if not ticket:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ticket not found")

    # Update status
    if update.status:
        try:
            new_status = TicketStatus(update.status.lower())
            ticket.status = new_status
            if new_status in [TicketStatus.RESOLVED, TicketStatus.CLOSED]:
                ticket.resolved_at = datetime.now(timezone.utc)
        except ValueError:
            pass

    # Update priority
    if update.priority:
        try:
            ticket.priority = TicketPriority(update.priority.lower())
        except ValueError:
            pass

    # Update internal notes
    if update.internal_notes is not None:
        ticket.internal_notes = update.internal_notes

    # Update assignment
    if update.assigned_to_id is not None:
        ticket.assigned_to_id = update.assigned_to_id if update.assigned_to_id > 0 else None

    # Add response
    if update.response:
        ticket.response = update.response
        ticket.responded_at = datetime.now(timezone.utc)
        ticket.responded_by_id = admin_user.id
        ticket.status = TicketStatus.RESOLVED

        # Send response email to customer
        _send_ticket_response_email(ticket, update.response, admin_user.name)

    db.commit()
    db.refresh(ticket)

    return TicketOut(
        id=ticket.id,
        name=ticket.name,
        email=ticket.email,
        subject=ticket.subject,
        message=ticket.message,
        category=ticket.category.value,
        status=ticket.status.value,
        priority=ticket.priority.value,
        internal_notes=ticket.internal_notes,
        response=ticket.response,
        responded_at=ticket.responded_at,
        created_at=ticket.created_at,
        updated_at=ticket.updated_at,
        resolved_at=ticket.resolved_at,
        assigned_to_name=None,
        responded_by_name=None,
    )


@router.get("/admin/dashboard", response_model=AdminDashboardStats)
def get_dashboard_stats(
    db: Session = Depends(get_db),
    admin_user: AdminUser = Depends(get_current_admin),
) -> AdminDashboardStats:
    """Get comprehensive dashboard statistics for admin panel."""
    now = datetime.now(timezone.utc)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=today_start.weekday())
    month_start = today_start.replace(day=1)
    thirty_days_ago = now - timedelta(days=30)

    # User stats
    total_users = db.query(models.User).count()
    verified_users = db.query(models.User).filter(models.User.phone_verified.is_(True)).count()
    users_today = db.query(models.User).filter(models.User.created_at >= today_start).count()
    users_this_week = db.query(models.User).filter(models.User.created_at >= week_start).count()
    active_users = db.query(models.User).filter(models.User.last_login >= thirty_days_ago).count()

    # Adoption of the commission-model features (replaces the retired plan tiers).
    online_payments_enabled = db.query(models.User).filter(models.User.paystack_subaccount_active.is_(True)).count()
    storefronts_enabled = db.query(models.User).filter(models.User.storefront_enabled.is_(True)).count()
    # "Live" = actually discoverable in the public marketplace/global search
    # (same trust gate: active status + logo + online payments + active product).
    from app.api.routes_storefront import count_live_storefronts

    storefronts_live = count_live_storefronts(db)

    # Ticket stats
    total_tickets = db.query(SupportTicket).count()
    open_tickets = db.query(SupportTicket).filter(SupportTicket.status == TicketStatus.OPEN).count()
    in_progress_tickets = db.query(SupportTicket).filter(SupportTicket.status == TicketStatus.IN_PROGRESS).count()
    resolved_tickets = (
        db.query(SupportTicket).filter(SupportTicket.status.in_([TicketStatus.RESOLVED, TicketStatus.CLOSED])).count()
    )
    tickets_today = db.query(SupportTicket).filter(SupportTicket.created_at >= today_start).count()

    # Invoice stats. "This month" counts REVENUE invoices only (matches the
    # /admin/metrics/summary definition) so the Dashboard and Metrics pages agree
    # — expenses are business bookkeeping, not platform invoice activity.
    total_invoices = db.query(models.Invoice).count()
    invoices_this_month = (
        db.query(models.Invoice)
        .filter(
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= month_start,
        )
        .count()
    )
    paid_invoices = db.query(models.Invoice).filter(models.Invoice.status == "paid").count()

    # Revenue stats. Kept CONSISTENT with the main /admin/metrics dashboard:
    # revenue invoices only (not expenses), excluding internal/test accounts, and
    # capping implausible single invoices — otherwise this figure disagrees with
    # the platform GMV shown elsewhere.
    from sqlalchemy import or_ as _or

    from app.core.config import settings as _settings

    _excluded_ids: list[int] = []
    _raw = _settings.METRICS_EXCLUDED_EMAILS
    if _raw:
        _emails = {e.strip().lower() for e in _raw.split(",") if e.strip()}
        if _emails:
            _excluded_ids = [
                uid for (uid,) in db.query(models.User.id).filter(func.lower(models.User.email).in_(_emails)).all()
            ]
    # Also drop flagged (suspected junk/fraud) accounts so they can't inflate GMV.
    _flagged = {uid for (uid,) in db.query(models.User.id).filter(models.User.flagged_for_review.is_(True)).all()}
    if _flagged:
        _excluded_ids = list(set(_excluded_ids) | _flagged)
    _ceiling = _settings.METRICS_MAX_INVOICE_NAIRA or 0

    def _guard(q):
        if _excluded_ids:
            q = q.filter(models.Invoice.issuer_id.notin_(_excluded_ids))
        if _ceiling and _ceiling > 0:
            q = q.filter(models.Invoice.amount <= _ceiling)
        return q

    # GMV = gross value of REVENUE paid this month (volume flowing through Suoops).
    monthly_gmv = (
        _guard(
            db.query(func.sum(models.Invoice.amount)).filter(
                models.Invoice.invoice_type == "revenue",
                models.Invoice.status == "paid",
                models.Invoice.paid_at >= month_start,
            )
        ).scalar()
        or 0
    )

    # Commission = Suoops' actual earnings (manual invoices 1%, storefront 3%),
    # read from each invoice's stored fee ledger:
    #  - Wallet: charged at CREATION on every non-storefront revenue invoice.
    #  - Online: storefront orders charged by Paystack when PAID (count by paid_at).
    from app.utils.feature_gate import platform_fee_kobo

    # Sum the per-invoice fee ledger; recompute only for legacy (NULL) rows.
    wallet_rows = _guard(
        db.query(models.Invoice.platform_fee_kobo, models.Invoice.amount).filter(
            models.Invoice.invoice_type == "revenue",
            models.Invoice.created_at >= month_start,
            _or(models.Invoice.channel != "storefront", models.Invoice.channel.is_(None)),
        )
    ).all()
    online_rows = _guard(
        db.query(models.Invoice.platform_fee_kobo, models.Invoice.amount).filter(
            models.Invoice.invoice_type == "revenue",
            models.Invoice.channel == "storefront",
            models.Invoice.status == "paid",
            models.Invoice.paid_at >= month_start,
        )
    ).all()
    commission_kobo = sum(
        f if f is not None else platform_fee_kobo(a, channel="manual") for (f, a) in wallet_rows
    ) + sum(f if f is not None else platform_fee_kobo(a) for (f, a) in online_rows)

    return AdminDashboardStats(
        users={
            "total": total_users,
            "verified": verified_users,
            "registered_today": users_today,
            "registered_this_week": users_this_week,
            "active_last_30_days": active_users,
            "online_payments_enabled": online_payments_enabled,
            "storefronts_enabled": storefronts_enabled,
            "storefronts_live": storefronts_live,
        },
        tickets=TicketStats(
            total_tickets=total_tickets,
            open_tickets=open_tickets,
            in_progress_tickets=in_progress_tickets,
            resolved_tickets=resolved_tickets,
            tickets_today=tickets_today,
            tickets_this_week=db.query(SupportTicket).filter(SupportTicket.created_at >= week_start).count(),
            avg_response_time_hours=None,
        ),
        invoices={
            "total": total_invoices,
            "this_month": invoices_this_month,
            "paid": paid_invoices,
        },
        revenue={
            "this_month": float(commission_kobo) / 100,
            "gmv_this_month": float(monthly_gmv),
        },
    )
