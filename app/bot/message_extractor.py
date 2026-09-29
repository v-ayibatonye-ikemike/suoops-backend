from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def extract_message(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Extract the first message object from a WhatsApp webhook payload."""
    if not isinstance(payload, dict):
        return None

    # Already normalized payload
    if "entry" not in payload:
        message = dict(payload)
        message.setdefault("raw", payload)
        return message

    try:
        entry = payload.get("entry", [])[0]
        change = entry.get("changes", [])[0]
        value = change.get("value", {})
        messages = value.get("messages", [])
        if not messages:
            # Status webhooks (sent/delivered/read/failed) don't contain
            # messages -- they are delivery receipts, not inbound messages.
            statuses = value.get("statuses", [])
            if statuses:
                status = statuses[0]
                logger.debug(
                    "[STATUS] recipient=%s status=%s timestamp=%s",
                    status.get("recipient_id"),
                    status.get("status"),
                    status.get("timestamp"),
                )
                errors = status.get("errors", [])
                if errors:
                    err = errors[0]
                    wamid = status.get("id")
                    err_code = err.get("code")
                    err_title = err.get("title")
                    err_detail = err.get("error_data", {}).get("details", "")
                    logger.warning(
                        "[STATUS] Delivery FAILED to %s: wamid=%s code=%s title=%s -- %s",
                        status.get("recipient_id"),
                        wamid,
                        err_code,
                        err_title,
                        err_detail,
                    )
                    # If this wamid corresponds to a pending OTP, flag the
                    # delivery failure so the frontend can surface it to the
                    # user instead of leaving them on a spinning OTP screen.
                    if wamid:
                        try:
                            from app.services.otp_service import OTPService

                            OTPService().record_delivery_failure(
                                wamid=wamid,
                                error_code=err_code,
                                error_title=err_title,
                                error_detail=err_detail,
                            )
                        except Exception:  # noqa: BLE001
                            logger.exception("Failed to record OTP delivery failure for wamid=%s", wamid)
            return None

        message = messages[0]
        sender = message.get("from")
        if not sender:
            return None

        msg_type = message.get("type", "text")
        normalized_sender = sender if sender.startswith("+") else f"+{sender}"

        extracted: dict[str, Any] = {"from": normalized_sender, "type": msg_type}

        # WhatsApp message id (needed for read receipts / typing indicator
        # and for resolving inline replies via context.id).
        if message.get("id"):
            extracted["message_id"] = message["id"]

        # If this message is a reply to a previous one, capture the id of
        # the message being replied to so we can resolve inline edits.
        ctx = message.get("context") or {}
        if ctx.get("id"):
            extracted["context_id"] = ctx["id"]

        if msg_type == "text":
            extracted["text"] = message.get("text", {}).get("body", "")
        elif msg_type == "image":
            extracted["image_id"] = message.get("image", {}).get("id")
            extracted["caption"] = message.get("image", {}).get("caption", "")
        elif msg_type == "audio":
            extracted["audio_id"] = message.get("audio", {}).get("id")
        elif msg_type == "interactive":
            # Handle interactive button replies and list selections
            interactive = message.get("interactive", {})
            interactive_type = interactive.get("type")
            if interactive_type == "button_reply":
                # Button was clicked - extract button ID and title
                button_reply = interactive.get("button_reply", {})
                extracted["button_id"] = button_reply.get("id")
                extracted["button_title"] = button_reply.get("title")
                logger.info(
                    "[EXTRACT] Button clicked: id=%s, title=%s",
                    extracted.get("button_id"),
                    extracted.get("button_title"),
                )
            elif interactive_type == "list_reply":
                # List item was selected - extract row ID and title
                list_reply = interactive.get("list_reply", {})
                extracted["list_reply_id"] = list_reply.get("id")
                extracted["list_reply_title"] = list_reply.get("title")
                extracted["list_reply_description"] = list_reply.get("description")
                logger.info(
                    "[EXTRACT] List selected: id=%s, title=%s",
                    extracted.get("list_reply_id"),
                    extracted.get("list_reply_title"),
                )

        contacts = value.get("contacts", [])
        if contacts:
            extracted["contact"] = contacts[0]

        extracted["raw"] = payload
        return extracted
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to parse WhatsApp webhook payload: %s", exc)
        return None
