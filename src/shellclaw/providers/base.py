"""Provider protocol and Delta event types shared by all LLM backends."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Protocol, runtime_checkable


@dataclass
class DeltaText:
    """A streamed text chunk from the model."""
    text: str


@dataclass
class DeltaReasoning:
    """A streamed reasoning / thinking chunk (separate from visible assistant content)."""
    text: str


@dataclass
class DeltaToolCall:
    """The model wants to call a tool."""
    call_id: str
    name: str
    arguments: dict


@dataclass
class DeltaDone:
    """Stream is complete. finish_reason included for inspection."""
    finish_reason: str = "stop"
    total_tokens: int | None = None


Delta = DeltaText | DeltaReasoning | DeltaToolCall | DeltaDone


@runtime_checkable
class Provider(Protocol):
    """Minimal contract every LLM backend must satisfy."""

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AsyncIterator[Delta]:
        ...
