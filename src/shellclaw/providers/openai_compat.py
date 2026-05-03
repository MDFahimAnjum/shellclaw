"""OpenAI-compatible provider (non-streaming /chat/completions).

Covers: Ollama, OpenAI, Groq, OpenRouter, LM Studio, and any endpoint
that returns the standard chat completions JSON shape.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import AsyncIterator

import httpx

from .base import Delta, DeltaDone, DeltaReasoning, DeltaText, DeltaToolCall
from .reasoning_tool_xml import (
    parse_reasoning_xml_tool_calls,
    reasoning_has_tool_call_markup,
)
from .stream_log import log_provider_json_response


def _usage_total_tokens(data: dict) -> int | None:
    """Best-effort ``total_tokens`` from an OpenAI-style chat completion JSON body."""
    usage = data.get("usage")
    if not isinstance(usage, dict):
        return None
    raw = usage.get("total_tokens")
    if isinstance(raw, (int, float)):
        return int(raw)
    p, c = usage.get("prompt_tokens"), usage.get("completion_tokens")
    if isinstance(p, (int, float)) and isinstance(c, (int, float)):
        return int(p) + int(c)
    return None


def _openai_compat_post_target(base_url: str) -> tuple[str | None, str]:
    """Split ``base_url`` into an httpx ``base_url`` and POST path.

    If ``base_url`` already includes ``/chat/completions``, POST to that full URL
    and do not append the segment again.
    """
    raw = base_url.strip().rstrip("/")
    if "/chat/completions" in raw:
        return None, raw
    return raw, "/chat/completions"


def _reasoning_chunk(delta: dict) -> str | None:
    """OpenAI uses reasoning_content; Ollama often uses reasoning; native-style APIs use thinking."""
    for key in ("reasoning_content", "reasoning", "thinking"):
        piece = delta.get(key)
        if piece:
            if isinstance(piece, str):
                return piece
            if isinstance(piece, list):
                # Rare: list of structured parts; stringify conservatively.
                return "".join(str(p) for p in piece)
    return None


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        stream_log_path: Path | None = None,
        temperature: float | None = None,
        num_ctx: int | None = None,
    ) -> None:
        self._model = model
        self._stream_log_path = stream_log_path
        self._temperature = temperature
        self._num_ctx = num_ctx
        http_base, post_target = _openai_compat_post_target(base_url)
        self._post_target = post_target
        _headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        _timeout = httpx.Timeout(60.0, connect=10.0)
        if http_base is None:
            self._client = httpx.AsyncClient(headers=_headers, timeout=_timeout)
        else:
            self._client = httpx.AsyncClient(
                base_url=http_base,
                headers=_headers,
                timeout=_timeout,
            )

    async def stream(
        self,
        messages: list[dict],
        tools: list[dict],
        system: str,
    ) -> AsyncIterator[Delta]:
        payload: dict = {
            "model": self._model,
            "messages": [{"role": "system", "content": system}, *messages],
            "stream": False,
        }
        if self._temperature is not None:
            payload["temperature"] = self._temperature
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if self._num_ctx is not None:
            payload["options"] = {"num_ctx": self._num_ctx}

        resp = await self._client.post(self._post_target, json=payload)
        resp.raise_for_status()
        if self._stream_log_path is not None:
            await log_provider_json_response(
                self._stream_log_path,
                provider_tag=f"openai_compat model={self._model}",
                request_payload=payload,
                response=resp,
            )

        data = resp.json()
        usage_total = _usage_total_tokens(data)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")

        reasoning_buffer = ""
        r = _reasoning_chunk(message)
        if r:
            reasoning_buffer = r
            yield DeltaReasoning(text=r)

        raw_content = message.get("content")
        if isinstance(raw_content, str):
            content_buffer = raw_content
        elif isinstance(raw_content, list):
            parts: list[str] = []
            for block in raw_content:
                if isinstance(block, dict):
                    if block.get("type") == "text" and "text" in block:
                        parts.append(str(block["text"]))
                    elif "text" in block:
                        parts.append(str(block["text"]))
            content_buffer = "".join(parts)
        elif raw_content is None:
            content_buffer = ""
        else:
            content_buffer = str(raw_content)

        if content_buffer:
            yield DeltaText(text=content_buffer)

        structured_calls = message.get("tool_calls") or []
        for tc in structured_calls:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments", "{}")
            if isinstance(raw_args, dict):
                arguments = raw_args
            else:
                try:
                    arguments = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            yield DeltaToolCall(
                call_id=tc.get("id", ""),
                name=fn.get("name", ""),
                arguments=arguments,
            )

        if finish == "stop":
            if not structured_calls and not content_buffer.strip():
                synthetic = (
                    parse_reasoning_xml_tool_calls(reasoning_buffer)
                    if tools
                    else []
                )
                if synthetic:
                    for synthesized in synthetic:
                        yield synthesized
                elif (
                    reasoning_buffer.strip()
                    and not reasoning_has_tool_call_markup(reasoning_buffer)
                ):
                    yield DeltaText(text=reasoning_buffer.strip())
            yield DeltaDone(finish_reason="stop", total_tokens=usage_total)
            return

        yield DeltaDone(total_tokens=usage_total)

    async def close(self) -> None:
        await self._client.aclose()
