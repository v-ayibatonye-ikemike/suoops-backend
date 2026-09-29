from __future__ import annotations

import logging
import os
from typing import Any

import httpx
import requests

from app.core.config import settings
from app.utils.phone import normalize_phone

logger = logging.getLogger(__name__)


class WhatsAppClient:
    """WhatsApp Cloud API client for sending messages and downloading media."""

    @staticmethod
    def _is_test_mode() -> bool:
        # Keep tests hermetic: never hit real WhatsApp APIs under pytest.
        if settings.ENV.lower() in {"test", "testing"}:
            return True
        return bool(os.getenv("PYTEST_CURRENT_TEST"))

    @staticmethod
    def _is_valid_recipient(to: str | None) -> bool:
        """True if ``to`` looks like a real phone number Meta will accept.

        Guards against placeholder values (e.g. OAuth signups store
        "oauth_google_x" in the phone column) that Meta rejects with #131009,
        wasting an API call and denting the number's quality signals.
        """
        return WhatsAppClient._clean_recipient(to) is not None

    @staticmethod
    def _clean_recipient(to: str | None) -> str | None:
        """Normalize ``to`` to E.164 digits (no ``+``) and validate it.

        Returns the deliverable digit string, or ``None`` if the value can't be
        a real number Meta will accept. This both normalizes local formats
        (e.g. ``08012345678`` → ``2348012345678``) and rejects placeholders and
        malformed junk (e.g. ``09020452362090``) that Meta rejects with #131009.
        """
        if not to:
            return None
        digits = normalize_phone(str(to)).lstrip("+")
        # Valid E.164: 10–15 digits, and country codes never start with 0.
        if not digits.isdigit() or digits.startswith("0") or not (10 <= len(digits) <= 15):
            return None
        return digits

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", None)
        self.base_url = f"https://graph.facebook.com/v21.0/{self.phone_number_id}/messages"
        self.media_url = "https://graph.facebook.com/v21.0"

    def mark_read(self, message_id: str, *, typing: bool = False) -> bool:
        """Mark an inbound WhatsApp message as read, optionally with the
        "typing…" indicator. The typing bubble auto-clears within ~25 s
        or when the next outbound message is sent — perfect for slow ops
        like PDF generation, OCR, NLP. Best-effort: never raises.
        """
        if not message_id:
            return False
        if self._is_test_mode():
            logger.debug("[WHATSAPP][TEST] mark_read msg=%s typing=%s", message_id, typing)
            return True
        if not self.phone_number_id or not self.api_key:
            return False
        try:
            payload: dict[str, Any] = {
                "messaging_product": "whatsapp",
                "status": "read",
                "message_id": message_id,
            }
            if typing:
                payload["typing_indicator"] = {"type": "text"}
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=5,
            )
            response.raise_for_status()
            return True
        except Exception as exc:  # noqa: BLE001
            logger.debug("[WHATSAPP] mark_read failed for %s: %s", message_id, exc)
            return False

    def send_text(self, to: str, body: str) -> bool:
        """Send a plain text message. Returns True on success, False on failure."""
        if self._is_test_mode():
            logger.info("[WHATSAPP][TEST] Would send text to %s: %s", to, body[:200])
            return True
        if not self.phone_number_id or not self.api_key:
            logger.warning("[WHATSAPP] Not configured, would send to %s: %s", to, body)
            return False
        recipient = self._clean_recipient(to)
        if not recipient:
            logger.warning("[WHATSAPP] Skipping invalid recipient %r (not a phone number)", to)
            return False
        try:
            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "text",
                "text": {"body": body},
            }
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info("[WHATSAPP] ✓ Sent to %s: %s", to, body[:50])
            return True
        except requests.HTTPError as exc:  # pragma: no cover - external service
            detail = exc.response.text if exc.response is not None else "(no body)"
            logger.error(
                "[WHATSAPP] Failed to send to %s: %s | Response: %s",
                to,
                exc,
                detail,
            )
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP] Failed to send to %s: %s", to, exc)
            return False

    def send_document(self, to: str, url: str, filename: str, caption: str | None = None) -> bool:
        """Send a document (usually PDF). Accepts a URL or media_id.

        Returns True on success, False on failure.
        """
        if self._is_test_mode():
            logger.info("[WHATSAPP DOC][TEST] Would send to %s: %s (%s)", to, filename, url)
            return True
        if not self.phone_number_id or not self.api_key:
            logger.warning("[WHATSAPP DOC] Not configured, would send to %s: %s", to, filename)
            return False
        recipient = self._clean_recipient(to)
        if not recipient:
            logger.warning("[WHATSAPP DOC] Skipping invalid recipient %r (not a phone number)", to)
            return False

        try:
            document: dict[str, Any] = {"filename": filename}
            if caption:
                document["caption"] = caption

            # If it looks like a media_id (no :// scheme), use "id"; otherwise "link"
            if "://" in url:
                document["link"] = url
            else:
                document["id"] = url

            payload = {
                "messaging_product": "whatsapp",
                "to": recipient,
                "type": "document",
                "document": document,
            }

            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info("[WHATSAPP DOC] ✓ Sent to %s: %s", to, filename)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP DOC] Failed to send to %s: %s", to, exc)
            return False

    def send_image(self, to: str, url: str, caption: str | None = None) -> bool:
        """Send an image message. Accepts a URL or a media_id.

        Returns True on success, False on failure.
        """
        if self._is_test_mode():
            logger.info("[WHATSAPP IMG][TEST] Would send to %s: %s", to, url)
            return True
        if not self.phone_number_id or not self.api_key:
            logger.warning("[WHATSAPP IMG] Not configured, would send to %s: %s", to, url)
            return False

        try:
            image: dict[str, Any] = {}
            if caption:
                image["caption"] = caption
            # If it looks like a media_id (no :// scheme), use "id"; otherwise "link"
            if "://" in url:
                image["link"] = url
            else:
                image["id"] = url

            payload = {
                "messaging_product": "whatsapp",
                "to": to.replace("+", ""),
                "type": "image",
                "image": image,
            }

            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info("[WHATSAPP IMG] ✓ Sent to %s", to)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP IMG] Failed to send to %s: %s", to, exc)
            return False

    def upload_media(
        self, data: bytes, mime_type: str = "application/pdf", filename: str = "document.pdf"
    ) -> str | None:
        """Upload media bytes directly to WhatsApp's Media API.

        Returns the media_id on success, or None on failure.
        """
        if self._is_test_mode():
            logger.info("[WHATSAPP MEDIA][TEST] Would upload %d bytes as %s", len(data), filename)
            return "test-media-id"
        if not self.phone_number_id or not self.api_key:
            logger.warning("[WHATSAPP MEDIA] Not configured")
            return None

        upload_url = f"{self.media_url}/{self.phone_number_id}/media"
        try:
            response = requests.post(
                upload_url,
                headers={"Authorization": f"Bearer {self.api_key}"},
                files={"file": (filename, data, mime_type)},
                data={"messaging_product": "whatsapp", "type": mime_type},
                timeout=30,
            )
            response.raise_for_status()
            media_id = response.json().get("id")
            logger.info("[WHATSAPP MEDIA] ✓ Uploaded %s → media_id=%s", filename, media_id)
            return media_id
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP MEDIA] Upload failed for %s: %s", filename, exc)
            return None

    def send_template(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> bool:
        """Send a pre-approved template message.

        Returns True on success, False on failure. Use
        :meth:`send_template_with_id` if you need the returned wamid (e.g. to
        correlate delivery-status webhooks).
        """
        wamid = self.send_template_with_id(to, template_name, language, components)
        if wamid is None:
            return False
        # An empty string is a successful test-mode send.
        return True

    def send_template_with_id(
        self,
        to: str,
        template_name: str,
        language: str,
        components: list[dict[str, Any]] | None = None,
    ) -> str | None:
        """Send a pre-approved template and return the WhatsApp message id (wamid).

        Returns:
            The wamid string on success, ``""`` in test/unconfigured mode,
            or ``None`` on failure.
        """
        if self._is_test_mode():
            logger.info(
                "[WHATSAPP TEMPLATE][TEST] Would send to %s: %s (%s)",
                to,
                template_name,
                language,
            )
            return ""
        if not self.phone_number_id or not self.api_key:
            logger.warning(
                "[WHATSAPP TEMPLATE] Not configured, would send to %s: %s",
                to,
                template_name,
            )
            return None
        if not self._is_valid_recipient(to):
            logger.warning("[WHATSAPP TEMPLATE] Skipping invalid recipient %r (not a phone number)", to)
            return None

        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": self._clean_recipient(to),
            "type": "template",
            "template": {
                "name": template_name,
                "language": {"code": language},
            },
        }

        if components:
            payload["template"]["components"] = components

        try:
            logger.info("[WHATSAPP TEMPLATE] Sending to %s, template=%s, payload=%s", to, template_name, payload)
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            logger.info(
                "[WHATSAPP TEMPLATE] Response status=%s, body=%s",
                response.status_code,
                response.text[:500] if response.text else "empty",
            )
            response.raise_for_status()
            logger.info("[WHATSAPP TEMPLATE] ✓ Sent to %s with %s", to, template_name)
            try:
                data = response.json() or {}
                messages = data.get("messages") or []
                if messages and isinstance(messages, list):
                    wamid = messages[0].get("id")
                    if wamid:
                        return str(wamid)
            except Exception:  # noqa: BLE001
                logger.debug("[WHATSAPP TEMPLATE] Could not parse wamid from response", exc_info=True)
            return ""
        except requests.HTTPError as exc:
            detail = exc.response.text if exc.response is not None else "(no body)"
            logger.error("[WHATSAPP TEMPLATE] HTTP Error to %s: %s | Response: %s", to, exc, detail)
            return None
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP TEMPLATE] Failed to send to %s: %s", to, exc)
            return None

    def send_otp_template(
        self,
        to: str,
        otp_code: str,
        template_name: str = "otp_verifications",
        language: str = "en",
    ) -> str | None:
        """Send OTP verification code using approved authentication template.

        Returns:
            The wamid (WhatsApp message id) on success, ``""`` in test/unconfigured
            mode, or ``None`` on failure. Callers should treat both wamid strings
            and ``""`` as successful sends.
        """
        # Authentication templates with "Copy code" button structure:
        # - Body: {{1}} is the OTP code
        # - Button: URL button with otp{{1}} parameter
        components = [
            {"type": "body", "parameters": [{"type": "text", "text": otp_code}]},
            {"type": "button", "sub_type": "url", "index": "0", "parameters": [{"type": "text", "text": otp_code}]},
        ]

        logger.info("[WHATSAPP OTP] Sending OTP template '%s' to %s", template_name, to)
        return self.send_template_with_id(to, template_name, language, components)

    async def get_media_url(self, media_id: str) -> str:
        """Resolve a media ID into a downloadable URL."""
        if not self.api_key:
            raise ValueError("WhatsApp not configured")

        url = f"{self.media_url}/{media_id}"
        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.get(url, headers=headers)
            response.raise_for_status()
            data = response.json()
            return data["url"]

    async def download_media(self, media_url: str) -> bytes:
        """Download media bytes from the WhatsApp CDN."""
        if not self.api_key:
            raise ValueError("WhatsApp not configured")

        headers = {"Authorization": f"Bearer {self.api_key}"}

        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.get(media_url, headers=headers)
            response.raise_for_status()
            logger.info("[WHATSAPP] Downloaded %d bytes", len(response.content))
            return response.content

    def send_interactive_list(
        self,
        to: str,
        body: str,
        button_text: str,
        sections: list[dict[str, Any]],
        header: str | None = None,
        footer: str | None = None,
    ) -> bool:
        """
        Send an interactive list message (product catalog, menu, etc.).

        WhatsApp limits:
            - Max 10 rows total across all sections
            - Row title max 24 chars, description max 72 chars
            - Button text max 20 chars

        Args:
            to: Recipient phone number
            body: Message body text
            button_text: Text on the button that opens the list
            sections: List of sections, each with 'title' and 'rows'.
                      Each row: {'id': str, 'title': str, 'description': str (optional)}
            header: Optional header text
            footer: Optional footer text

        Returns:
            True if sent successfully
        """
        if self._is_test_mode():
            logger.info("[WHATSAPP LIST][TEST] Would send list to %s: %s", to, body[:100])
            return True
        if not self.phone_number_id or not self.api_key:
            logger.warning("[WHATSAPP LIST] Not configured, would send to %s", to)
            return False

        # Enforce WhatsApp limits
        total_rows = sum(len(s.get("rows", [])) for s in sections)
        if total_rows > 10:
            logger.warning("[WHATSAPP LIST] Truncating to 10 rows (had %d)", total_rows)
            # Truncate rows to fit within 10
            remaining = 10
            for section in sections:
                rows = section.get("rows", [])
                section["rows"] = rows[:remaining]
                remaining -= len(section["rows"])
                if remaining <= 0:
                    break

        interactive: dict[str, Any] = {
            "type": "list",
            "body": {"text": body},
            "action": {
                "button": button_text[:20],
                "sections": sections,
            },
        }

        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}

        payload = {
            "messaging_product": "whatsapp",
            "to": to.replace("+", ""),
            "type": "interactive",
            "interactive": interactive,
        }

        try:
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info("[WHATSAPP LIST] ✓ Sent list to %s with %d items", to, total_rows)
            return True
        except requests.HTTPError as exc:
            detail = exc.response.text if exc.response is not None else "(no body)"
            logger.error("[WHATSAPP LIST] HTTP Error to %s: %s | Response: %s", to, exc, detail)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP LIST] Failed to send to %s: %s", to, exc)
            return False

    def send_interactive_buttons(
        self,
        to: str,
        body: str,
        buttons: list[dict[str, str]],
        header: str | None = None,
        footer: str | None = None,
    ) -> bool:
        """
        Send an interactive message with reply buttons.

        Args:
            to: Recipient phone number
            body: Message body text
            buttons: List of buttons, each with 'id' and 'title' (max 3 buttons, title max 20 chars)
            header: Optional header text
            footer: Optional footer text

        Returns:
            True if sent successfully
        """
        if self._is_test_mode():
            logger.info("[WHATSAPP BUTTONS][TEST] Would send to %s: %s", to, body[:80])
            return True
        if not self.phone_number_id or not self.api_key:
            logger.warning("[WHATSAPP BUTTONS] Not configured, would send to %s", to)
            return False

        # Build button rows (max 3 buttons allowed by WhatsApp)
        button_rows = [{"type": "reply", "reply": {"id": btn["id"], "title": btn["title"][:20]}} for btn in buttons[:3]]

        interactive: dict[str, Any] = {
            "type": "button",
            "body": {"text": body},
            "action": {"buttons": button_rows},
        }

        if header:
            interactive["header"] = {"type": "text", "text": header}
        if footer:
            interactive["footer"] = {"text": footer}

        payload = {
            "messaging_product": "whatsapp",
            "to": to.replace("+", ""),
            "type": "interactive",
            "interactive": interactive,
        }

        try:
            response = requests.post(
                self.base_url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=10,
            )
            response.raise_for_status()
            logger.info("[WHATSAPP BUTTONS] ✓ Sent to %s with %d buttons", to, len(buttons))
            return True
        except requests.HTTPError as exc:
            detail = exc.response.text if exc.response is not None else "(no body)"
            logger.error("[WHATSAPP BUTTONS] HTTP Error to %s: %s | Response: %s", to, exc, detail)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("[WHATSAPP BUTTONS] Failed to send to %s: %s", to, exc)
            return False
