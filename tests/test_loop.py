"""Tests for the agent loop — mock LLM responses."""

import pytest
from unittest.mock import AsyncMock, patch

from shellclaw.agent.loop import (
    AgentLoop,
    EventDone,
    EventError,
    EventReasoning,
    EventToolResult,
    EventToolStart,
    EventThinking,
)
from shellclaw.agent.terminal_history import TerminalHistoryStore, USER_TERMINAL_BUNDLE_HEADER
from shellclaw.agent.tools import OBSERVE_TOOL_SCHEMAS_V0, observe_tool_schemas, dispatch
from shellclaw.config import AppConfig, ProviderConfig, AgentConfig
from shellclaw.providers.base import DeltaDone, DeltaReasoning, DeltaText, DeltaToolCall


def _make_config(max_iterations: int = 10) -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="ollama", model="test", api_key="test"),
        agent=AgentConfig(max_iterations=max_iterations),
    )


async def _collect(gen) -> list:
    return [item async for item in gen]


class MockProvider:
    """A mock provider that returns a pre-set sequence of delta sequences per call."""

    def __init__(self, responses: list[list]):
        self._responses = iter(responses)

    async def stream(self, messages, tools, system):
        try:
            deltas = next(self._responses)
        except StopIteration:
            yield DeltaDone()
            return

        for delta in deltas:
            yield delta


class TestAgentLoop:
    async def test_bundle_user_terminal_in_single_user_message(self):
        """Manual terminal runs are bundled into the one user message for that chat turn."""
        config = _make_config()
        log = TerminalHistoryStore()
        log.append_user("uname -a", "Linux test")
        loop = AgentLoop(config, terminal_log=log)
        loop._provider = MockProvider([
            [
                DeltaText(text="Answer."),
                DeltaDone(),
            ]
        ])

        await _collect(loop.run("what is this?"))

        assert len(loop._history.messages) == 2
        user_msg = loop._history.messages[0]
        assert user_msg["role"] == "user"
        content = user_msg["content"]
        assert USER_TERMINAL_BUNDLE_HEADER in content
        assert "$ uname -a" in content
        assert "Linux test" in content
        assert "what is this?" in content

    async def test_second_chat_turn_does_not_rebundle_same_terminal_commands(self):
        """Bundled terminal block is only for user commands since the last chat submit."""
        config = _make_config()
        log = TerminalHistoryStore()
        log.append_user("echo hi", "hi")
        loop = AgentLoop(config, terminal_log=log)
        loop._provider = MockProvider([
            [
                DeltaText(text="first."),
                DeltaDone(),
            ],
            [
                DeltaText(text="second."),
                DeltaDone(),
            ],
        ])

        await _collect(loop.run("first question"))
        await _collect(loop.run("second question"))

        users = [m for m in loop._history.messages if m["role"] == "user"]
        assert len(users) == 2
        assert "echo hi" in users[0]["content"]
        assert "echo hi" not in users[1]["content"]
        assert users[1]["content"] == "second question"

    async def test_tool_run_recorded_in_terminal_log(self):
        """Agent tool execution is appended to TerminalHistoryStore (except history meta-tools)."""
        config = _make_config()
        log = TerminalHistoryStore()

        with patch("shellclaw.agent.loop.dispatch", new=AsyncMock(return_value="Filesystem info here")):
            mock_provider = MockProvider([
                [
                    DeltaToolCall(
                        call_id="c1",
                        name="run_safe",
                        arguments={"cmd": "df -h"},
                    ),
                    DeltaDone(finish_reason="tool_calls"),
                ],
                [
                    DeltaText(text="Done."),
                    DeltaDone(),
                ],
            ])

            loop = AgentLoop(config, terminal_log=log)
            loop._provider = mock_provider

            await _collect(loop.run("check disk"))

        tool_entries = [e for e in log._entries if e.kind == "tool"]
        assert len(tool_entries) == 1
        assert tool_entries[0].description.startswith("run_safe:")
        assert "Filesystem info here" in tool_entries[0].output

    async def test_plain_text_answer(self):
        """LLM gives a plain-text answer with no tool calls."""
        mock_provider = MockProvider([
            [
                DeltaText(text="Your disk is almost full. "),
                DeltaText(text="Here is what I suggest."),
                DeltaDone(),
            ]
        ])

        config = _make_config()
        loop = AgentLoop(config)
        loop._provider = mock_provider

        events = await _collect(loop.run("my disk is full"))

        done_events = [e for e in events if isinstance(e, EventDone)]
        assert len(done_events) == 1
        assert "disk" in done_events[0].text.lower()

    async def test_tool_call_then_answer(self):
        """LLM runs one tool then gives a plain-text answer."""
        with patch("shellclaw.agent.loop.dispatch", new=AsyncMock(return_value="Filesystem info here")):
            mock_provider = MockProvider([
                [
                    DeltaToolCall(
                        call_id="c1",
                        name="run_safe",
                        arguments={"cmd": "df -h"},
                    ),
                    DeltaDone(finish_reason="tool_calls"),
                ],
                [
                    DeltaText(text="Your disk has plenty of space."),
                    DeltaDone(),
                ],
            ])

            config = _make_config()
            loop = AgentLoop(config)
            loop._provider = mock_provider

            events = await _collect(loop.run("check disk usage"))

        tool_starts = [e for e in events if isinstance(e, EventToolStart)]
        tool_results = [e for e in events if isinstance(e, EventToolResult)]
        done_events = [e for e in events if isinstance(e, EventDone)]

        assert len(tool_starts) == 1
        assert tool_starts[0].tool_name == "run_safe"
        assert len(tool_results) == 1
        assert len(done_events) == 1

    async def test_max_iterations_counts_each_tool_in_one_response(self):
        """max_iterations limits individual tool executions, not LLM rounds (batch-safe)."""
        batched = [
            DeltaToolCall(call_id="a", name="run_safe", arguments={"cmd": "echo a"}),
            DeltaToolCall(call_id="b", name="run_safe", arguments={"cmd": "echo b"}),
            DeltaToolCall(call_id="c", name="run_safe", arguments={"cmd": "echo c"}),
            DeltaDone(finish_reason="tool_calls"),
        ]
        recovery = [
            DeltaText(text="Stopped early after two tools."),
            DeltaDone(),
        ]
        with patch("shellclaw.agent.loop.dispatch", new=AsyncMock(return_value="out")) as mock_dispatch:
            mock_provider = MockProvider([batched, recovery])
            config = _make_config(max_iterations=2)
            loop = AgentLoop(config)
            loop._provider = mock_provider

            events = await _collect(loop.run("batch tools"))

        assert mock_dispatch.call_count == 2
        tool_starts = [e for e in events if isinstance(e, EventToolStart)]
        assert len(tool_starts) == 2
        error_events = [e for e in events if isinstance(e, EventError)]
        assert len(error_events) == 1
        assert "tool calls" in error_events[0].message.lower()

    async def test_max_iterations_yields_error_then_final_answer(self):
        """At max_iterations tool budget, emit EventError, then one tool-free turn and EventDone."""
        # MockProvider returns one sequence per stream() call: three single-tool rounds,
        # then the recovery turn must be the *next* sequence.
        tool_rounds = [
            [
                DeltaToolCall(call_id=f"c{i}", name="run_safe", arguments={"cmd": "df -h"}),
                DeltaDone(finish_reason="tool_calls"),
            ]
            for i in range(3)
        ]
        recovery = [
            DeltaText(text="Based on the data above, your disk looks fine."),
            DeltaDone(),
        ]
        responses = tool_rounds + [recovery]

        with patch("shellclaw.agent.loop.dispatch", new=AsyncMock(return_value="output")):
            mock_provider = MockProvider(responses)
            config = _make_config(max_iterations=3)
            loop = AgentLoop(config)
            loop._provider = mock_provider

            events = await _collect(loop.run("endless diagnosis"))

        error_events = [e for e in events if isinstance(e, EventError)]
        assert len(error_events) == 1
        assert "maximum" in error_events[0].message.lower()

        done_events = [e for e in events if isinstance(e, EventDone)]
        assert len(done_events) == 1
        assert "disk" in done_events[0].text.lower()

        # Hidden recovery user line must not be persisted in agent history.
        user_contents = [
            m["content"]
            for m in loop._history.messages
            if m.get("role") == "user"
        ]
        assert not any("can't run any more tools" in c for c in user_contents)

    async def test_max_iterations_recovery_passes_empty_tools(self):
        """The post-limit recovery request disables tools."""
        tool_round = [
            DeltaToolCall(call_id="c0", name="run_safe", arguments={"cmd": "df -h"}),
            DeltaDone(finish_reason="tool_calls"),
        ]
        recovery = [DeltaText(text="Done."), DeltaDone()]
        call_args: list[dict] = []

        class SpyProvider:
            async def stream(self, messages, tools, system):
                call_args.append({"tools": tools, "messages": messages})
                if len(call_args) == 1:
                    for d in tool_round:
                        yield d
                else:
                    for d in recovery:
                        yield d

        with patch("shellclaw.agent.loop.dispatch", new=AsyncMock(return_value="out")):
            config = _make_config(max_iterations=1)
            loop = AgentLoop(config)
            loop._provider = SpyProvider()

            await _collect(loop.run("x"))

        assert len(call_args) == 2
        assert call_args[0]["tools"] == observe_tool_schemas(False)
        assert call_args[1]["tools"] == []
        last_user = call_args[1]["messages"][-1]
        assert last_user["role"] == "user"
        assert "can't run any more tools" in last_user["content"].lower()

    async def test_text_thinking_emitted(self):
        """Text deltas are emitted as EventThinking."""
        mock_provider = MockProvider([
            [
                DeltaText(text="Let me check your disk."),
                DeltaDone(),
            ]
        ])

        config = _make_config()
        loop = AgentLoop(config)
        loop._provider = mock_provider

        events = await _collect(loop.run("hi"))

        thinking = [e for e in events if isinstance(e, EventThinking)]
        assert any("Let me check" in e.text for e in thinking)

    async def test_reasoning_deltas_emitted(self):
        """Reasoning chunks are forwarded as EventReasoning without mixing into the answer."""
        mock_provider = MockProvider([
            [
                DeltaReasoning(text="First I will "),
                DeltaReasoning(text="think."),
                DeltaText(text="Done."),
                DeltaDone(),
            ]
        ])

        config = _make_config()
        loop = AgentLoop(config)
        loop._provider = mock_provider

        events = await _collect(loop.run("hi"))

        reasoning = [e for e in events if isinstance(e, EventReasoning)]
        assert "".join(e.text for e in reasoning) == "First I will think."

        done = [e for e in events if isinstance(e, EventDone)]
        assert done and done[0].text == "Done."

    async def test_observe_loop_passes_tool_schemas_from_config(self):
        """AgentLoop.run sends observe_tool_schemas(advanced_toolset) to the provider."""
        call_args_list = []

        class SpyProvider:
            async def stream(self, messages, tools, system):
                call_args_list.append({"tools": tools})
                yield DeltaText(text="All good.")
                yield DeltaDone()

        config = _make_config()
        loop = AgentLoop(config)
        loop._provider = SpyProvider()

        await _collect(loop.run("hello"))

        assert call_args_list[0]["tools"] == observe_tool_schemas(False)

        config2 = AppConfig(
            provider=ProviderConfig(name="ollama", model="test", api_key="test"),
            agent=AgentConfig(max_iterations=10, advanced_toolset=True),
        )
        loop2 = AgentLoop(config2)
        loop2._provider = SpyProvider()
        await _collect(loop2.run("hello"))
        assert call_args_list[1]["tools"] == OBSERVE_TOOL_SCHEMAS_V0


class TestTerminalHistoryTools:
    async def test_dispatch_terminal_history_without_log_returns_error(self):
        out = await dispatch("terminal_history_summary", {})
        assert "not available" in out.lower()

    async def test_dispatch_terminal_history_fetch_with_log(self):
        log = TerminalHistoryStore()
        log.append_user("ls", "a\nb")
        out = await dispatch("terminal_history_fetch", {"id": 1}, terminal_log=log)
        assert "ls" in out
        assert "a" in out

    async def test_meta_history_tools_not_appended_to_store(self):
        """terminal_history_* calls must not create recursive log entries."""
        log = TerminalHistoryStore()
        log.append_user("x", "y")
        mock_provider = MockProvider([
            [
                DeltaToolCall(
                    call_id="h1",
                    name="terminal_history_summary",
                    arguments={},
                ),
                DeltaDone(finish_reason="tool_calls"),
            ],
            [
                DeltaText(text="ok."),
                DeltaDone(),
            ],
        ])
        loop = AgentLoop(_make_config(), terminal_log=log)
        loop._provider = mock_provider
        await _collect(loop.run("q"))
        kinds = [e.kind for e in log._entries]
        assert kinds.count("tool") == 0
        assert kinds.count("user") == 1


class TestTerminalScreenSnapshot:
    async def test_dispatch_without_callback(self):
        out = await dispatch("terminal_screen_snapshot", {})
        assert "not available" in out.lower()

    async def test_dispatch_with_callback(self):
        def snap() -> dict[str, str]:
            return {"mode": "interactive", "content": "cpu  1%  idle"}

        out = await dispatch("terminal_screen_snapshot", {}, terminal_snapshot=snap)
        assert "interactive" in out
        assert "cpu  1%  idle" in out

    async def test_snapshot_tool_not_logged_in_terminal_store(self):
        log = TerminalHistoryStore()
        log.append_user("x", "y")
        mock_provider = MockProvider(
            [
                [
                    DeltaToolCall(
                        call_id="s1",
                        name="terminal_screen_snapshot",
                        arguments={},
                    ),
                    DeltaDone(finish_reason="tool_calls"),
                ],
                [
                    DeltaText(text="done."),
                    DeltaDone(),
                ],
            ]
        )
        loop = AgentLoop(_make_config(), terminal_log=log)
        loop._provider = mock_provider

        def snap() -> dict[str, str]:
            return {"mode": "normal", "content": "z"}

        await _collect(loop.run("q", terminal_snapshot=snap))
        assert all(e.kind != "tool" for e in log._entries)


class TestStopExecution:
    async def test_dispatch_without_callback(self):
        out = await dispatch("stop_execution", {})
        assert "not available" in out.lower()

    async def test_dispatch_with_callback(self):
        def stop() -> str:
            return "Sent interrupt (^C) to the live terminal foreground command (same as the Stop button)."

        out = await dispatch("stop_execution", {}, pty_stop=stop)
        assert "Sent interrupt" in out

    async def test_stop_tool_not_logged_in_terminal_store(self):
        log = TerminalHistoryStore()
        log.append_user("x", "y")
        mock_provider = MockProvider(
            [
                [
                    DeltaToolCall(
                        call_id="st1",
                        name="stop_execution",
                        arguments={},
                    ),
                    DeltaDone(finish_reason="tool_calls"),
                ],
                [
                    DeltaText(text="done."),
                    DeltaDone(),
                ],
            ]
        )
        loop = AgentLoop(_make_config(), terminal_log=log)
        loop._provider = mock_provider

        def stop() -> str:
            return "ok"

        await _collect(loop.run("q", pty_stop=stop))
        assert all(e.kind != "tool" for e in log._entries)
