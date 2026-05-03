"""Summarize older tool-call turns into memory and strip them from API history."""

from __future__ import annotations

import json
from dataclasses import dataclass

from ..config import CONTEXT_COMPRESSION_CHAR_BUDGET
from ..providers.base import DeltaDone, DeltaReasoning, DeltaText, DeltaToolCall, Provider
from .tools import tool_descriptions_for_compression

# One-off summarization turn: compress tool traffic, keep facts relevant to the user's ask.
_COMPRESSION_SYSTEM = """\
You are a compression toolkit. You will be given a conversation history. 
Compress the given conversation (tool calls and tool outputs) by **extracting the related information to the user's query** in a compact manner.

## CRITICAL OUTPUT RULES
- Keep ONLY facts from tool call results relevant to the user's query.
- Be brief and concise (Omit shell noise, redundant errors, and verbatim logs).
- *Just output the facts/compressed content. No need for additional explanation.*
- *NO commentary or suggestions. Just the facts.*
- Don't add user intent or other information. Just the facts.
- The output should ba few bullets or a paragraph and can be understood without the original conversation.
- If the results are useful for diagnosing the problem, *include all information in the output* (for example, list of files, directories, etc.).
"""

@dataclass
class ToolCallCompressionMemory:
    """Accumulated summaries from compression runs (appended across the session)."""

    memory: str = ""

    def append_summary(self, summary: str) -> None:
        s = (summary or "").strip()
        if not s:
            return
        if self.memory:
            self.memory = f"{self.memory}\n\n{s}"
        else:
            self.memory = s

    def clear(self) -> None:
        self.memory = ""


def count_tool_messages(messages: list[dict]) -> int:
    return sum(1 for m in messages if m.get("role") == "tool")


def history_char_estimate(messages: list[dict]) -> int:
    """Rough context size in characters (content + serialized tool_calls)."""
    total = 0
    for m in messages:
        total += len(str(m.get("content") or ""))
        if m.get("role") == "assistant":
            tc = m.get("tool_calls")
            if tc:
                total += len(json.dumps(tc, ensure_ascii=False))
    return total


def web_search_in_prefix(prefix: list[dict]) -> bool:
    for m in prefix:
        if m.get("role") != "assistant":
            continue
        for tc in m.get("tool_calls") or []:
            fn = tc.get("function") or {}
            if fn.get("name") == "web_search":
                return True
    return False


def should_compress(prefix: list[dict], full_messages: list[dict]) -> bool:
    """Threshold: 4+ tool results, web_search in prefix with 3+ tools, or history char budget."""
    total_tools = count_tool_messages(full_messages)
    if total_tools >= 4:
        return True
    if total_tools >= 3 and web_search_in_prefix(prefix):
        return True
    if history_char_estimate(full_messages) > CONTEXT_COMPRESSION_CHAR_BUDGET:
        return True
    return False


def prefix_has_tool_rows_to_drop(prefix: list[dict]) -> bool:
    for m in prefix:
        if m.get("role") == "tool":
            return True
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return True
    return False


def _tool_round_end_exclusive(messages: list[dict], assistant_idx: int) -> int:
    end = assistant_idx + 1
    while end < len(messages) and messages[end].get("role") == "tool":
        end += 1
    return end


def _previous_assistant_with_tools(messages: list[dict], before_idx: int) -> int | None:
    for i in range(before_idx - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            return i
    return None


def split_for_compression(
    messages: list[dict],
) -> tuple[list[dict], list[dict]] | None:
    """Split into (prefix, suffix) where suffix is the last two assistant+tool rounds.

    If only one completed tool round exists, suffix is that single round.

    Returns None if there is no trailing tool round or it has no tool results yet.
    """
    if not messages:
        return None

    last_assistant_with_tools: int | None = None
    for i in range(len(messages) - 1, -1, -1):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            last_assistant_with_tools = i
            break

    if last_assistant_with_tools is None:
        return None

    end = _tool_round_end_exclusive(messages, last_assistant_with_tools)
    if end == last_assistant_with_tools + 1:
        return None

    prev = _previous_assistant_with_tools(messages, last_assistant_with_tools)
    suffix_start = prev if prev is not None else last_assistant_with_tools
    suffix = messages[suffix_start:end]
    prefix = messages[:suffix_start]
    return prefix, suffix


def _keep_message_from_prefix(m: dict) -> bool:
    """Retain user messages and assistant messages without tool_calls."""
    role = m.get("role")
    if role == "user":
        return True
    if role == "assistant":
        return not m.get("tool_calls")
    return False


def rewrite_prefix_drop_tools(prefix: list[dict]) -> list[dict]:
    return [m for m in prefix if _keep_message_from_prefix(m)]


def _format_argument_value(val: object) -> str:
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, (dict, list)):
        return json.dumps(val, ensure_ascii=False, separators=(",", ":"))
    return str(val).strip()


def _format_arguments_param_lines(arguments_json: str | None) -> str:
    """Format tool arguments as one ``parameter, value`` line per parameter."""
    raw = (arguments_json or "").strip()
    if not raw:
        return ""
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw
    if parsed is None:
        return ""
    if not isinstance(parsed, dict):
        return str(parsed).strip()
    lines: list[str] = []
    for key, val in parsed.items():
        if val is None:
            continue
        lines.append(f"{key}, {_format_argument_value(val)}")
    return "\n".join(lines)


def _format_single_tool_call_except_result(
    name: str,
    arguments_json: str | None,
    descriptions: dict[str, str],
) -> str:
    """Text for one tool: name, Params block, optional Descriptions (no ``Result``)."""
    parts: list[str] = [f"Tool call: {name}", "Params:"]
    param_block = _format_arguments_param_lines(arguments_json)
    if param_block:
        parts.append(param_block)
    desc = (descriptions.get(name) or "").strip()
    if desc:
        parts.append(f"Descriptions: {desc}")
    return "\n".join(parts)


def format_prefix_as_compression_markdown(
    messages: list[dict],
    *,
    tool_descriptions: dict[str, str] | None = None,
) -> str:
    """Render prefix messages as plain markdown text for a single ``user`` role payload.

    Providers such as local LLMs handle this better than raw ``assistant`` + ``tool`` messages.
    """
    descriptions = tool_descriptions if tool_descriptions is not None else tool_descriptions_for_compression()
    user_parts: list[str] = []
    assistant_plain_parts: list[str] = []
    tool_sections: list[str] = []

    i = 0
    n = len(messages)
    while i < n:
        m = messages[i]
        role = m.get("role")

        if role == "user":
            user_parts.append(str(m.get("content") or "").strip())
            i += 1
            continue

        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            plain = str(m.get("content") or "").strip()
            if plain and not tool_calls:
                assistant_plain_parts.append(plain)
                i += 1
                continue
            if not tool_calls:
                i += 1
                continue

            # Collect tool results following this assistant message (match by tool_call_id).
            i += 1
            tool_contents: dict[str, str] = {}
            while i < n and messages[i].get("role") == "tool":
                tid = messages[i].get("tool_call_id") or ""
                tool_contents[tid] = str(messages[i].get("content") or "")
                i += 1

            blocks: list[str] = []
            for tc in tool_calls:
                tid = tc.get("id") or ""
                fn = (tc.get("function") or {})
                name = fn.get("name") or "(unknown)"
                call_text = _format_single_tool_call_except_result(
                    name,
                    fn.get("arguments"),
                    descriptions,
                )
                result = tool_contents.get(tid, "(missing tool result)")
                blocks.append(
                    f"{call_text}\n\nResult:\n{result}",
                )
            if blocks:
                tool_sections.append("\n\n---\n\n".join(blocks))
            continue

        if role == "tool":
            # Orphan tool row (should not happen in well-formed history); skip.
            i += 1
            continue

        i += 1

    parts: list[str] = []
    users_joined = "\n\n".join(u for u in user_parts if u)
    if users_joined:
        parts.append(f"## User\n\n{users_joined}")

    assistant_joined = "\n\n".join(a for a in assistant_plain_parts if a)
    if assistant_joined:
        block = f"## Assistant\n\n{assistant_joined}"
        parts.append(block)

    if tool_sections:
        combined_tools = "\n\n---\n\n".join(tool_sections)
        parts.append(f"## Assistant (Tool calls with results)\n\n{combined_tools}")

    return "\n\n".join(parts).strip()


async def _collect_summary_stream(
    provider: Provider,
    prefix_messages: list[dict],
) -> str:
    body = format_prefix_as_compression_markdown(prefix_messages)
    payload = [{"role": "user", "content": body}]
    accumulated = ""
    async for delta in provider.stream(
        messages=payload,
        tools=[],
        system=_COMPRESSION_SYSTEM,
    ):
        if isinstance(delta, DeltaText):
            accumulated += delta.text
        elif isinstance(delta, DeltaReasoning):
            pass
        elif isinstance(delta, DeltaToolCall):
            pass
        elif isinstance(delta, DeltaDone):
            break
    return accumulated.strip()


async def maybe_compress_history(
    messages: list[dict],
    provider: Provider,
    memory: ToolCallCompressionMemory,
) -> None:
    """If thresholds match, summarize the prefix (minus last two tool rounds), append to memory, trim history.

    On provider/summary failure, skips compression so the chat can continue.
    Mutates ``messages`` in place.
    """
    split = split_for_compression(messages)
    if split is None:
        return

    prefix, suffix = split
    if not should_compress(prefix, messages):
        return
    if not prefix_has_tool_rows_to_drop(prefix):
        return

    try:
        summary = await _collect_summary_stream(provider, prefix)
    except Exception:
        # Fail soft: do not strip tool context if summarization fails.
        return

    memory.append_summary(summary)

    trimmed_prefix = rewrite_prefix_drop_tools(prefix)
    messages[:] = trimmed_prefix + suffix
