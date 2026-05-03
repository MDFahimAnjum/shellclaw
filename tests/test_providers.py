"""Tests for provider adapters using mocked HTTP responses."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import respx
import httpx

from shellclaw.providers.base import DeltaDone, DeltaText, DeltaToolCall
from shellclaw.providers.openai_compat import OpenAICompatProvider
from shellclaw.providers.anthropic import AnthropicProvider


class TestOpenAICompatProvider:
    @pytest.fixture
    def provider(self):
        return OpenAICompatProvider(
            base_url="http://localhost:11434/v1",
            api_key="test",
            model="test-model",
        )

    @respx.mock
    async def test_text_streaming(self, provider):
        completion = {
            "choices": [{
                "message": {"role": "assistant", "content": "Hello world"},
                "finish_reason": "stop",
            }]
        }

        respx.post("http://localhost:11434/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json=completion,
                headers={"content-type": "application/json"},
            )
        )

        deltas = []
        async for delta in provider.stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="you are helpful",
        ):
            deltas.append(delta)

        text_deltas = [d for d in deltas if isinstance(d, DeltaText)]
        assert len(text_deltas) >= 1
        full_text = "".join(d.text for d in text_deltas)
        assert "Hello" in full_text

    @respx.mock
    async def test_tool_call_streaming(self, provider):
        completion = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": "call_abc",
                        "type": "function",
                        "function": {
                            "name": "run_safe",
                            "arguments": '{"cmd":"df -h"}',
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }]
        }

        respx.post("http://localhost:11434/v1/chat/completions").mock(
            return_value=httpx.Response(
                200,
                json=completion,
                headers={"content-type": "application/json"},
            )
        )

        deltas = []
        async for delta in provider.stream(
            messages=[{"role": "user", "content": "check disk"}],
            tools=[],
            system="",
        ):
            deltas.append(delta)

        tool_deltas = [d for d in deltas if isinstance(d, DeltaToolCall)]
        assert len(tool_deltas) == 1
        assert tool_deltas[0].name == "run_safe"
        assert tool_deltas[0].arguments == {"cmd": "df -h"}

    @respx.mock
    async def test_full_base_url_with_chat_completions_not_duplicated(self):
        """POST targets the given URL when it already includes /chat/completions."""
        completion = {
            "choices": [{
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }]
        }
        route = respx.post("https://nebius.example/v1/chat/completions").mock(
            return_value=httpx.Response(200, json=completion),
        )
        provider = OpenAICompatProvider(
            base_url="https://nebius.example/v1/chat/completions",
            api_key="k",
            model="m",
        )
        async for _delta in provider.stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="sys",
        ):
            pass
        assert route.called
        await provider.close()

    @respx.mock
    async def test_payload_always_includes_model(self):
        captured: dict = {}

        def _capture(request: httpx.Request) -> httpx.Response:
            captured["json"] = json.loads(request.content.decode())
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "x"}, "finish_reason": "stop"}]},
            )

        respx.post("http://localhost:11434/v1/chat/completions").mock(
            side_effect=_capture,
        )
        provider = OpenAICompatProvider(
            base_url="http://localhost:11434/v1",
            api_key="k",
            model="llama3.2",
        )
        async for _delta in provider.stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="",
        ):
            pass
        assert captured["json"].get("model") == "llama3.2"
        await provider.close()


class TestAnthropicProvider:
    @pytest.fixture
    def provider(self):
        return AnthropicProvider(api_key="test-key", model="claude-3-5-sonnet")

    @respx.mock
    async def test_text_streaming(self, provider):
        body = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "Hello there"}],
            "stop_reason": "end_turn",
        }

        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json=body,
                headers={"content-type": "application/json"},
            )
        )

        deltas = []
        async for delta in provider.stream(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system="",
        ):
            deltas.append(delta)

        text_deltas = [d for d in deltas if isinstance(d, DeltaText)]
        assert len(text_deltas) >= 1

    @respx.mock
    async def test_tool_call_streaming(self, provider):
        body = {
            "id": "msg_01",
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "tool_use",
                "id": "toolu_01",
                "name": "run_safe",
                "input": {"cmd": "df -h"},
            }],
            "stop_reason": "tool_use",
        }

        respx.post("https://api.anthropic.com/v1/messages").mock(
            return_value=httpx.Response(
                200,
                json=body,
                headers={"content-type": "application/json"},
            )
        )

        deltas = []
        async for delta in provider.stream(
            messages=[{"role": "user", "content": "check disk"}],
            tools=[],
            system="",
        ):
            deltas.append(delta)

        tool_deltas = [d for d in deltas if isinstance(d, DeltaToolCall)]
        assert len(tool_deltas) == 1
        assert tool_deltas[0].name == "run_safe"
        assert tool_deltas[0].call_id == "toolu_01"
