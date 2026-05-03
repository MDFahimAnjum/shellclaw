"""Tests for tool-call context compression."""

import json
import pytest

from shellclaw.agent.compress import (
    ToolCallCompressionMemory,
    count_tool_messages,
    format_prefix_as_compression_markdown,
    history_char_estimate,
    maybe_compress_history,
    prefix_has_tool_rows_to_drop,
    rewrite_prefix_drop_tools,
    should_compress,
    split_for_compression,
    web_search_in_prefix,
)
from shellclaw.config import CONTEXT_COMPRESSION_CHAR_BUDGET
from shellclaw.agent.loop import AgentLoop
from shellclaw.agent.prompts import build_system_prompt
from shellclaw.config import AgentConfig, AppConfig, ProviderConfig
from shellclaw.providers.base import DeltaDone, DeltaText


def _assistant_tool(name: str, call_id: str, args: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [{
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)},
        }],
    }


def _tool(call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def _user(text: str) -> dict:
    return {"role": "user", "content": text}


class MockProvider:
    """Yields preset delta sequences per stream call."""

    def __init__(self, responses: list[list]):
        self._responses = iter(responses)
        self.stream_calls: list[tuple[list, list, str]] = []

    async def stream(self, messages, tools, system):
        self.stream_calls.append((messages, tools, system))
        try:
            deltas = next(self._responses)
        except StopIteration:
            yield DeltaDone()
            return
        for delta in deltas:
            yield delta


def _four_round_history() -> list[dict]:
    """user + run_safe x3 + read_file (4th) — matches one user and four tool rounds."""
    return [
        _user("What uses disk?"),
        _assistant_tool("run_safe", "c1", {"cmd": "df -h"}),
        _tool("c1", "out1"),
        _assistant_tool("run_safe", "c2", {"cmd": "du -sh /"}),
        _tool("c2", "out2"),
        _assistant_tool("run_safe", "c3", {"cmd": "ls"}),
        _tool("c3", "out3"),
        _assistant_tool("read_file", "c4", {"path": "/etc/os-release"}),
        _tool("c4", "NAME=Linux"),
    ]


class TestSplitForCompression:
    def test_four_rounds_keeps_last_two_assistant_and_tool_rounds(self):
        msgs = _four_round_history()
        split = split_for_compression(msgs)
        assert split is not None
        prefix, suffix = split
        # Suffix is the last two rounds: run_safe c3 + read_file c4.
        assert prefix == msgs[:5]
        assert suffix == msgs[5:]
        assert len(suffix) == 4
        assert suffix[0]["tool_calls"][0]["function"]["name"] == "run_safe"
        assert suffix[2]["tool_calls"][0]["function"]["name"] == "read_file"

    def test_no_tools_returns_none(self):
        assert split_for_compression([_user("hi")]) is None

    def test_assistant_tools_without_results_returns_none(self):
        msgs = [_user("x"), _assistant_tool("run_safe", "c1", {"cmd": "true"})]
        assert split_for_compression(msgs) is None


class TestShouldCompress:
    def test_four_tools_triggers(self):
        msgs = _four_round_history()
        sp = split_for_compression(msgs)
        assert sp is not None
        prefix, _ = sp
        assert should_compress(prefix, msgs) is True

    def test_three_tools_does_not_trigger_without_budget_or_web_search(self):
        msgs = _four_round_history()[:7]
        sp = split_for_compression(msgs)
        assert sp is not None
        prefix, _ = sp
        assert count_tool_messages(msgs) == 3
        assert should_compress(prefix, msgs) is False

    def test_char_budget_triggers(self):
        pad = "x" * (CONTEXT_COMPRESSION_CHAR_BUDGET + 50)
        msgs = _four_round_history()
        msgs[2] = _tool("c1", pad)
        sp = split_for_compression(msgs)
        assert sp is not None
        prefix, _ = sp
        assert history_char_estimate(msgs) > CONTEXT_COMPRESSION_CHAR_BUDGET
        assert should_compress(prefix, msgs) is True

    def test_web_search_in_prefix_three_total_tools(self):
        msgs = [
            _user("q"),
            _assistant_tool("web_search", "w1", {"query": "foo"}),
            _tool("w1", "results..."),
            _assistant_tool("get_distro_info", "g1", {}),
            _tool("g1", "Ubuntu"),
            _assistant_tool("run_safe", "r1", {"cmd": "true"}),
            _tool("r1", "ok"),
        ]
        sp = split_for_compression(msgs)
        assert sp is not None
        prefix, _ = sp
        assert web_search_in_prefix(prefix) is True
        assert count_tool_messages(msgs) == 3
        assert should_compress(prefix, msgs) is True


class TestFormatCompressionMarkdown:
    def test_single_user_and_two_tool_rounds(self):
        msgs = [
            _user("find conda size"),
            _assistant_tool("list_dir", "call_a", {"path": "/root"}),
            _tool("call_a", "permission denied"),
            _assistant_tool("list_dir", "call_b", {"path": "/home"}),
            _tool("call_b", "fahim\nlost+found"),
        ]
        text = format_prefix_as_compression_markdown(msgs)
        assert "## User" in text
        assert "find conda size" in text
        assert "## Assistant (Tool calls with results)" in text
        assert "Tool call: list_dir" in text
        assert "Params:" in text
        assert "path, /root" in text
        assert "path, /home" in text
        assert "Descriptions:" in text
        assert "Result:" in text
        assert "---" in text
        assert "permission denied" in text


class TestRewritePrefix:
    def test_drops_tool_messages_keeps_user_and_plain_assistant(self):
        prefix = [
            _user("hello"),
            {"role": "assistant", "content": "Thinking."},
            _assistant_tool("run_safe", "c1", {"cmd": "true"}),
            _tool("c1", "ok"),
        ]
        assert prefix_has_tool_rows_to_drop(prefix) is True
        kept = rewrite_prefix_drop_tools(prefix)
        assert kept == [
            _user("hello"),
            {"role": "assistant", "content": "Thinking."},
        ]


class TestMaybeCompressHistory:
    @pytest.mark.asyncio
    async def test_summarizes_and_trims(self):
        msgs = _four_round_history()
        mem = ToolCallCompressionMemory()
        prov = MockProvider([
            [
                DeltaText(text="Earlier tools checked disk; last round kept."),
                DeltaDone(),
            ]
        ])

        await maybe_compress_history(msgs, prov, mem)

        assert len(prov.stream_calls) == 1
        comp_messages, comp_tools, _comp_sys = prov.stream_calls[0]
        assert comp_tools == []
        assert len(comp_messages) == 1
        assert comp_messages[0]["role"] == "user"
        body = comp_messages[0]["content"]
        assert "## User" in body
        assert "## Assistant (Tool calls with results)" in body
        assert "Tool call:" in body
        assert "Params:" in body
        assert "Descriptions:" in body
        assert mem.memory == "Earlier tools checked disk; last round kept."
        assert count_tool_messages(msgs) == 2
        assert msgs[0]["role"] == "user"
        assert msgs[1]["tool_calls"][0]["function"]["name"] == "run_safe"
        assert msgs[3]["tool_calls"][0]["function"]["name"] == "read_file"

    @pytest.mark.asyncio
    async def test_provider_exception_skips_mutation(self):
        msgs = _four_round_history()
        orig = json.dumps(msgs)
        mem = ToolCallCompressionMemory()

        class BadProvider:
            async def stream(self, messages, tools, system):
                raise RuntimeError("network")
                yield DeltaDone()  # Makes this an async generator; unreachable.

        await maybe_compress_history(msgs, BadProvider(), mem)
        assert msgs == json.loads(orig)
        assert mem.memory == ""


def test_build_system_prompt_additional_context_section():
    text = build_system_prompt(additional_context="Remember: disk was ok.")
    assert (
        "\n\n## Additional Context [from past tool calls and results]\n Following are results from your past tool calls. You can use this information to help you answer the user's question. \nRemember: disk was ok." in text
    )


def test_clear_history_clears_compression_memory():
    cfg = AppConfig(
        provider=ProviderConfig(name="ollama", model="test", api_key="test"),
        agent=AgentConfig(max_iterations=10),
    )
    loop = AgentLoop(cfg)
    loop._compression_memory.memory = "stale"
    loop.clear_history()
    assert loop._compression_memory.memory == ""
