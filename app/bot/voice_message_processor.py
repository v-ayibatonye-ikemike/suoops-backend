from __future__ import annotations

import logging
from typing import Any, Callable

from app.bot.invoice_intent_processor import InvoiceIntentProcessor
from app.bot.nlp_service import NLPService
from app.bot.whatsapp_client import WhatsAppClient
from app.core.config import settings
from app.models import models

logger = logging.getLogger(__name__)


class VoiceMessageProcessor:
    """Process WhatsApp voice notes by downloading, transcribing, and handling intents."""

    def __init__(
        self,
        client: WhatsAppClient,
        nlp: NLPService,
        invoice_processor: InvoiceIntentProcessor,
        speech_service_factory: Callable[[], Any],
    ) -> None:
        self.client = client
        self.nlp = nlp
        self.invoice_processor = invoice_processor
        self._speech_service_factory = speech_service_factory

    def _check_user_has_business_plan(self, sender: str) -> tuple[bool, models.User | None]:
        """
        Resolve the business user for a WhatsApp sender.

        Voice invoicing is free under the commission model, so access only
        requires a linked business account.

        Returns:
            (has_access: bool, user: User | None)
        """
        # Fast path: feature flag disabled – open access.
        if not settings.FEATURE_VOICE_REQUIRES_PAID:
            return True, None
        # Dev/Test environments bypass gating for easier local workflows.
        if settings.ENV.lower() not in {"prod", "production"}:
            return True, None

        normalized = sender.strip()
        if not normalized.startswith("+"):
            if normalized.startswith("234"):
                normalized = f"+{normalized}"
            elif normalized.startswith("0"):
                normalized = f"+234{normalized[1:]}"
            else:
                normalized = f"+{normalized}"

        user = self.invoice_processor.db.query(models.User).filter(models.User.phone == normalized).first()
        if not user:
            return False, None

        # Voice invoicing is free under the commission model.
        return True, user

    async def process(self, sender: str, media_id: str, payload: dict[str, Any]) -> None:
        try:
            # Check if voice feature is globally enabled
            if not settings.FEATURE_VOICE_ENABLED:
                self.client.send_text(
                    sender,
                    "🎙️ Voice invoices are currently unavailable.\n\n"
                    "Please send a text message instead:\n"
                    '"Invoice [Customer] [Amount] for [Description]"\n\n'
                    'Example: "Invoice Jane 50000 for logo design"',
                )
                return

            # Voice is free; we only need a linked business account.
            has_access, user = self._check_user_has_business_plan(sender)
            if not has_access or user is None:
                self.client.send_text(
                    sender,
                    "❌ Your WhatsApp number isn't linked to a business account.\n"
                    "Register at suoops.com to start invoicing!",
                )
                return

            self.client.send_text(sender, "🎙️ Processing your voice message...")
            media_url = await self.client.get_media_url(media_id)
            audio_bytes = await self.client.download_media(media_url)

            transcript = await self._speech_service_factory().transcribe_audio(audio_bytes)
            if not transcript or len(transcript.split()) < 3:
                self.client.send_text(
                    sender,
                    "⚠️ Your voice message was too short or unclear.\n\n"
                    "Please try again and speak clearly:\n"
                    '"Invoice [Customer Name] [Amount] for [Description]"',
                )
                return

            self.client.send_text(sender, f'📝 I heard: "{transcript}"\n\nProcessing...')
            parse = self.nlp.parse_text(transcript, is_speech=True)
            await self.invoice_processor.handle(sender, parse, payload)
        except Exception as exc:  # noqa: BLE001
            logger.exception("[VOICE] Failed to process audio")
            error_msg = str(exc).lower()
            if "timeout" in error_msg or "connection" in error_msg:
                user_msg = (
                    "⚠️ The voice service is slow right now. " "Please try again in a moment or send a text message."
                )
            elif "too large" in error_msg or "size" in error_msg:
                user_msg = (
                    "⚠️ That voice message is too long.\n\n"
                    "Please keep it under 1 minute, or send a text message instead:\n"
                    "`Invoice Joy 08012345678, 5000 wig`"
                )
            else:
                user_msg = (
                    "❌ Sorry, I couldn't process that voice message.\n\n"
                    "Please try again or send a text message instead:\n"
                    "`Invoice Joy 08012345678, 5000 wig`"
                )
            self.client.send_text(sender, user_msg)
