"""Metrics facade.

Uses Prometheus client library if available; otherwise falls back to logging-only no-ops.
Service code should ONLY call the semantic helpers here so we can change backend freely.
"""

from __future__ import annotations

import logging
import time

"""Extended metrics.

Adds new domain counters for authentication & tax flows. All helper functions
gracefully no-op when Prometheus client library is unavailable.

New metrics:
- oauth_logins_total            Successful OAuth login callbacks
- tax_profile_updates_total     Tax profile update operations
- vat_calculations_total        VAT summary or calculation requests
"""

logger = logging.getLogger("metrics")

try:  # pragma: no cover - import guard
    from prometheus_client import Counter, Histogram

    _INVOICE_CREATED = Counter("invoice_created_total", "Invoices successfully created")
    _INVOICE_PAID = Counter("invoice_paid_total", "Invoices marked paid")
    _WHATSAPP_PARSE_UNKNOWN = Counter("whatsapp_parse_unknown_total", "Inbound WhatsApp messages with unknown intent")
    _PAYMENT_CONFIRM_LATENCY = Histogram(
        "payment_confirmation_latency_seconds", "Latency from creation to payment confirmation"
    )
    _OAUTH_LOGINS = Counter("oauth_logins_total", "Successful OAuth login callbacks")
    _TAX_PROFILE_UPDATES = Counter("tax_profile_updates_total", "Tax profile updates")
    _VAT_CALCULATIONS = Counter("vat_calculations_total", "VAT calculations or summaries served")
    _COMPLIANCE_CHECKS = Counter("compliance_checks_total", "Tax compliance summary checks served")
    _OTP_SIGNUP_REQUESTS = Counter("otp_signup_requests_total", "OTP signup requests initiated")
    _OTP_SIGNUP_VERIFICATIONS = Counter("otp_signup_verifications_total", "Successful OTP signup verifications")
    _OTP_LOGIN_REQUESTS = Counter("otp_login_requests_total", "OTP login requests initiated")
    _OTP_LOGIN_VERIFICATIONS = Counter("otp_login_verifications_total", "Successful OTP login verifications")
    _OTP_RESENDS = Counter("otp_resends_total", "OTP resend attempts")
    _OTP_RESEND_BLOCKED = Counter("otp_resends_blocked_total", "Resend attempts blocked by cooldown")
    _OTP_INVALID_ATTEMPTS = Counter("otp_invalid_attempts_total", "Invalid or expired OTP attempts")
    _OTP_SIGNUP_VERIFY_LATENCY = Histogram(
        "otp_signup_verify_latency_seconds",
        "Latency between signup request and successful verification",
        buckets=(1, 3, 5, 10, 15, 30, 45, 60, 90, 120, 180, 300, 600),
    )
    _OTP_LOGIN_VERIFY_LATENCY = Histogram(
        "otp_login_verify_latency_seconds",
        "Latency between login request and successful verification",
        buckets=(1, 3, 5, 10, 15, 30, 45, 60, 90, 120, 180, 300, 600),
    )
    _OTP_WHATSAPP_DELIVERY_SUCCESS = Counter(
        "otp_whatsapp_delivery_success_total", "Successful WhatsApp OTP message deliveries"
    )
    _OTP_WHATSAPP_DELIVERY_FAILURE = Counter(
        "otp_whatsapp_delivery_failure_total", "Failed WhatsApp OTP message deliveries"
    )
    _OTP_EMAIL_DELIVERY_SUCCESS = Counter("otp_email_delivery_success_total", "Successful Email OTP deliveries")
    _OTP_EMAIL_DELIVERY_FAILURE = Counter("otp_email_delivery_failure_total", "Failed Email OTP deliveries")
    _OTP_RESEND_SUCCESS_CONVERSION = Counter(
        "otp_resend_success_conversion_total",
        "Successful OTP verifications that followed at least one resend",
    )
    # Subscription metrics
    _SUBSCRIPTION_PAYMENT_INITIATED = Counter(
        "subscription_payment_initiated_total", "Subscription payment initiations", ["plan"]
    )
    _SUBSCRIPTION_PAYMENT_SUCCESS = Counter(
        "subscription_payment_success_total", "Successful subscription payments", ["plan"]
    )
    _SUBSCRIPTION_PAYMENT_FAILED = Counter(
        "subscription_payment_failed_total", "Failed subscription payments", ["plan", "reason"]
    )
    _SUBSCRIPTION_UPGRADES = Counter(
        "subscription_upgrades_total", "Subscription plan upgrades", ["from_plan", "to_plan"]
    )
    _INVOICE_CREATED_BY_PLAN = Counter(
        "invoice_created_by_plan_total", "Invoices created by subscription plan", ["plan"]
    )
    _AVERAGE_INVOICE_VALUE = Histogram(
        "invoice_amount_naira",
        "Invoice amounts in Naira",
        buckets=(100, 500, 1000, 2500, 5000, 10000, 25000, 50000, 100000, 250000, 500000, 1000000),
    )
    _ENABLED = True
except Exception:  # noqa: BLE001
    _ENABLED = False
    (
        _INVOICE_CREATED,
        _INVOICE_PAID,
        _WHATSAPP_PARSE_UNKNOWN,
        _PAYMENT_CONFIRM_LATENCY,
        _OAUTH_LOGINS,
        _TAX_PROFILE_UPDATES,
        _VAT_CALCULATIONS,
        _COMPLIANCE_CHECKS,
        _OTP_SIGNUP_REQUESTS,
        _OTP_SIGNUP_VERIFICATIONS,
        _OTP_LOGIN_REQUESTS,
        _OTP_LOGIN_VERIFICATIONS,
        _OTP_RESENDS,
        _OTP_RESEND_BLOCKED,
        _OTP_INVALID_ATTEMPTS,
        _OTP_SIGNUP_VERIFY_LATENCY,
        _OTP_LOGIN_VERIFY_LATENCY,
        _OTP_WHATSAPP_DELIVERY_SUCCESS,
        _OTP_WHATSAPP_DELIVERY_FAILURE,
        _OTP_EMAIL_DELIVERY_SUCCESS,
        _OTP_EMAIL_DELIVERY_FAILURE,
        _OTP_RESEND_SUCCESS_CONVERSION,
        _SUBSCRIPTION_PAYMENT_INITIATED,
        _SUBSCRIPTION_PAYMENT_SUCCESS,
        _SUBSCRIPTION_PAYMENT_FAILED,
        _SUBSCRIPTION_UPGRADES,
        _INVOICE_CREATED_BY_PLAN,
        _AVERAGE_INVOICE_VALUE,
    ) = (None,) * 28  # type: ignore
    logger.warning("Prometheus client not available; metrics will be log-only")


def invoice_created():
    if _ENABLED:
        _INVOICE_CREATED.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric invoice_created_total += 1")


def invoice_paid(latency_seconds: float | None = None):
    if _ENABLED:
        _INVOICE_PAID.inc()  # type: ignore[union-attr]
        if latency_seconds is not None:
            _PAYMENT_CONFIRM_LATENCY.observe(latency_seconds)  # type: ignore[union-attr]
    else:
        logger.debug("metric invoice_paid_total += 1")
        if latency_seconds is not None:
            logger.debug("observe payment_confirmation_latency_seconds=%s", latency_seconds)


def parse_unknown():
    if _ENABLED:
        _WHATSAPP_PARSE_UNKNOWN.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric whatsapp_parse_unknown_total += 1")


def oauth_login_success():
    if _ENABLED:
        _OAUTH_LOGINS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric oauth_logins_total += 1")


def tax_profile_updated():
    if _ENABLED:
        _TAX_PROFILE_UPDATES.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric tax_profile_updates_total += 1")


def vat_calculation_record():
    if _ENABLED:
        _VAT_CALCULATIONS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric vat_calculations_total += 1")


def compliance_check_record():
    if _ENABLED:
        _COMPLIANCE_CHECKS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric compliance_checks_total += 1")


# ---------------- OTP metrics helpers -----------------
def otp_signup_requested():
    if _ENABLED:
        _OTP_SIGNUP_REQUESTS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_signup_requests_total += 1")


def otp_signup_verified():
    if _ENABLED:
        _OTP_SIGNUP_VERIFICATIONS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_signup_verifications_total += 1")


def otp_login_requested():
    if _ENABLED:
        _OTP_LOGIN_REQUESTS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_login_requests_total += 1")


def otp_login_verified():
    if _ENABLED:
        _OTP_LOGIN_VERIFICATIONS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_login_verifications_total += 1")


def otp_resend_attempt():
    if _ENABLED:
        _OTP_RESENDS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_resends_total += 1")


def otp_resend_blocked():
    if _ENABLED:
        _OTP_RESEND_BLOCKED.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_resends_blocked_total += 1")


def otp_invalid_attempt():
    if _ENABLED:
        _OTP_INVALID_ATTEMPTS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_invalid_attempts_total += 1")


def otp_signup_latency_observe(seconds: float):
    if _ENABLED:
        _OTP_SIGNUP_VERIFY_LATENCY.observe(seconds)  # type: ignore[union-attr]
    else:
        logger.debug("observe otp_signup_verify_latency_seconds=%s", seconds)


def otp_login_latency_observe(seconds: float):
    if _ENABLED:
        _OTP_LOGIN_VERIFY_LATENCY.observe(seconds)  # type: ignore[union-attr]
    else:
        logger.debug("observe otp_login_verify_latency_seconds=%s", seconds)


def otp_whatsapp_delivery_success():
    if _ENABLED:
        _OTP_WHATSAPP_DELIVERY_SUCCESS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_whatsapp_delivery_success_total += 1")


def otp_whatsapp_delivery_failure():
    if _ENABLED:
        _OTP_WHATSAPP_DELIVERY_FAILURE.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_whatsapp_delivery_failure_total += 1")


def otp_email_delivery_success():
    if _ENABLED:
        _OTP_EMAIL_DELIVERY_SUCCESS.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_email_delivery_success_total += 1")


def otp_email_delivery_failure():
    if _ENABLED:
        _OTP_EMAIL_DELIVERY_FAILURE.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_email_delivery_failure_total += 1")


def otp_resend_success_conversion():
    if _ENABLED:
        _OTP_RESEND_SUCCESS_CONVERSION.inc()  # type: ignore[union-attr]
    else:
        logger.debug("metric otp_resend_success_conversion_total += 1")


# ---------------- Subscription metrics helpers -----------------
def subscription_payment_initiated(plan: str):
    """Record subscription payment initiation."""
    if _ENABLED:
        _SUBSCRIPTION_PAYMENT_INITIATED.labels(plan=plan).inc()  # type: ignore[union-attr]
    else:
        logger.debug(f"metric subscription_payment_initiated_total[plan={plan}] += 1")


def subscription_payment_success(plan: str):
    """Record successful subscription payment."""
    if _ENABLED:
        _SUBSCRIPTION_PAYMENT_SUCCESS.labels(plan=plan).inc()  # type: ignore[union-attr]
    else:
        logger.debug(f"metric subscription_payment_success_total[plan={plan}] += 1")


def subscription_payment_failed(plan: str, reason: str = "unknown"):
    """Record failed subscription payment."""
    if _ENABLED:
        _SUBSCRIPTION_PAYMENT_FAILED.labels(plan=plan, reason=reason).inc()  # type: ignore[union-attr]
    else:
        logger.debug(f"metric subscription_payment_failed_total[plan={plan}, reason={reason}] += 1")


def subscription_upgrade(from_plan: str, to_plan: str):
    """Record subscription plan upgrade."""
    if _ENABLED:
        _SUBSCRIPTION_UPGRADES.labels(from_plan=from_plan, to_plan=to_plan).inc()  # type: ignore[union-attr]
    else:
        logger.debug(f"metric subscription_upgrades_total[from={from_plan}, to={to_plan}] += 1")


def invoice_created_by_plan(plan: str):
    """Record invoice creation by subscription plan."""
    if _ENABLED:
        _INVOICE_CREATED_BY_PLAN.labels(plan=plan).inc()  # type: ignore[union-attr]
    else:
        logger.debug(f"metric invoice_created_by_plan_total[plan={plan}] += 1")


def record_invoice_amount(amount_naira: float):
    """Record invoice amount for average value tracking."""
    if _ENABLED:
        _AVERAGE_INVOICE_VALUE.observe(amount_naira)  # type: ignore[union-attr]
    else:
        logger.debug(f"observe invoice_amount_naira={amount_naira}")


class PaymentLatencyTimer:
    def __init__(self):
        self.start = time.perf_counter()

    def stop(self) -> float:
        dur = time.perf_counter() - self.start
        invoice_paid(latency_seconds=dur)
        return dur


__all__ = [
    "invoice_created",
    "invoice_paid",
    "parse_unknown",
    "PaymentLatencyTimer",
    "oauth_login_success",
    "tax_profile_updated",
    "vat_calculation_record",
    "compliance_check_record",
    # OTP
    "otp_signup_requested",
    "otp_signup_verified",
    "otp_login_requested",
    "otp_login_verified",
    "otp_resend_attempt",
    "otp_resend_blocked",
    "otp_invalid_attempt",
    "otp_signup_latency_observe",
    "otp_login_latency_observe",
    "otp_whatsapp_delivery_success",
    "otp_whatsapp_delivery_failure",
    "otp_email_delivery_success",
    "otp_email_delivery_failure",
    "otp_resend_success_conversion",
    # Subscription
    "subscription_payment_initiated",
    "subscription_payment_success",
    "subscription_payment_failed",
    "subscription_upgrade",
    "invoice_created_by_plan",
    "record_invoice_amount",
]
