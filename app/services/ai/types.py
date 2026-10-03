from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass(frozen=True)
class AIMessage:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class AIRequest:
    feature: str
    messages: list[AIMessage]
    prompt_version: str
    model: str | None = None
    max_tokens: int = 800
    temperature: float = 0.2
    metadata: dict[str, str | int | float | bool] = field(default_factory=dict)


@dataclass(frozen=True)
class AICompletion:
    content: str
    provider: str
    model: str
    input_tokens: int
    output_tokens: int
