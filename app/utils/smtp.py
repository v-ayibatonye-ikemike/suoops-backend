"""Shared SMTP email helper used by Celery tasks."""

from __future__ import annotations

import logging
import smtplib
from base64 import b64encode
from collections.abc import Mapping, Sequence
from email.message import Message
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import getaddresses, parseaddr

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)


class EmailOutcomeUnknown(RuntimeError):
    """SMTP submission began but acceptance could not be confirmed."""

# Reusable SMTP connection for batch sends (avoids reconnect per email)
_smtp_connection: smtplib.SMTP | None = None


def email_api_enabled() -> bool:
    return settings.EMAIL_TRANSPORT == "brevo_api"


def email_is_configured() -> bool:
    if email_api_enabled():
        return bool(settings.BREVO_API_KEY)
    return bool(get_smtp_configs())


def _send_brevo_message(message: Message, timeout: int) -> bool:
    key = settings.BREVO_API_KEY
    if not key or not key.startswith("xkeysib-"):
        logger.error("Brevo HTTPS delivery requires a REST API key")
        return False
    from_header = str(message.get("From") or settings.FROM_EMAIL or "noreply@suoops.com")
    name, address = parseaddr(from_header)
    sender = {"email": address}
    if name:
        sender["name"] = name
    payload: dict[str, object] = {"sender": sender, "subject": str(message.get("Subject", ""))}
    for header, field in (("To", "to"), ("Cc", "cc"), ("Bcc", "bcc")):
        recipients = [
            {"email": email, **({"name": display} if display else {})}
            for display, email in getaddresses([str(message.get(header, ""))])
            if email
        ]
        if recipients:
            payload[field] = recipients
    if "to" not in payload or not address:
        logger.error("Email submission requires a sender and recipient")
        return False
    reply_to = message.get("Reply-To")
    if reply_to:
        reply_name, reply_email = parseaddr(str(reply_to))
        payload["replyTo"] = {"email": reply_email, **({"name": reply_name} if reply_name else {})}
    headers = {
        header: str(message[header])
        for header in ("List-Unsubscribe", "List-Unsubscribe-Post")
        if message.get(header)
    }
    if headers:
        payload["headers"] = headers
    attachments: list[dict[str, str]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        content = part.get_payload(decode=True)
        if not isinstance(content, bytes):
            logger.error("Email MIME part has no encoded content")
            return False
        if part.get_content_disposition() == "attachment":
            attachments.append({"name": part.get_filename() or "attachment", "content": b64encode(content).decode("ascii")})
        elif part.get_content_type() in {"text/plain", "text/html"}:
            field = "htmlContent" if part.get_content_type() == "text/html" else "textContent"
            try:
                payload[field] = content.decode(part.get_content_charset() or "utf-8")
            except (UnicodeError, LookupError):
                logger.error("Email MIME content has an invalid character encoding")
                return False
        else:
            logger.error("Unsupported email MIME content type")
            return False
    if attachments:
        payload["attachment"] = attachments
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(
                "https://api.brevo.com/v3/smtp/email", headers={"api-key": key}, json=payload,
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as error:
        logger.error("Brevo rejected email submission: HTTP %d", error.response.status_code)
        return False
    except httpx.RequestError as error:
        logger.error("Brevo email request failed: %s", type(error).__name__)
        if headers and not isinstance(error, (httpx.ConnectError, httpx.ConnectTimeout)):
            raise EmailOutcomeUnknown("Brevo acceptance is unknown; do not retry on another channel") from error
        return False
    try:
        result = response.json()
    except ValueError as error:
        logger.error("Brevo returned an invalid email acceptance response")
        if headers:
            raise EmailOutcomeUnknown("Brevo acceptance is unknown; do not retry on another channel") from error
        return False
    if response.status_code != 201 or not isinstance(result, dict) or not isinstance(result.get("messageId"), str) or not result["messageId"].strip():
        logger.error("Brevo did not confirm email acceptance")
        if headers:
            raise EmailOutcomeUnknown("Brevo acceptance is unknown; do not retry on another channel")
        return False
    logger.info("Brevo HTTPS API accepted email submission")
    return True


def send_email_message(
    message: Message, *, smtp_config: Mapping[str, object] | None = None, timeout: int = 15,
) -> bool:
    """Deliver existing MIME content using the explicitly selected transport."""
    if email_api_enabled():
        return _send_brevo_message(message, timeout)
    if smtp_config is None:
        host, port, user, password, _ = get_smtp_config()
    else:
        host, port = smtp_config.get("host"), smtp_config.get("port")
        user, password = smtp_config.get("user"), smtp_config.get("password")
    if not isinstance(host, str) or not host or not isinstance(port, int):
        logger.error("Invalid SMTP host or port")
        return False
    if (user is not None and not isinstance(user, str)) or (password is not None and not isinstance(password, str)):
        logger.error("Invalid SMTP credentials")
        return False
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as server:
            server.starttls()
            if user and password:
                server.login(user, password)
            refused = server.send_message(message)
        if refused:
            logger.error("SMTP refused email recipients")
            return False
    except (smtplib.SMTPException, OSError) as error:
        logger.error("SMTP email submission failed: %s", type(error).__name__)
        return False
    return True


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


def get_retention_smtp_configs() -> list[tuple[str, int, str | None, str | None, str]]:
    """Reuse Brevo for commercial mail; never fall back to transactional-only ZeptoMail."""
    return [config for config in get_smtp_configs() if config[0].lower() == "smtp-relay.brevo.com"]


def send_email_with_fallback(
    to_email: str,
    subject: str,
    html_body: str | None,
    plain_body: str,
    *,
    check_suppression: bool = True,
    timeout: int = 15,
    provider_configs: Sequence[tuple[str, int, str | None, str | None, str]] | None = None,
    unsubscribe_url: str | None = None,
) -> bool:
    """Send one email, trying each configured provider until one accepts it.

    This is the DRY entry point every caller should use so transactional mail
    (OTP, invites, notifications) survives a single provider outage — a ZeptoMail
    auth failure (SMTP 535) falls back to Brevo automatically. ``check_suppression``
    is skipped for critical mail (e.g. login OTP) that must send regardless.
    """
    configs = get_smtp_configs() if provider_configs is None else provider_configs
    if not configs and not email_api_enabled():
        logger.warning("SMTP not configured, skipping email to %s", to_email)
        return False

    if check_suppression:
        from app.services.email_suppression import is_suppressed

        if is_suppressed(to_email):
            logger.info("Skipping suppressed address %s", to_email)
            return False

    if email_api_enabled():
        if provider_configs is not None and any(config[0].lower() != "smtp-relay.brevo.com" for config in provider_configs):
            logger.error("Brevo API cannot deliver an explicit non-Brevo provider configuration")
            return False
        msg = MIMEMultipart("alternative")
        msg["From"] = settings.FROM_EMAIL or "noreply@suoops.com"
        msg["To"] = to_email
        msg["Subject"] = subject
        if unsubscribe_url:
            msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.attach(MIMEText(plain_body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))
        return send_email_message(msg, timeout=timeout)

    last_error: str | None = None
    for host, port, user, password, from_email in configs:
        msg = MIMEMultipart("alternative")
        msg["From"] = from_email
        msg["To"] = to_email
        msg["Subject"] = subject
        if unsubscribe_url:
            msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
            msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
        msg.attach(MIMEText(plain_body, "plain"))
        if html_body:
            msg.attach(MIMEText(html_body, "html"))
        submitted = False
        try:
            with smtplib.SMTP(host, port, timeout=timeout) as server:
                server.starttls()
                server.login(user, password)
                submitted = True
                refused = server.send_message(msg)
                if unsubscribe_url and refused:
                    logger.warning("Retention email recipient was refused by %s", host)
                    return False
            logger.info("SMTP provider %s accepted email for %s", host, to_email)
            return True
        except Exception as e:  # noqa: BLE001 — fall through to the next provider
            if unsubscribe_url and submitted and not isinstance(e, smtplib.SMTPRecipientsRefused):
                raise EmailOutcomeUnknown("SMTP acceptance is unknown; do not retry on another channel") from e
            last_error = f"{host}: {e}"
            logger.warning("SMTP send via %s failed for %s: %s", host, to_email, e)
            continue

    logger.error("All SMTP providers failed for %s (last error: %s)", to_email, last_error)
    return False


def send_retention_email(to_email: str, subject: str, plain_body: str, unsubscribe_url: str) -> bool:
    return send_email_with_fallback(
        to_email, subject, None, plain_body,
        provider_configs=get_retention_smtp_configs(),
        unsubscribe_url=unsubscribe_url,
    )

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
    if email_api_enabled():
        return [send_email_with_fallback(*email) for email in emails]
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
