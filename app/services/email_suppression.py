"""SES bounce/complaint handling via Amazon SNS.

Receives SNS notifications published by SES (bounce + complaint events),
verifies the SNS signature, and records suppressed addresses so the send
path can skip them — protecting domain sending reputation.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any

import httpx
from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.db.session import SessionLocal
from app.models.models import EmailSuppression

logger = logging.getLogger(__name__)

# Only certs served from an AWS SNS host are trusted.
_SNS_HOST_RE = re.compile(r"^sns\.[a-z0-9-]+\.amazonaws\.com$")

# Cache fetched signing certificates by URL (they rotate rarely).
_cert_cache: dict[str, bytes] = {}


# ── Suppression store ────────────────────────────────────────────────────


def is_suppressed(email: str) -> bool:
    """Return True if the address is on the suppression list."""
    if not email:
        return False
    key = email.strip().lower()
    try:
        with SessionLocal() as db:
            return db.query(EmailSuppression.id).filter(EmailSuppression.email == key).first() is not None
    except Exception as e:  # never let a lookup failure block legitimate sends
        logger.warning("Suppression lookup failed for %s: %s", email, e)
        return False


def _record_suppression(email: str, reason: str, detail: str | None) -> bool:
    key = (email or "").strip().lower()
    if not key:
        return False
    try:
        with SessionLocal() as db:
            existing = db.query(EmailSuppression).filter(EmailSuppression.email == key).first()
            if existing:
                return False
            db.add(
                EmailSuppression(
                    email=key,
                    reason=reason,
                    detail=(detail or "")[:255] or None,
                    source="ses",
                )
            )
            db.commit()
            logger.info("Suppressed %s (%s)", key, reason)
            return True
    except Exception as e:
        logger.warning("Failed to record suppression for %s: %s", email, e)
        return False


# ── SNS signature verification ───────────────────────────────────────────


def _string_to_sign(message: dict[str, Any]) -> str | None:
    msg_type = message.get("Type")
    if msg_type == "Notification":
        keys = ["Message", "MessageId", "Subject", "Timestamp", "TopicArn", "Type"]
    elif msg_type in ("SubscriptionConfirmation", "UnsubscribeConfirmation"):
        keys = ["Message", "MessageId", "SubscribeURL", "Timestamp", "Token", "TopicArn", "Type"]
    else:
        return None

    parts: list[str] = []
    for key in keys:
        if key == "Subject" and key not in message:
            continue
        if key not in message:
            return None
        parts.append(key)
        parts.append(message[key])
    return "\n".join(parts) + "\n"


def _fetch_cert(url: str) -> bytes | None:
    if url in _cert_cache:
        return _cert_cache[url]
    try:
        resp = httpx.get(url, timeout=10.0)
        if resp.status_code != 200:
            logger.warning("SNS cert fetch failed: %s %s", url, resp.status_code)
            return None
        _cert_cache[url] = resp.content
        return resp.content
    except Exception as e:
        logger.warning("SNS cert fetch error %s: %s", url, e)
        return None


def verify_sns_signature(message: dict[str, Any]) -> bool:
    """Verify an SNS message signature against its signing certificate."""
    cert_url = message.get("SigningCertURL", "")
    if not cert_url.startswith("https://"):
        logger.warning("SNS SigningCertURL not https")
        return False
    host = httpx.URL(cert_url).host
    if not _SNS_HOST_RE.match(host):
        logger.warning("SNS SigningCertURL host not trusted: %s", host)
        return False

    string_to_sign = _string_to_sign(message)
    if string_to_sign is None:
        logger.warning("SNS message missing fields for signature")
        return False

    signature_b64 = message.get("Signature")
    if not signature_b64:
        return False
    try:
        signature = base64.b64decode(signature_b64)
    except Exception:
        return False

    cert_bytes = _fetch_cert(cert_url)
    if not cert_bytes:
        return False

    try:
        cert = x509.load_pem_x509_certificate(cert_bytes)
        algo = hashes.SHA256() if message.get("SignatureVersion") == "2" else hashes.SHA1()
        cert.public_key().verify(  # type: ignore[union-attr]
            signature,
            string_to_sign.encode("utf-8"),
            padding.PKCS1v15(),
            algo,
        )
        return True
    except InvalidSignature:
        logger.warning("SNS signature invalid")
        return False
    except Exception as e:
        logger.warning("SNS signature verification error: %s", e)
        return False


# ── Notification handling ────────────────────────────────────────────────


def _handle_ses_event(inner: dict[str, Any]) -> int:
    """Process an SES bounce/complaint event. Returns number suppressed."""
    event_type = inner.get("notificationType") or inner.get("eventType")
    suppressed = 0

    if event_type == "Bounce":
        bounce = inner.get("bounce", {})
        # Only permanent (hard) bounces are suppressed; transient ones may recover.
        if bounce.get("bounceType") != "Permanent":
            return 0
        sub = bounce.get("bounceSubType")
        for r in bounce.get("bouncedRecipients", []):
            if _record_suppression(r.get("emailAddress"), "bounce", sub):
                suppressed += 1

    elif event_type == "Complaint":
        complaint = inner.get("complaint", {})
        sub = complaint.get("complaintFeedbackType")
        for r in complaint.get("complainedRecipients", []):
            if _record_suppression(r.get("emailAddress"), "complaint", sub):
                suppressed += 1

    return suppressed


def process_sns_payload(raw: bytes, msg_type_header: str) -> dict[str, Any]:
    """Verify and process an incoming SNS payload. Returns a small status dict.

    Raises ValueError on verification failure so the caller can return 403.
    """
    try:
        message = json.loads(raw)
    except Exception:
        raise ValueError("invalid_json")

    if not verify_sns_signature(message):
        raise ValueError("signature_invalid")

    msg_type = message.get("Type") or msg_type_header

    # Auto-confirm the subscription so SES can start delivering events.
    if msg_type == "SubscriptionConfirmation":
        subscribe_url = message.get("SubscribeURL", "")
        host = httpx.URL(subscribe_url).host if subscribe_url else ""
        if subscribe_url.startswith("https://") and _SNS_HOST_RE.match(host):
            try:
                httpx.get(subscribe_url, timeout=10.0)
                logger.info("Confirmed SNS subscription for topic %s", message.get("TopicArn"))
            except Exception as e:
                logger.warning("SNS subscription confirm failed: %s", e)
        return {"status": "subscription_confirmed"}

    if msg_type == "Notification":
        try:
            inner = json.loads(message.get("Message", "{}"))
        except Exception:
            return {"status": "ignored", "reason": "unparsable_message"}
        suppressed = _handle_ses_event(inner)
        return {"status": "ok", "suppressed": suppressed}

    return {"status": "ignored", "reason": msg_type}
