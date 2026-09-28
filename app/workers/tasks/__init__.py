"""
Celery Tasks Module.

This module provides a modular, SRP-compliant task organization.
All tasks are registered with the Celery app.

Sub-modules:
- pdf_tasks: Invoice and receipt PDF generation
- messaging_tasks: WhatsApp, OCR, and payment sync
- tax_tasks: Tax reports and fiscalization
- expense_tasks: Expense summaries and reminders
- engagement_tasks: Lifecycle email notifications
"""
from __future__ import annotations

from .engagement_tasks import (
    send_engagement_emails,
)
from .announcement_tasks import (
    send_feature_announcement,
)
from .morning_insights_tasks import (
    send_morning_insights,
)
from .customer_engagement_tasks import (
    send_dormant_customer_nudges,
    send_post_payment_referrals,
)
from .social_marketing_tasks import (
    run_daily_promotion,
)
from .feedback_tasks import (
    collect_user_feedback,
)
from .maintenance_tasks import (
    cleanup_stale_webhooks,
    delete_inactive_accounts,
    downgrade_expired_subscriptions,
    warn_inactive_accounts,
)
from .growth_tasks import (
    send_aggregate_unpaid_alerts,
    send_payment_upsells,
    send_weekly_free_summary,
)
from .welcome_tasks import (
    broadcast_welcome,
    send_activation_followup,
    send_instant_welcome,
)
from .expense_tasks import (
    send_expense_reminders,
    send_expense_summary,
)
from .escrow_tasks import (
    release_due_escrow_orders,
)
from .messaging_tasks import (
    ocr_parse_image,
    process_whatsapp_inbound,
    send_customer_payment_reminders,
    send_daily_summaries,
    send_overdue_reminders,
    sync_provider_status,
)

# Re-export all tasks for backward compatibility
from .pdf_tasks import (
    generate_invoice_pdf_async,
    generate_receipt_pdf_async,
)
from .tax_tasks import (
    generate_previous_month_reports,
    transmit_invoice,
)

__all__ = [
    # PDF tasks
    "generate_invoice_pdf_async",
    "generate_receipt_pdf_async",
    # Messaging tasks
    "process_whatsapp_inbound",
    "send_overdue_reminders",
    "send_customer_payment_reminders",
    "send_daily_summaries",
    "sync_provider_status",
    "ocr_parse_image",
    # Tax tasks
    "generate_previous_month_reports",
    "transmit_invoice",
    # Expense tasks
    "send_expense_summary",
    "send_expense_reminders",
    # Engagement tasks
    "send_engagement_emails",
    # One-time announcements
    "send_feature_announcement",
    # Morning insights
    "send_morning_insights",
    # Customer engagement
    "send_dormant_customer_nudges",
    "send_post_payment_referrals",
    # Social marketing
    "run_daily_promotion",
    # Instant welcome
    "broadcast_welcome",
    "send_instant_welcome",
    "send_activation_followup",
    # Maintenance
    "downgrade_expired_subscriptions",
    "cleanup_stale_webhooks",
    "warn_inactive_accounts",
    "delete_inactive_accounts",
    # Growth tasks
    "send_aggregate_unpaid_alerts",
    "send_weekly_free_summary",
    "send_payment_upsells",
]
