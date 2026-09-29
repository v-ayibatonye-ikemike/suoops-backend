"""
Speech-to-text service for transcribing WhatsApp voice notes.

Single Responsibility: Handle audio transcription via OpenAI Whisper API.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import httpx

from app.core.config import settings

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class SpeechService:
    """Transcribe audio to text using OpenAI Whisper API."""

    def __init__(self):
        self.api_key = getattr(settings, "OPENAI_API_KEY", None)
        self.base_url = "https://api.openai.com/v1/audio/transcriptions"
        self.model = "whisper-1"

    async def transcribe_audio(self, audio_bytes: bytes, language: str = "en") -> str:
        """
        Transcribe audio bytes to text.

        Args:
            audio_bytes: Raw audio file bytes
            language: Language code (default: en for English)

        Returns:
            Transcribed text string

        Raises:
            ValueError: If API key not configured
            httpx.HTTPError: If transcription fails
        """
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not configured")

        try:
            files = {
                "file": ("audio.ogg", audio_bytes, "audio/ogg"),
            }
            data = {
                "model": self.model,
                "language": language,
            }
            headers = {"Authorization": f"Bearer {self.api_key}"}

            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    self.base_url,
                    headers=headers,
                    files=files,
                    data=data,
                )
                response.raise_for_status()
                result = response.json()
                transcript = result.get("text", "").strip()
                logger.info("[SPEECH] Transcribed %d bytes -> %d chars", len(audio_bytes), len(transcript))
                return transcript

        except httpx.HTTPError as e:
            logger.error("[SPEECH] Transcription failed: %s", e)
            raise
        except Exception as e:
            logger.error("[SPEECH] Unexpected error: %s", e)
            raise


def get_speech_service() -> SpeechService:
    """Dependency injection helper."""
    return SpeechService()
