"""Services for generating, sending, and verifying OTP codes via WhatsApp or Email."""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import string
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import redis
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app import metrics
from app.bot.whatsapp_client import WhatsAppClient
from app.core.config import settings
from app.core.redis_utils import get_ca_cert_path, map_cert_reqs, prepare_redis_url
from app.utils.pii import mask_email

logger = logging.getLogger(__name__)


class OTPDeliveryClient(Protocol):
    """Protocol describing the interface required for sending OTP messages."""

    def send_text(self, to: str, body: str) -> None:  # pragma: no cover - protocol stub
        ...


@dataclass
class OTPRecord:
    """Represents an OTP stored for a phone number and purpose."""

    code: str
    attempts: int
    created_at: float

    def serialize(self) -> str:
        return json.dumps(
            {
                "code": self.code,
                "attempts": self.attempts,
                "created_at": self.created_at,
            }
        )

    @classmethod
    def deserialize(cls, payload: str) -> OTPRecord:
        data = json.loads(payload)
        return cls(
            code=data["code"],
            attempts=int(data.get("attempts", 0)),
            created_at=float(data["created_at"]),
        )


class BaseKeyValueStore(Protocol):
    """Minimal key-value interface used by the OTP service."""

    def set(self, key: str, value: str, ttl_seconds: int) -> None:  # pragma: no cover - protocol stub
        ...

    def get(self, key: str) -> str | None:  # pragma: no cover - protocol stub
        ...

    def delete(self, key: str) -> None:  # pragma: no cover - protocol stub
        ...


class RedisStore(BaseKeyValueStore):
    """Redis-backed store for OTP codes and signup sessions."""

    def __init__(self, url: str) -> None:
        # Use centralized Redis client for connection pooling
        try:
            from app.db.redis_client import get_redis_client

            self._client = get_redis_client()
        except Exception as e:
            logger.warning("OTP service falling back to direct Redis connection: %s", e)
            # Fallback to direct connection
            options: dict[str, Any] = {"decode_responses": True}
            tls_url = prepare_redis_url(url) or url
            if tls_url and tls_url.startswith("rediss://"):
                options["ssl_cert_reqs"] = map_cert_reqs()
                options["ssl_ca_certs"] = get_ca_cert_path()

            self._client = redis.Redis.from_url(tls_url, **options)

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        self._client.setex(key, ttl_seconds, value)

    def get(self, key: str) -> str | None:
        return self._client.get(key)

    def delete(self, key: str) -> None:
        self._client.delete(key)


class InMemoryStore(BaseKeyValueStore):
    """Fallback in-memory store used for tests or when Redis is unavailable."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[str, float]] = {}

    def set(self, key: str, value: str, ttl_seconds: int) -> None:
        expires_at = time.time() + ttl_seconds
        self._data[key] = (value, expires_at)

    def get(self, key: str) -> str | None:
        entry = self._data.get(key)
        if not entry:
            return None
        value, expires_at = entry
        if time.time() > expires_at:
            self._data.pop(key, None)
            return None
        return value

    def delete(self, key: str) -> None:
        self._data.pop(key, None)


_SHARED_STORE: BaseKeyValueStore | None = None


def _build_store() -> BaseKeyValueStore:
    """Return a shared OTP store.

    Behaviour:
    * If ``REDIS_URL`` is configured, always return a ``RedisStore`` (cached).
      We do NOT perform a live healthcheck here, because a transient Redis
      hiccup at worker boot (e.g. "max number of clients reached") would
      otherwise crash the entire worker before it can serve any traffic.
      The ``RedisStore`` is a thin wrapper around the shared connection pool;
      real OTP operations performed at request time will surface Redis errors
      to the caller as 500s, prompting a client-side retry that will likely
      hit a healthy moment.
    * We also do NOT cache an ``InMemoryStore`` fallback when ``REDIS_URL``
      is set, because doing so would lock the worker into per-process
      split-brain mode for its entire lifetime — OTPs written by one worker
      would be invisible to others. Failing loudly is preferable.
    * Only when ``REDIS_URL`` is empty (tests / local dev without Redis) do we
      use ``InMemoryStore``, and that fallback IS cached because it is the
      only store available.
    """
    global _SHARED_STORE
    if _SHARED_STORE is not None:
        return _SHARED_STORE
    redis_url = getattr(settings, "REDIS_URL", "")
    if redis_url:
        try:
            store = RedisStore(redis_url)
        except Exception as exc:  # noqa: BLE001
            # Constructing the RedisStore should not require a live connection
            # (it just resolves the shared pool). If it somehow fails, fall
            # back to in-memory ONLY in dev/test. In prod, re-raise so the
            # deploy fails clearly instead of starting in split-brain mode.
            env = getattr(settings, "ENV", "dev").lower()
            if env in {"prod", "production"}:
                logger.error(
                    "OTP RedisStore construction failed in %s; aborting: %s",
                    env,
                    exc,
                )
                raise
            logger.warning(
                "OTP RedisStore unavailable in %s; falling back to in-memory store: %s",
                env,
                exc,
            )
        else:
            _SHARED_STORE = store
            return store
    logger.warning("REDIS_URL not configured — using InMemoryStore for OTPs")
    _SHARED_STORE = InMemoryStore()
    return _SHARED_STORE


class OTPService:
    """Generate, send, and validate OTP codes via Email or WhatsApp for signup and login flows."""

    DEFAULT_DIGITS = 6
    OTP_TTL = 10 * 60  # 10 minutes
    SIGNUP_SESSION_TTL = 30 * 60  # 30 minutes
    RESEND_COOLDOWN = 60  # 1 minute between resend attempts
    MAX_ATTEMPTS = 3

    def __init__(
        self,
        store: BaseKeyValueStore | None = None,
        delivery: OTPDeliveryClient | None = None,
        otp_length: int = DEFAULT_DIGITS,
    ) -> None:
        self._store = store or _build_store()
        self._delivery = delivery or WhatsAppClient(settings.WHATSAPP_API_KEY)
        self._otp_length = otp_length

    @staticmethod
    def _otp_key(identifier: str, purpose: str) -> str:
        """Generate OTP key for phone or email."""
        return f"otp:{purpose}:{identifier}"

    @staticmethod
    def _signup_key(identifier: str) -> str:
        """Generate signup data key for phone or email."""
        return f"signup-data:{identifier}"

    @staticmethod
    def _wamid_key(wamid: str) -> str:
        """Map a WhatsApp message id back to the OTP it belongs to."""
        return f"otp:wamid:{wamid}"

    @staticmethod
    def _delivery_failure_key(identifier: str, purpose: str) -> str:
        """Flag set when a status webhook reports delivery failure."""
        return f"otp:delivery-failed:{purpose}:{identifier}"

    def record_delivery_failure(
        self,
        wamid: str,
        error_code: int | str | None = None,
        error_title: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """Mark the OTP associated with ``wamid`` as undeliverable.

        Returns True if a matching OTP session was found and flagged.
        """
        if not wamid:
            return False
        mapping_raw = self._store.get(self._wamid_key(wamid))
        if not mapping_raw:
            return False
        try:
            mapping = json.loads(mapping_raw)
            purpose = mapping["purpose"]
            identifier = mapping["identifier"]
        except (ValueError, KeyError):
            logger.warning("Malformed wamid mapping for %s: %r", wamid, mapping_raw)
            return False
        payload = json.dumps(
            {
                "code": str(error_code) if error_code is not None else None,
                "title": error_title,
                "detail": error_detail,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._store.set(
            self._delivery_failure_key(identifier, purpose),
            payload,
            self.OTP_TTL,
        )
        logger.info(
            "[OTP] Recorded delivery failure for %s purpose=%s wamid=%s code=%s",
            identifier,
            purpose,
            wamid,
            error_code,
        )
        # Mapping no longer needed once we've flagged the failure.
        self._store.delete(self._wamid_key(wamid))
        return True

    def get_delivery_status(self, identifier: str, purpose: str) -> dict[str, Any]:
        """Return delivery status for a pending OTP.

        Possible ``state`` values:
            - ``failed``  – status webhook reported the message could not be delivered
            - ``pending`` – OTP issued, no failure reported yet
            - ``none``    – no active OTP for this identifier+purpose
        """
        failure_raw = self._store.get(self._delivery_failure_key(identifier, purpose))
        if failure_raw:
            try:
                info = json.loads(failure_raw)
            except ValueError:
                info = {}
            return {
                "state": "failed",
                "code": info.get("code"),
                "title": info.get("title"),
                "detail": info.get("detail"),
            }
        if self._store.get(self._otp_key(identifier, purpose)):
            return {"state": "pending"}
        return {"state": "none"}

    def _get_delivery_method(self, identifier: str) -> str:
        """Determine if identifier is email or phone number."""
        if "@" in identifier:
            return "email"
        return "whatsapp"

    def _send_email_otp(self, email: str, otp: str, purpose: str) -> None:
        """Send OTP via email, trying every configured provider (ZeptoMail →
        Brevo) so a single provider's SMTP auth failure doesn't block login."""
        from app.utils.smtp import get_smtp_configs, send_email_with_fallback

        if not get_smtp_configs():
            logger.error("SMTP not configured. Set SMTP_*_ZEP / SMTP_* / BREVO_SMTP_LOGIN+BREVO_SMTP_KEY.")
            raise ValueError("Email OTP is not available")

        # Render the HTML template (+ plain-text fallback body).
        template_dir = Path(__file__).parent.parent.parent / "templates" / "email"
        jinja_env = Environment(
            loader=FileSystemLoader(str(template_dir)), autoescape=select_autoescape(["html", "xml"])
        )
        html_body = jinja_env.get_template("otp_verification.html").render(
            otp_code=otp,
            purpose=purpose,
            current_year=datetime.now(timezone.utc).year,
        )
        action = "complete your signup" if purpose == "signup" else "login securely"
        plain_body = (
            "SuoOps Verification Code\n\n"
            f"Your OTP is {otp}.\n\n"
            f"Enter this code to {action}.\n"
            "This code expires in 10 minutes.\n\n"
            "If you did not request this code, please ignore this message.\n\n"
            "---\nPowered by SuoOps\n"
        )

        # OTP is critical transactional mail: skip suppression and use the shared
        # multi-provider sender so a ZeptoMail 535 falls back to Brevo automatically.
        sent = send_email_with_fallback(
            email,
            "SuoOps Verification Code",
            html_body,
            plain_body,
            check_suppression=False,
        )
        if not sent:
            raise ValueError("Failed to send OTP email. Please try again.")

        logger.info("Successfully sent email OTP to %s", mask_email(email))

    def request_signup(self, identifier: str, payload: dict[str, Any], deliver_to: str | None = None) -> str:
        """Start signup by persisting user-provided data and sending OTP.

        The OTP is keyed under ``identifier`` (the phone) so verification is
        unchanged, but delivered to ``deliver_to`` (the email) when given — the
        signup code verifies the account by email; the WhatsApp number is proven
        separately when the user first messages the bot. Returns the channel used.
        """
        now = datetime.now(timezone.utc)
        enriched = {**payload, "_requested_at": now.isoformat()}
        self._store.set(self._signup_key(identifier), json.dumps(enriched), self.SIGNUP_SESSION_TTL)
        return self._send_otp(identifier, purpose="signup", deliver_to=deliver_to)

    def complete_signup(self, identifier: str, otp: str) -> dict[str, Any]:
        """Validate OTP and return stored signup data."""
        if not self.verify_otp(identifier, otp, purpose="signup"):
            raise ValueError("Invalid or expired OTP")
        raw_payload = self._store.get(self._signup_key(identifier))
        if not raw_payload:
            raise ValueError("Signup session expired")
        self._store.delete(self._signup_key(identifier))
        data = json.loads(raw_payload)
        data.pop("_requested_at", None)  # Not needed post verification
        return data

    def request_login(self, identifier: str, deliver_to: str | None = None) -> str:
        """Send OTP for login.

        Args:
            identifier: Phone number or email address the OTP is keyed under
                (must match what the client submits at verify).
            deliver_to: Optional address to DELIVER the code to instead of
                ``identifier`` — e.g. deliver a phone-login code to the user's
                email so we don't pay for a WhatsApp message. Keying is
                unchanged, so verification still works with ``identifier``.

        Returns the delivery channel actually used ("email" or "whatsapp").
        """
        return self._send_otp(identifier, purpose="login", deliver_to=deliver_to)

    def send_code(self, identifier: str, purpose: str) -> None:
        """Generate and deliver an OTP for an arbitrary purpose.

        Used by flows (e.g. admin passwordless login) that need a code outside
        the standard signup/login helpers. Delivery channel is inferred from the
        identifier (email vs phone).
        """
        self._send_otp(identifier, purpose=purpose)

    def verify_otp(self, identifier: str, otp: str, purpose: str) -> bool:
        key = self._otp_key(identifier, purpose)
        raw_record = self._store.get(key)
        logger.info("OTP verify | key=%s found=%s", key, bool(raw_record))
        if not raw_record:
            logger.warning("OTP not found in Redis | key=%s", key)
            metrics.otp_invalid_attempt()
            return False
        record = OTPRecord.deserialize(raw_record)
        logger.info("OTP record | attempts=%d", record.attempts)
        if record.attempts >= self.MAX_ATTEMPTS:
            logger.warning("OTP max attempts exceeded | key=%s", key)
            self._store.delete(key)
            metrics.otp_invalid_attempt()
            return False
        if not hmac.compare_digest(str(record.code), str(otp)):
            logger.warning("OTP mismatch | key=%s attempt=%d", key, record.attempts + 1)
            record.attempts += 1
            self._store.set(key, record.serialize(), int(self.OTP_TTL))
            metrics.otp_invalid_attempt()
            return False
        # Success path: record latency
        latency = datetime.now(timezone.utc).timestamp() - record.created_at
        if latency >= 0:
            if purpose == "signup":
                metrics.otp_signup_latency_observe(latency)
            elif purpose == "login":
                metrics.otp_login_latency_observe(latency)
        # Resend conversion check before deleting OTP key
        resend_flag_key = f"otp:resend-used:{purpose}:{identifier}"
        if self._store.get(resend_flag_key):
            metrics.otp_resend_success_conversion()
            self._store.delete(resend_flag_key)
        self._store.delete(key)
        return True

    def resend_otp(self, identifier: str, purpose: str, deliver_to: str | None = None) -> str:
        """Resend OTP for phone or email.

        Args:
            identifier: Phone number or email address the OTP is keyed under.
            purpose: 'signup' or 'login'
            deliver_to: Optional address to deliver to instead of ``identifier``
                (see ``request_login``). Returns the channel used.
        """
        key = self._otp_key(identifier, purpose)
        raw_record = self._store.get(key)
        if raw_record:
            record = OTPRecord.deserialize(raw_record)
            elapsed = datetime.now(timezone.utc).timestamp() - record.created_at
            if elapsed < self.RESEND_COOLDOWN:
                raise ValueError("Please wait before requesting another code")
        # For a signup resend, recover the email delivery target from the pending
        # signup session (the user doesn't exist yet) so the code still emails.
        if deliver_to is None and purpose == "signup":
            raw_signup = self._store.get(self._signup_key(identifier))
            if raw_signup:
                try:
                    deliver_to = json.loads(raw_signup).get("email") or None
                except ValueError:
                    deliver_to = None
        channel = self._send_otp(identifier, purpose, deliver_to=deliver_to)
        # Mark that a resend occurred (ephemeral flag used for conversion metric)
        self._store.set(f"otp:resend-used:{purpose}:{identifier}", "1", self.OTP_TTL)
        return channel

    def _send_otp(self, identifier: str, purpose: str, deliver_to: str | None = None) -> str:
        """Send OTP via email or WhatsApp.

        The OTP is always KEYED under ``identifier`` (so verification matches
        what the client submits), but DELIVERED to ``deliver_to`` when given.
        This lets a phone-keyed login code be emailed to the user, avoiding a
        paid WhatsApp message. Returns the channel used ("email"/"whatsapp").

        Args:
            identifier: Phone number or email the OTP is keyed under.
            purpose: 'signup' or 'login'
            deliver_to: Optional delivery address override.
        """
        code = self._generate_code()
        now_ts = datetime.now(timezone.utc).timestamp()
        record = OTPRecord(code=code, attempts=0, created_at=now_ts)
        self._store.set(self._otp_key(identifier, purpose), record.serialize(), self.OTP_TTL)
        # Clear any stale delivery-failure flag from a previous attempt.
        self._store.delete(self._delivery_failure_key(identifier, purpose))

        target = deliver_to or identifier
        # Prefer email delivery when the target is an email address.
        if self._get_delivery_method(target) == "email":
            try:
                self._send_email_otp(target, code, purpose)
                metrics.otp_email_delivery_success()
                return "email"
            except Exception:
                metrics.otp_email_delivery_failure()
                # Fall back to WhatsApp ONLY when email was a cheaper delivery for
                # a phone-keyed code (i.e. we can still reach the user by phone).
                # When email is the primary identifier there is no phone to fall
                # back to, so re-raise.
                if not (deliver_to and self._get_delivery_method(identifier) == "whatsapp"):
                    raise
                logger.warning("Email OTP delivery failed; falling back to WhatsApp for %s", identifier)

        # WhatsApp delivery — primary phone OTP, or fallback after an email failure.
        try:
            wamid = self._delivery.send_otp_template(
                to=identifier,
                otp_code=code,
                template_name="otp_verifications",  # Approved authentication template
                language="en",
            )
            # send_otp_template returns the wamid on success, "" in test/
            # unconfigured mode, or None on failure.
            if wamid is None:
                metrics.otp_whatsapp_delivery_failure()
                raise ValueError("Failed to send OTP via WhatsApp template")
            metrics.otp_whatsapp_delivery_success()
            logger.info("OTP template sent successfully to %s (wamid=%s)", identifier, wamid or "<none>")
            if wamid:
                # Map wamid -> (purpose, identifier) so the delivery-status
                # webhook can flag failures back to the waiting user.
                self._store.set(
                    self._wamid_key(wamid),
                    json.dumps({"purpose": purpose, "identifier": identifier}),
                    self.OTP_TTL,
                )
        except Exception:
            metrics.otp_whatsapp_delivery_failure()
            raise
        return "whatsapp"

    def _generate_code(self) -> str:
        return "".join(secrets.choice(string.digits) for _ in range(self._otp_length))

    def _format_message(self, otp: str, purpose: str) -> str:
        action = "complete your signup" if purpose == "signup" else "login securely"
        return (
            "SuoOps Verification Code\n\n"
            f"Your OTP is {otp}.\n\n"
            f"Enter this code to {action}.\n"
            "This code expires in 10 minutes.\n\n"
            "If you did not request this code, please ignore this message."
        )
