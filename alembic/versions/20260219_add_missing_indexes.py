"""add missing database indexes for query performance

Revision ID: 20260219_add_missing_indexes
Revises: 20260205_paystack_subscription
Create Date: 2026-02-19 00:00:00.000000

Adds indexes on frequently-filtered FK and status columns
that were identified as causing full table scans at scale.

Critical:
  - invoice.customer_id (FK, JOINs, GROUP BY, WhatsApp lookups)
  - customer.phone (WhatsApp bot resolves by phone on every message)
  - invoiceline.invoice_id (FK, JOINs, cascade DELETEs)
  - invoice.status (analytics, admin, overdue checks)

High:
  - invoice.due_date (overdue detection, aging reports)
  - user.created_at (admin dashboard date range filters)
  - user.plan (admin stats GROUP BY)
  - vat_returns.user_id (FK, VAT queries)
  - referral.status / type / created_at (admin dashboard filters)

Medium:
  - customer.email (invoice delivery lookups)
  - support_tickets.priority / assigned_to_id (admin dashboard)

Composite indexes for dominant query patterns:
  - invoice(issuer_id, invoice_type, created_at) — analytics #1 pattern
  - invoice(status, invoice_type, due_date) — overdue worker
  - support_tickets(status, priority) — admin "open urgent tickets"
"""

from alembic import op

revision = "20260219_add_missing_indexes"
down_revision = "20260205_paystack_subscription"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Invoice table ──────────────────────────────────────────────────
    op.create_index(
        "ix_invoice_customer_id",
        "invoice",
        ["customer_id"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_invoice_due_date",
        "invoice",
        ["due_date"],
        unique=False,
        if_not_exists=True,
    )
    # Composite: analytics dominant pattern (issuer + type + date range)
    op.create_index(
        "ix_invoice_issuer_type_created",
        "invoice",
        ["issuer_id", "invoice_type", "created_at"],
        unique=False,
        if_not_exists=True,
    )
    # Composite: overdue worker (status + type + due_date)
    op.create_index(
        "ix_invoice_status_type_duedate",
        "invoice",
        ["status", "invoice_type", "due_date"],
        unique=False,
        if_not_exists=True,
    )

    # ── InvoiceLine table ──────────────────────────────────────────────
    op.create_index(
        "ix_invoiceline_invoice_id",
        "invoiceline",
        ["invoice_id"],
        unique=False,
        if_not_exists=True,
    )

    # ── Customer table ─────────────────────────────────────────────────
    op.create_index(
        "ix_customer_phone",
        "customer",
        ["phone"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_customer_email",
        "customer",
        ["email"],
        unique=False,
        if_not_exists=True,
    )

    # ── User table ─────────────────────────────────────────────────────
    op.create_index(
        "ix_user_created_at",
        "user",
        ["created_at"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_user_plan",
        "user",
        ["plan"],
        unique=False,
        if_not_exists=True,
    )

    # ── VATReturn table ────────────────────────────────────────────────
    op.create_index(
        "ix_vat_returns_user_id",
        "vat_returns",
        ["user_id"],
        unique=False,
        if_not_exists=True,
    )

    # ── Referral table ─────────────────────────────────────────────────
    op.create_index(
        "ix_referral_status",
        "referral",
        ["status"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_referral_type",
        "referral",
        ["referral_type"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_referral_created_at",
        "referral",
        ["created_at"],
        unique=False,
        if_not_exists=True,
    )

    # ── SupportTicket table ────────────────────────────────────────────
    op.create_index(
        "ix_support_tickets_priority",
        "support_tickets",
        ["priority"],
        unique=False,
        if_not_exists=True,
    )
    op.create_index(
        "ix_support_tickets_assigned_to_id",
        "support_tickets",
        ["assigned_to_id"],
        unique=False,
        if_not_exists=True,
    )
    # Composite: admin "show me open urgent tickets"
    op.create_index(
        "ix_support_tickets_status_priority",
        "support_tickets",
        ["status", "priority"],
        unique=False,
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index("ix_support_tickets_status_priority", table_name="support_tickets", if_exists=True)
    op.drop_index("ix_support_tickets_assigned_to_id", table_name="support_tickets", if_exists=True)
    op.drop_index("ix_support_tickets_priority", table_name="support_tickets", if_exists=True)
    op.drop_index("ix_referral_created_at", table_name="referral", if_exists=True)
    op.drop_index("ix_referral_type", table_name="referral", if_exists=True)
    op.drop_index("ix_referral_status", table_name="referral", if_exists=True)
    op.drop_index("ix_vat_returns_user_id", table_name="vat_returns", if_exists=True)
    op.drop_index("ix_user_plan", table_name="user", if_exists=True)
    op.drop_index("ix_user_created_at", table_name="user", if_exists=True)
    op.drop_index("ix_customer_email", table_name="customer", if_exists=True)
    op.drop_index("ix_customer_phone", table_name="customer", if_exists=True)
    op.drop_index("ix_invoiceline_invoice_id", table_name="invoiceline", if_exists=True)
    op.drop_index("ix_invoice_status_type_duedate", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_issuer_type_created", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_due_date", table_name="invoice", if_exists=True)
    op.drop_index("ix_invoice_customer_id", table_name="invoice", if_exists=True)
