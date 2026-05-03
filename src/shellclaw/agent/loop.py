"""The ReAct agentic loop.

AgentLoop.run(user_message) is an async generator that yields AgentEvent
instances.  The TUI consumes these events to update the UI in real time.

Flow:
  user message -> system prompt -> stream LLM -> tool call -> execute -> feed back
  repeat until the LLM gives a plain-text answer or max_iterations (tool calls) reached.
  ``max_iterations`` counts each tool execution since the last user message, so it stays
  correct when the model batches multiple tools in one reply or when history compression
  removes older tool rows. If the cap is hit, emit an error for the UI, then one final
  LLM call with tools disabled and an extra user message that is not stored in history.

The LLM proposes fixes via <proposed_actions> in its text output (parsed
by the chat widget), not via a dedicated tool.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import AsyncIterator

from ..config import AppConfig, provider_stream_log_path
from ..token_usage import split_trailing_token_usage
from ..providers import get_provider
from ..providers.base import DeltaDone, DeltaReasoning, DeltaText, DeltaToolCall, Provider
from .compress import ToolCallCompressionMemory, maybe_compress_history
from .context import MessageHistory
from .prompts import build_system_prompt
from .terminal_history import TerminalHistoryStore
from .tools import observe_tool_schemas, dispatch, format_disk_usage_command

# Do not mirror these into TerminalHistoryStore — they only read from it.
_SKIP_TERMINAL_LOG_TOOLS = frozenset({
    "terminal_history_summary",
    "terminal_history_fetch",
    "terminal_latest",
    "terminal_screen_snapshot",
    "stop_execution",
})

# Appended only to the API payload for the post-limit recovery turn; never stored
# in MessageHistory or shown as a user bubble in the chat widget.
_MAX_ITER_FORCE_ANSWER_USER = (
    "You can't run any more tools — just give your answer based on the conversation above."
)


# ---------------------------------------------------------------------------
# Event types yielded to the TUI
# ---------------------------------------------------------------------------

@dataclass
class EventThinking:
    """LLM is generating a text chunk (streaming)."""
    text: str


@dataclass
class EventReasoning:
    """LLM is streaming internal reasoning / thinking (not the final answer)."""
    text: str


@dataclass
class EventToolStart:
    """About to run a tool."""
    tool_name: str
    arguments: dict


@dataclass
class EventToolResult:
    """A tool finished and produced output."""
    tool_name: str
    command: str
    output: str


@dataclass
class EventToolOutput:
    """Incremental stdout line while a tool (e.g. ``run_safe``) is executing."""

    line: str


@dataclass
class EventDone:
    """The LLM finished its response with plain text (may contain <proposed_actions>)."""
    text: str
    total_tokens: int | None = None


@dataclass
class EventError:
    """Something went wrong."""
    message: str


def _assistant_body_and_tokens(raw: str) -> tuple[str, int | None]:
    """Strip trailing usage JSON for history/API; return body and optional total_tokens."""
    body, total = split_trailing_token_usage(raw)
    return body.strip(), total


AgentEvent = (
    EventThinking
    | EventReasoning
    | EventToolStart
    | EventToolOutput
    | EventToolResult
    | EventDone
    | EventError
)


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------

class AgentLoop:
    def __init__(
        self,
        config: AppConfig,
        *,
        terminal_log: TerminalHistoryStore | None = None,
    ) -> None:
        self._config = config
        self._terminal_log = terminal_log
        self._provider: Provider = get_provider(
            name=config.provider.name,
            model=config.provider.model,
            api_key=config.provider.api_key,
            base_url=config.provider.base_url,
            stream_log_path=provider_stream_log_path(),
            temperature=config.provider.temperature,
            num_ctx=config.provider.num_ctx,
        )
        self._history = MessageHistory()
        self._compression_memory = ToolCallCompressionMemory()

    async def run(
        self,
        user_message: str,
        distro_info: str = "",
        hardware_profile: dict | None = None,
        *,
        pty_runner: Callable[[str, int], Awaitable[tuple[str, int]]] | None = None,
        terminal_snapshot: Callable[[], dict[str, str]] | None = None,
        pty_stop: Callable[[], str] | None = None,
    ) -> AsyncIterator[AgentEvent]:
        """Run the ReAct loop and yield events."""
        adv = self._config.agent.advanced_toolset

        bundle = ""
        if self._terminal_log is not None:
            bundle = self._terminal_log.format_bundle_for_next_user_turn()
        if bundle:
            composed = f"{bundle}\n\n{user_message}"
        else:
            composed = user_message
        self._history.add_user(composed)

        max_iter = self._config.agent.max_iterations
        # Count each tool invocation since this user message (not LLM rounds). Resets
        # every run() so compression cannot "hide" past tools from the cap.
        tool_calls_executed = 0
        empty_replies = 0
        compress_ctx = self._config.agent.tool_context_compression
        # Failsafe if the model never settles (independent of tool-call budget).
        observe_passes = 0
        max_observe_passes = max(max_iter + 24, 48)

        while observe_passes < max_observe_passes:
            observe_passes += 1
            if tool_calls_executed >= max_iter:
                break
            if compress_ctx:
                await maybe_compress_history(
                    self._history.messages,
                    self._provider,
                    self._compression_memory,
                )
            system = build_system_prompt(
                distro_info=distro_info,
                hardware_profile=hardware_profile,
                user_message=user_message,
                advanced_toolset=adv,
                additional_context=(
                    self._compression_memory.memory if compress_ctx else ""
                ),
            )
            # Reply text persisted to MessageHistory; excludes reasoning-only stream chunks.
            accumulated_text = ""
            tool_was_called = False
            stream_total: int | None = None

            async for delta in self._provider.stream(
                messages=self._history.as_list(),
                tools=observe_tool_schemas(adv),
                system=system,
            ):
                if isinstance(delta, DeltaText):
                    accumulated_text += delta.text
                    yield EventThinking(text=delta.text)

                elif isinstance(delta, DeltaReasoning):
                    # Never merge into accumulated_text or MessageHistory — only the
                    # TUI reasoning pane consumes this; API history stays answer-only.
                    yield EventReasoning(text=delta.text)

                elif isinstance(delta, DeltaToolCall):
                    if tool_calls_executed >= max_iter:
                        break
                    tool_calls_executed += 1
                    tool_was_called = True
                    call_id = delta.call_id or f"call_{uuid.uuid4().hex[:8]}"

                    yield EventToolStart(tool_name=delta.name, arguments=delta.arguments)

                    line_queue: asyncio.Queue[str | None] = asyncio.Queue()

                    async def _stream_sink(line: str) -> None:
                        await line_queue.put(line)

                    async def _execute_tool() -> str:
                        try:
                            return await dispatch(
                                delta.name,
                                delta.arguments,
                                terminal_log=self._terminal_log,
                                stream_line=_stream_sink,
                                pty_runner=pty_runner,
                                terminal_snapshot=terminal_snapshot,
                                pty_stop=pty_stop,
                            )
                        finally:
                            await line_queue.put(None)

                    exec_task = asyncio.create_task(_execute_tool())
                    while True:
                        line = await line_queue.get()
                        if line is None:
                            break
                        yield EventToolOutput(line=line)
                    result = await exec_task
                    stripped = (result or "").strip()
                    if not stripped or stripped == "%":
                        result = (
                            "[Tool completed with no visible output. "
                            "Assume the command ran but produced nothing to show.]"
                        )
                    if (
                        self._terminal_log is not None
                        and delta.name not in _SKIP_TERMINAL_LOG_TOOLS
                    ):
                        self._terminal_log.append_tool(
                            delta.name,
                            delta.arguments,
                            _format_command(delta.name, delta.arguments),
                            result,
                        )
                    yield EventToolResult(
                        tool_name=delta.name,
                        command=_format_command(delta.name, delta.arguments),
                        output=result,
                    )

                    self._history.add_assistant_tool_call(
                        tool_calls=[{
                            "id": call_id,
                            "type": "function",
                            "function": {
                                "name": delta.name,
                                "arguments": json.dumps(delta.arguments),
                            },
                        }],
                    )
                    accumulated_text = ""
                    self._history.add_tool_result(tool_call_id=call_id, content=result)

                elif isinstance(delta, DeltaDone):
                    stream_total = delta.total_tokens
                    break

            # If a structured tool call ran, continue to let the LLM
            # reason about the result in the next iteration.
            if tool_was_called:
                empty_replies = 0
                continue

            # Got a real text answer — done.
            if accumulated_text.strip():
                body, tok_text = _assistant_body_and_tokens(accumulated_text)
                tok = stream_total if stream_total is not None else tok_text
                if body:
                    self._history.add_assistant(body)
                yield EventDone(text=body, total_tokens=tok)
                return

            # Empty response — retry up to 2 times before giving up.
            empty_replies += 1
            if empty_replies >= 2:
                yield EventDone(text="")
                return

        yield EventError(
            message=(
                f"Reached the maximum of {max_iter} tool calls."
            )
        )

        recovery_system = build_system_prompt(
            distro_info=distro_info,
            hardware_profile=hardware_profile,
            user_message=user_message,
            advanced_toolset=adv,
            additional_context=(
                self._compression_memory.memory if compress_ctx else ""
            ),
        )
        recovery_messages = [
            *self._history.as_list(),
            {"role": "user", "content": _MAX_ITER_FORCE_ANSWER_USER},
        ]
        accumulated_text = ""
        stream_total: int | None = None
        async for delta in self._provider.stream(
            messages=recovery_messages,
            tools=[],
            system=recovery_system,
        ):
            if isinstance(delta, DeltaText):
                accumulated_text += delta.text
                yield EventThinking(text=delta.text)
            elif isinstance(delta, DeltaReasoning):
                yield EventReasoning(text=delta.text)
            elif isinstance(delta, DeltaToolCall):
                # tools=[] — should not happen; never execute.
                pass
            elif isinstance(delta, DeltaDone):
                stream_total = delta.total_tokens
                break

        if accumulated_text.strip():
            body, tok_text = _assistant_body_and_tokens(accumulated_text)
            tok = stream_total if stream_total is not None else tok_text
            if body:
                self._history.add_assistant(body)
            yield EventDone(text=body, total_tokens=tok)
        else:
            fallback = (
                "I hit the iteration limit and could not produce a longer answer. "
                "Try a narrower question or increase max_iterations in settings."
            )
            self._history.add_assistant(fallback)
            yield EventDone(text=fallback, total_tokens=stream_total)

    async def explain_command(
        self,
        command: str,
        output: str = "",
        tldr_context: str = "",
        distro_info: str = "",
    ) -> AsyncIterator[str]:
        """Stream a contextual explanation of a command and its output."""
        system = build_system_prompt(
            distro_info=distro_info,
            advanced_toolset=self._config.agent.advanced_toolset,
        )
        explain_history = MessageHistory()

        parts = [
            "Give a very short, compact explanation of this shell command in plain English.\n\n"
            f"Command:\n{command}"
        ]
        if output:
            parts.append(f"Output (truncated context):\n{output}")
        if tldr_context:
            parts.append(f"Reference (tldr-pages — do not repeat verbatim):\n{tldr_context}")
        parts.append(
            "Constraints: 2–4 sentences total. Cover only what matters for this output and "
            "one practical risk if any. Skip flag-by-flag breakdown (shown elsewhere). "
            "No section headings; avoid bullet lists unless absolutely necessary (max 3 short items)."
        )

        explain_history.add_user("\n\n".join(parts))

        async for delta in self._provider.stream(
            messages=explain_history.as_list(),
            tools=[],
            system=system,
        ):
            if isinstance(delta, DeltaText):
                yield delta.text

    def clear_history(self) -> None:
        """Reset conversation history."""
        self._history.clear()
        self._compression_memory.clear()


def _format_command(tool_name: str, arguments: dict) -> str:
    """Produce a human-readable command string from a tool call."""
    match tool_name:
        case "run_safe":
            return arguments.get("cmd", "")
        case "read_file":
            return f"cat {arguments.get('path', '')}"
        case "list_dir":
            return f"List directory: {arguments.get('path', '')}"
        case "disk_usage":
            return f"Check disk usage: {arguments.get('path', '/')}"
        case "process_list":
            return f"List running programs"
        case "journal_logs":
            mode = arguments.get("mode", "")
            return f"Read system logs (mode={mode})"
        case "service_status":
            return f"Check service status: {arguments.get('unit', '')}"
        case "network_info":
            return "Check network information"
        case "get_distro_info":
            return "Check distro information"
        case "web_search":
            q = arguments.get("query", "")
            return f"Search the web: {q}"
        case "terminal_screen_snapshot":
            return "Capture live terminal screen"
        case "stop_execution":
            return "Stop / interrupt live terminal command (^C)"
        case _:
            return tool_name
