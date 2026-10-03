from __future__ import annotations

from typing import Protocol

import httpx

from app.core.config import settings

from .types import AICompletion, AIMessage


class AIProvider(Protocol):
    name: str

    async def complete(
        self,
        *,
        messages: list[AIMessage],
        model: str,
        max_tokens: int,
        temperature: float,
        structured: bool,
    ) -> AICompletion: ...


class OpenAIProvider:
    name = "openai"
    _url = "https://api.openai.com/v1/chat/completions"

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or settings.OPENAI_API_KEY

    async def complete(
        self,
        *,
        messages: list[AIMessage],
        model: str,
        max_tokens: int,
        temperature: float,
        structured: bool,
    ) -> AICompletion:
        if not self._api_key:
            raise RuntimeError("OPENAI_API_KEY is not configured")

        payload: dict[str, object] = {
            "model": model,
            "messages": [{"role": message.role, "content": message.content} for message in messages],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if structured:
            payload["response_format"] = {"type": "json_object"}

        async with httpx.AsyncClient(timeout=settings.AI_PROVIDER_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self._url,
                headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            response.raise_for_status()
            body = response.json()

        usage = body.get("usage") or {}
        choice = body["choices"][0]["message"]
        return AICompletion(
            content=choice["content"],
            provider=self.name,
            model=body.get("model") or model,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
        )


def build_ai_provider() -> AIProvider:
    if settings.AI_PROVIDER != "openai":
        raise ValueError(f"Unsupported AI provider: {settings.AI_PROVIDER}")
    return OpenAIProvider()
