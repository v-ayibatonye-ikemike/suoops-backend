from __future__ import annotations

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings
from app.core.redis_utils import get_ssl_options, prepare_redis_url


def _create_celery() -> Celery:
    # For Celery, don't add SSL params to URL - use broker_use_ssl instead
    redis_url = prepare_redis_url(settings.REDIS_URL, add_ssl_params=False)
    # RedBeat needs the URL with SSL query params (it uses redis-py directly)
    redbeat_redis_url = prepare_redis_url(settings.REDIS_URL, add_ssl_params=True)
    ssl_options = get_ssl_options()
    celery = Celery(
        "whatsinvoice",
        broker=redis_url,
        backend=redis_url,
        include=["app.workers.tasks"],
    )
    celery.conf.update(
        task_default_queue="default",
        task_serializer="json",
        result_serializer="json",
        accept_content=["json"],
        timezone="UTC",
        enable_utc=True,
        task_always_eager=settings.ENV.lower() in {"test"},
        # Use RedBeat so the beat schedule persists in Redis across deploys
        beat_scheduler="redbeat.RedBeatScheduler",
        redbeat_redis_url=redbeat_redis_url,
        redbeat_key_prefix="suoops-beat",
        # Limit Redis connections to avoid "max number of clients reached"
        broker_pool_limit=3,
        redis_backend_transport_options={"max_connections": 5},
        broker_connection_retry_on_startup=True,
    )
    if ssl_options:
        celery.conf.update(
            broker_use_ssl=ssl_options,
            redis_backend_use_ssl=ssl_options,
        )
    # Beat schedule (only active outside test env)
    if settings.ENV.lower() not in {"test"}:
        celery.conf.beat_schedule = {
            "escrow-release-due-orders": {
                "task": "escrow.release_due_orders",
                "schedule": crontab(minute="*/15"),  # every 15 min — pay out elapsed holds
            },
            "escrow-cancel-stale-pending": {
                "task": "escrow.cancel_stale_pending",
                "schedule": crontab(minute=20, hour="*/6"),  # every 6h — clear abandoned unpaid orders
            },
            "monthly-tax-reports": {
                "task": "tax.generate_previous_month_reports",
                "schedule": crontab(minute=0, hour=2, day_of_month=1),  # 02:00 UTC first day
                "args": ["paid"],
            },
            "daily-overdue-reminders": {
                "task": "maintenance.send_overdue_reminders",
                "schedule": crontab(minute=0, hour=8),  # 08:00 UTC = 09:00 WAT
            },
            "daily-customer-payment-reminders": {
                "task": "reminders.send_customer_payment_reminders",
                "schedule": crontab(minute=30, hour=8),  # 08:30 UTC = 09:30 WAT
            },
            "daily-mark-paid-nudges": {
                "task": "reminders.send_mark_paid_nudges",
                "schedule": crontab(minute=0, hour=11),  # 11:00 UTC = 12:00 WAT
            },
            "daily-business-summary": {
                "task": "summary.send_daily_summaries",
                "schedule": crontab(minute=0, hour=18),  # 18:00 UTC = 19:00 WAT
            },
            "daily-engagement-emails": {
                "task": "engagement.send_lifecycle_emails",
                "schedule": crontab(minute=0, hour=9),  # 09:00 UTC = 10:00 WAT
            },
            # morning-insights DISABLED — cut marketing email volume
            # "morning-insights-mon-thu": {
            #     "task": "insights.send_morning_insights",
            #     "schedule": crontab(minute=0, hour=7, day_of_week="1,4"),
            # },
            "weekly-dormant-customer-nudges": {
                "task": "customer_engagement.send_dormant_customer_nudges",
                "schedule": crontab(minute=0, hour=10, day_of_week="2"),  # Tue 10:00 UTC
            },
            "weekly-feedback-collection": {
                "task": "feedback.collect_user_feedback",
                "schedule": crontab(minute=0, hour=12, day_of_week=3),  # Wed 12:00 UTC = 13:00 WAT
            },
            "daily-subscription-expiry": {
                "task": "maintenance.downgrade_expired_subscriptions",
                "schedule": crontab(minute=0, hour=1),  # 01:00 UTC = 02:00 WAT
            },
            "weekly-webhook-cleanup": {
                "task": "maintenance.cleanup_stale_webhooks",
                "schedule": crontab(minute=0, hour=3, day_of_week=0),  # Sun 03:00 UTC
            },
            "daily-social-promotion": {
                "task": "social_marketing.run_daily_promotion",
                "schedule": crontab(minute=0, hour=10),  # 10:00 UTC = 11:00 WAT
            },
            "weekly-log-cleanup": {
                "task": "maintenance.cleanup_old_logs",
                "schedule": crontab(minute=30, hour=3, day_of_week=0),  # Sun 03:30 UTC
            },
            "weekly-warn-inactive-accounts": {
                "task": "maintenance.warn_inactive_accounts",
                "schedule": crontab(minute=0, hour=4, day_of_week=1),  # Mon 04:00 UTC
            },
            "weekly-delete-inactive-accounts": {
                "task": "maintenance.delete_inactive_accounts",
                "schedule": crontab(minute=0, hour=4, day_of_week=2),  # Tue 04:00 UTC (1 day after warning scan)
            },
            "weekly-winback-churned-businesses": {
                "task": "maintenance.winback_churned_businesses",
                "schedule": crontab(minute=0, hour=10, day_of_week=3),  # Wed 10:00 UTC = 11:00 WAT
            },
            "biweekly-aggregate-unpaid-alerts": {
                "task": "growth.send_aggregate_unpaid_alerts",
                "schedule": crontab(minute=0, hour=8, day_of_week="1,4"),  # Mon+Thu 08:00 UTC = 09:00 WAT
            },
            "weekly-free-user-summary": {
                "task": "growth.send_weekly_free_summary",
                "schedule": crontab(minute=0, hour=8, day_of_week=1),  # Mon 08:00 UTC = 09:00 WAT
            },
            "daily-payment-upsells": {
                "task": "growth.send_payment_upsells",
                "schedule": crontab(minute=0, hour=14),  # 14:00 UTC = 15:00 WAT
            },
            "weekly-storefront-completion-nudges": {
                "task": "growth.send_storefront_completion_nudges",
                "schedule": crontab(minute=0, hour=10, day_of_week=5),  # Fri 10:00 UTC = 11:00 WAT
            },
            "weekly-expense-habit-reminders": {
                "task": "expense.send_reminders",
                "schedule": crontab(minute=0, hour=15, day_of_week=5),  # Fri 15:00 UTC = 16:00 WAT
            },
            "daily-activation-nudges": {
                "task": "maintenance.nudge_zero_invoice_users",
                "schedule": crontab(minute=0, hour=9),  # 09:00 UTC = 10:00 WAT
            },
            "afternoon-activation-nudges": {
                "task": "maintenance.nudge_zero_invoice_users",
                "schedule": crontab(minute=0, hour=16),  # 16:00 UTC = 17:00 WAT — catch morning signups
            },
        }
    return celery


celery_app = _create_celery()
