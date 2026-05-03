"""Message history management for the agent loop.

Maintains the conversation as a flat list of OpenAI-style message dicts.
Provides a token-budget-aware trimming method to prevent context overflow.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..config import MAX_HISTORY_CHARS, MAX_TERMINAL_USER_MESSAGE_CHARS

# Prepended to user messages that record manual side-terminal commands + output.
TERMINAL_USER_MESSAGE_PREFIX = "[Side terminal — I ran this command myself]"


def format_terminal_user_message(
    command: str,
    output: str,
    *,
    max_chars: int = MAX_TERMINAL_USER_MESSAGE_CHARS,
) -> str:
    """Format a manual terminal run as a single user-role message for the LLM."""
    body = (output or "").strip() or "(no output)"
    text = f"{TERMINAL_USER_MESSAGE_PREFIX}\n$ {command}\n\n{body}"
    if len(text) > max_chars:
        return text[: max_chars - 50].rstrip() + "\n... [truncated for context length]"
    return text


@dataclass
class MessageHistory:
    """Ordered list of role/content message dicts for the LLM."""

    messages: list[dict] = field(default_factory=list)

    def add_user(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content})

    def add_assistant_tool_call(self, tool_calls: list[dict]) -> None:
        # API convention: assistant messages with tool_calls must not carry parallel text.
        self.messages.append({
            "role": "assistant",
            "content": "",
            "tool_calls": tool_calls,
        })

    def add_tool_result(self, tool_call_id: str, content: str) -> None:
        self.messages.append({
            "role": "tool",
            "tool_call_id": tool_call_id,
            "content": content,
        })

    def as_list(self) -> list[dict]:
        """Return the trimmed message list ready for the provider."""
        return _trim(self.messages)

    def clear(self) -> None:
        self.messages.clear()


def _message_chars(msg: dict) -> int:
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    extra = sum(
        len(str(tc.get("function", {}).get("arguments", "")))
        for tc in tool_calls
    )
    return len(content) + extra


def _trim(messages: list[dict]) -> list[dict]:
    """Drop oldest non-system messages when history exceeds budget."""
    total = sum(_message_chars(m) for m in messages)
    if total <= MAX_HISTORY_CHARS:
        return list(messages)

    # Always keep the first user message for context
    trimmed = list(messages)
    keep_first = trimmed[:1]
    rest = trimmed[1:]

    while rest and total > MAX_HISTORY_CHARS:
        removed = rest.pop(0)
        total -= _message_chars(removed)

    return keep_first + rest
