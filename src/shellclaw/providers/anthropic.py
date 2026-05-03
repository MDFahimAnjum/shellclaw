"""Anthropic provider adapter.

Anthropic uses a different auth header (x-api-key), a slightly different
message schema, and wraps tool calls differently from the OpenAI format.
This thin adapter normalises everything to the same Delta sequence (non-streaming JSON).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import httpx

from .base import Delta, DeltaDone, DeltaText, DeltaToolCall


def _anthropic_usage_total(body: dict) -> int | None:
    usage = body.get("usage")
    if not isinstance(usage, dict):
        return None
    inp = usage.get("input_tokens")
    out = usage.get("output_tokens")
    if isinstance(inp, (int, float)) and isinstance(out, (int, float)):
        return int(inp) + int(out)
    if isinstance(inp, (int, float)):
        return int(inp)
    if isinstance(out, (int, float)):
        return int(out)
    return None
from .stream_log import log_provider_json_response

ANTHROPIC_API_URL = "https://api.anthropic.com"
ANTHROPIC_API_VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 4096


def _convert_tools_to_anthropic(tools: list[dict]) -> list[dict]:
    """Convert OpenAI-style tool schema to Anthropic's format."""
    converted = []
    for tool in tools:
        fn = tool.get("function", tool)
        converted.append({
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        })
    return converted


def _convert_messages_to_anthropic(messages: list[dict]) -> list[dict]:
    """Convert messages list; tool results use Anthropic's tool_result format."""
    converted = []
    for msg in messages:
        role = msg["role"]
        if role == "tool":
            converted.append({
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": msg.get("content", ""),
                    }
                ],
            })
        elif role == "assistant" and msg.get("tool_calls"):
            content = []
            if msg.get("content"):
                content.append({"type": "text", "text": msg["content"]})
            for tc in msg["tool_calls"]:
                content.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": json.loads(tc["function"]["arguments"] or "{}"),
                })
            converted.append({"role": "assistant", "content": content})
        else:
            converted.append({"role": role, "content": msg.get("content", "")})
    return converted


class AnthropicProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        stream_log_path: Path | None = None,
    ) -> None:
        self._model = model
        self._stream_log_path = stream_log_path
        self._client = httpx.AsyncClient(
            base_url=ANTHROPIC_API_URL,
            headers={
                "x-api-key": api_key,
                "anthropic-version": ANTHROPIC_API_VERSION,
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(60.0, connect=10.0),
        )

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AsyncIterator[Delta]:
        payload: dict = {
            "model": self._model,
            "max_tokens": DEFAULT_MAX_TOKENS,
            "system": system,
            "messages": _convert_messages_to_anthropic(messages),
            "stream": False,
        }
        if tools:
            payload["tools"] = _convert_tools_to_anthropic(tools)

        resp = await self._client.post("/v1/messages", json=payload)
        resp.raise_for_status()
        if self._stream_log_path is not None:
            await log_provider_json_response(
                self._stream_log_path,
                provider_tag=f"anthropic model={self._model}",
                request_payload=payload,
                response=resp,
            )

        body = resp.json()
        usage_total = _anthropic_usage_total(body)
        for block in body.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text", "")
                if text:
                    yield DeltaText(text=text)
            elif btype == "tool_use":
                raw_input = block.get("input")
                if isinstance(raw_input, dict):
                    arguments = raw_input
                else:
                    try:
                        arguments = json.loads(raw_input or "{}")
                    except (json.JSONDecodeError, TypeError):
                        arguments = {}
                if not isinstance(arguments, dict):
                    arguments = {}
                yield DeltaToolCall(
                    call_id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=arguments,
                )

        yield DeltaDone(total_tokens=usage_total)

    async def close(self) -> None:
        await self._client.aclose()
