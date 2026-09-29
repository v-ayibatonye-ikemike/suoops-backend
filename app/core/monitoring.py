import logging

from app.core.config import settings

_initialized = False


def init_monitoring() -> None:
    global _initialized
    if _initialized:
        return
    dsn = getattr(settings, "SENTRY_DSN", None)
    if dsn:
        try:
            import sentry_sdk
            from sentry_sdk.integrations.fastapi import FastApiIntegration
            from sentry_sdk.integrations.redis import RedisIntegration
            from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
            from sentry_sdk.integrations.starlette import StarletteIntegration

            # IMPORTANT: this is the ONLY place Sentry is initialized. Calling
            # sentry_sdk.init() twice re-applies integrations and double-wraps
            # RedisIntegration's command hook, causing a RecursionError
            # ("maximum recursion depth exceeded") on every Redis command in
            # the web process. Keep it single.
            sentry_sdk.init(
                dsn=dsn,
                integrations=[
                    FastApiIntegration(),
                    StarletteIntegration(),
                    SqlalchemyIntegration(),
                    RedisIntegration(),
                ],
                traces_sample_rate=0.02 if settings.ENV.lower().startswith("prod") else 1.0,
                profiles_sample_rate=0.0,  # Disabled to minimize Sentry costs
                environment=settings.ENV,
                release=f"suoops-backend@{settings.ENV}",
                # Disable PII to comply with NDPA — user context set explicitly via sentry_sdk.set_user
                send_default_pii=False,
            )
            logging.getLogger(__name__).info("Sentry initialized")
        except Exception as exc:  # noqa: BLE001
            logging.getLogger(__name__).warning("Failed to init Sentry: %s", exc)
    _initialized = True
