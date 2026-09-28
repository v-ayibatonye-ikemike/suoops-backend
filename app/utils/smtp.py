"""Shared SMTP email helper used by Celery tasks."""
from __future__ import annotations

import logging
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from app.core.config import settings

logger = logging.getLogger(__name__)

# Reusable SMTP connection for batch sends (avoids reconnect per email)
_smtp_connection: smtplib.SMTP | None = None


def get_smtp_configs() -> list[tuple[str, int, str | None, str | None, str]]:
    """All configured SMTP provider blocks, in priority order:

        1. ZeptoMail  (SMTP_*_ZEP)
        2. Brevo      (BREVO_SMTP_LOGIN / BREVO_SMTP_KEY)
        3. Generic    (SMTP_*)

    Only blocks with BOTH a user and password are returned. Senders try each in
    order, so a runtime auth/connection failure on the primary (e.g. a rotated
    ZeptoMail token → SMTP 535) automatically falls back to the next provider.
    Dedicated provider credentials take precedence over legacy generic SMTP
    values so a stale SMTP_PASSWORD cannot shadow a rotated BREVO_SMTP_KEY.
    Each tuple is ``(host, port, user, password, from_email)`` — host+creds always
    travel together so one provider's host is never paired with another's creds.
    """
    configs: list[tuple[str, int, str | None, str | None, str]] = []
    seen: set[tuple[str, str]] = set()
    default_from = getattr(settings, "FROM_EMAIL", None) or "noreply@suoops.com"

    def _add(host: str, port: int, user: str | None, password: str | None, from_email: str) -> None:
        if user and password and (host, user) not in seen:
            configs.append((host, port, user, password, from_email))
            seen.add((host, user))

    # 1) ZeptoMail (explicit _ZEP vars).
    _add(
        getattr(settings, "SMTP_HOST_ZEP", None) or "smtp.zeptomail.com",
        getattr(settings, "SMTP_PORT_ZEP", None) or getattr(settings, "SMTP_PORT", 587),
        getattr(settings, "SMTP_USER_ZEP", None),
        getattr(settings, "SMTP_PASSWORD_ZEP", None),
        getattr(settings, "FROM_EMAIL_ZEP", None) or default_from,
    )

    brevo_smtp_key = getattr(settings, "BREVO_SMTP_KEY", None)
    legacy_brevo_key = getattr(settings, "BREVO_API_KEY", None)
    if not brevo_smtp_key and legacy_brevo_key and legacy_brevo_key.startswith("xsmtpsib-"):
        brevo_smtp_key = legacy_brevo_key

    # 2) Brevo — always its own relay host so it can't inherit ZeptoMail's host
    #    from a shared SMTP_HOST; this is the genuine independent fallback.
    _add(
        "smtp-relay.brevo.com",
        getattr(settings, "SMTP_PORT", 587),
        getattr(settings, "BREVO_SMTP_LOGIN", None),
        brevo_smtp_key,
        default_from,
    )

    # 3) Generic SMTP_* remains available for other providers and legacy
    #    deployments. It comes after dedicated Brevo configuration so stale
    #    generic Brevo credentials cannot shadow a rotated BREVO_SMTP_KEY.
    _add(
        getattr(settings, "SMTP_HOST", None) or "smtp-relay.brevo.com",
        getattr(settings, "SMTP_PORT", 587),
        getattr(settings, "SMTP_USER", None),
        getattr(settings, "SMTP_PASSWORD", None),
        default_from,
    )

    return configs


def get_smtp_config() -> tuple[str, int, str | None, str | None, str]:
    """The highest-priority configured SMTP block (backward-compatible).

    Prefer :func:`send_email_with_fallback` in senders so a failed primary can
    fall back to the next provider automatically.
    """
    configs = get_smtp_configs()
    if configs:
        return configs[0]
    # Nothing configured → return a host with empty creds so callers log
    # "not configured" rather than crashing on a missing tuple.
    default_from = getattr(settings, "FROM_EMAIL", None) or "noreply@suoops.com"
    return (
        getattr(settings, "SMTP_HOST", None) or "smtp-relay.brevo.com",
        getattr(settings, "SMTP_PORT", 587),
        None,
        None,
        default_from,
    )


# Backwards-compat alias (was module-private).
_get_smtp_config = get_smtp_config


def send_email_with_fallback(
    to_email: str,
    subject: str,
    html_body: str | None,
    plain_body: str,
    *,
    check_suppression: bool = True,
    timeout: int = 15,
) -> bool:
    """Send one email, trying each configured provider until one accepts it.

    This is the DRY entry point every caller should use so transactional mail
    (OTP, invites, notifications) survives a single provider outage — a ZeptoMail
    auth failure (SMTP 535) falls back to Brevo automatically. ``check_suppression``
    is skipped for critical mail (e.g. login OTP) that must send regardless.
    """
    configs = get_smtp_configs()
    if not configs:
        logger.warning("SMTP not configured, skipping email to %s", to_email)
        return False

    if check_suppression:
        from app.services.email_suppression import is_suppressed
        if is_suppressed(to_email):
            logger.info("Skipping suppressed address %s", to_email)
            return False

    last_error: str | None = None
    for host, port, user, password, from_email in configs:
        msg = MIMEMultipart("alternative")
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        msg.attach(MIMEText(plain_body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))
        try:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.starttls()
                server.login(user, password)
                server.send_message(msg)
            logger.info("SMTP provider %s accepted email for %s", host, to_email)
            return True
        except Exception as e:  # noqa: BLE001 — fall through to the next provider
            last_error = f"{host}: {e}"
            logger.warning("SMTP send via %s failed for %s: %s", host, to_email, e)
            continue

    logger.error("All SMTP providers failed for %s (last error: %s)", to_email, last_error)
    return False


def send_smtp_email(to_email: str, subject: str, html_body: str | None, plain_body: str) -> bool:
    """Send an email via SMTP with automatic provider fallback. Returns True on success."""
    return send_email_with_fallback(to_email, subject, html_body, plain_body)



def send_smtp_batch(
    emails: list[tuple[str, str, str | None, str]],
) -> list[bool]:
    """Send a batch of emails over a single SMTP connection.

    Each item is (to_email, subject, html_body, plain_body).
    Returns a list of booleans (True=success) in the same order.

    Much faster than calling send_smtp_email() in a loop because
    the TLS handshake + login happens once instead of per-email.
    """
    configs = get_smtp_configs()
    if not configs:
        logger.warning("SMTP not configured, skipping batch of %d emails", len(emails))
        return [False] * len(emails)

    from app.services.email_suppression import is_suppressed

    last_error: str | None = None
    # Try each provider for the connection+login; the first that connects sends
    # the whole batch. A per-recipient send failure is not a provider fault, so
    # it doesn't trigger fallback — only a failed connection/login does.
    for host, port, user, password, from_email in configs:
        results: list[bool] = []
        try:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(user, password)

                for to_email, subject, html_body, plain_body in emails:
                    if is_suppressed(to_email):
                        logger.info("Skipping suppressed address %s", to_email)
                        results.append(False)
                        continue
                    msg = MIMEMultipart("alternative")
                    msg["From"] = from_email
                    msg["To"] = to_email
                    msg["Subject"] = subject
                    msg.attach(MIMEText(plain_body, "plain"))
                    if html_body:
                        msg.attach(MIMEText(html_body, "html"))
                    try:
                        server.send_message(msg)
                        results.append(True)
                    except Exception as e:  # noqa: BLE001
                        logger.warning("Batch SMTP send failed to %s: %s", to_email, e)
                        results.append(False)
            logger.info(
                "SMTP provider %s accepted %d/%d batch emails",
                host,
                sum(results),
                len(results),
            )
            return results
        except Exception as e:  # noqa: BLE001 — connection/login failed → next provider
            last_error = f"{host}: {e}"
            logger.warning("Batch SMTP connection via %s failed: %s", host, e)
            continue

    logger.error("All SMTP providers failed for batch (last error: %s)", last_error)
    return [False] * len(emails)
