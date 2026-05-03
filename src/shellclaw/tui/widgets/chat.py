"""Conversation panel widget.

Renders user messages, LLM plain-English responses, and inline
action blocks parsed from fenced shell regions (``bash``, ``zsh``, ``sh``, …):

When the fence body strictly alternates a ``#`` description line and a command
line, each pair becomes an :class:`_ActionBlock`; otherwise the whole fence stays
in the Markdown bubble unchanged.

Supports live-streaming: begin_stream() creates a bubble that
stream_token() updates token by token.  On finalize_stream(),
the raw text is re-rendered through add_assistant() which does
the parsing.
"""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.widgets import Markdown, Static

from ...utils import StreamWaitBubble
from ...token_usage import format_tokens_compact, split_trailing_token_usage


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------

# Markdown fence language tags treated as structured command proposals.
_SHELL_FENCE_LANG_ALT = (
    r"bash|zsh|sh|shell|fish|ksh|dash|ash|tcsh|csh|powershell|pwsh|cmd"
)

_SHELL_ACTION_FENCE_RE = re.compile(
    rf"```(?:{_SHELL_FENCE_LANG_ALT})\s*\n(?P<body>.*?)\n\s*```",
    re.DOTALL | re.IGNORECASE,
)

def _strip_trailing_tool_artifacts(text: str) -> str:
    """Remove a trailing <tool_call>...</tool_call> block from streamed text."""
    s = text.rstrip()
    end_tag = s.rfind("</tool_call>")
    if end_tag == -1:
        return s
    start_tag = s.rfind("<tool_call>", 0, end_tag)
    if start_tag == -1:
        return s
    return s[:start_tag].rstrip()


def _body_is_desc_command_pairs(body: str) -> bool:
    """True if non-empty lines strictly alternate ``#`` description then command."""
    lines = [ln.strip() for ln in body.strip().splitlines() if ln.strip()]
    if not lines:
        return False
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("#"):
            return False
        desc = line.lstrip("#").strip()
        if not desc:
            return False
        if i + 1 >= len(lines):
            return False
        cmd = lines[i + 1]
        if cmd.startswith("#"):
            return False
        if not cmd.strip():
            return False
        i += 2
    return True


def _parse_desc_command_fence(body: str) -> list[tuple[str, str]]:
    """Extract (command, description) pairs from a structured fence body.

    Each pair is: a line starting with ``#`` (description, rest of line after
    ``#``), then the next non-empty non-comment line (command).
    """
    actions: list[tuple[str, str]] = []
    pending_desc: str | None = None
    for raw in body.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            pending_desc = line.lstrip("#").strip() or None
            continue
        desc = (pending_desc or "").strip()
        actions.append((line, desc))
        pending_desc = None
    return actions


# ---------------------------------------------------------------------------
# Messages posted by action buttons
# ---------------------------------------------------------------------------

class ExecuteAction(Message):
    """User clicked Execute on a proposed command."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command


class ExplainAction(Message):
    """User clicked Explain on a proposed command."""

    def __init__(self, command: str, description: str) -> None:
        super().__init__()
        self.command = command
        self.description = description


class CopyToTerminal(Message):
    """User clicked Copy-to-terminal — paste into the terminal input."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command


# ---------------------------------------------------------------------------
# Bubble widgets
# ---------------------------------------------------------------------------

class _UserBubble(Static):
    DEFAULT_CSS = """
    _UserBubble {
        background: $primary-darken-2;
        color: $text;
        padding: 1 2;
        margin: 1 1 0 1;
        text-style: bold;
    }
    """


class _AssistantBubble(Markdown):
    DEFAULT_CSS = """
    _AssistantBubble {
        padding: 1 2;
        margin: 0 1 0 1;
        color: $text;
        background: $surface;
    }
    """


class _ErrorBubble(Static):
    DEFAULT_CSS = """
    _ErrorBubble {
        background: $error-darken-2;
        color: $text;
        padding: 1 2;
        margin: 1 1;
        text-style: bold;
    }
    """


# ---------------------------------------------------------------------------
# Clickable button labels  — plain colored text, no background box
# ---------------------------------------------------------------------------

class _ExecuteLabel(Static):
    DEFAULT_CSS = "_ExecuteLabel { color: $success; width: auto; height: 1; }"

    def __init__(self, command: str) -> None:
        super().__init__("▶ run")
        self._command = command

    def on_click(self) -> None:
        self.post_message(ExecuteAction(self._command))


class _ExplainLabel(Static):
    DEFAULT_CSS = "_ExplainLabel { color: $primary-lighten-2; width: auto; height: 1; padding: 0 2; }"

    def __init__(self, command: str, description: str = "") -> None:
        super().__init__("? explain")
        self._command = command
        self._description = description

    def on_click(self) -> None:
        self.post_message(ExplainAction(self._command, self._description))


class _CopyLabel(Static):
    DEFAULT_CSS = "_CopyLabel { color: $warning; width: auto; height: 1; }"

    def __init__(self, command: str) -> None:
        super().__init__("⎘ copy")
        self._command = command

    def on_click(self) -> None:
        self.post_message(CopyToTerminal(self._command))


# ---------------------------------------------------------------------------
# Structured blocks
# ---------------------------------------------------------------------------

class _ActionBlock(Vertical):
    """One proposed command parsed from a structured shell fence, with buttons."""

    DEFAULT_CSS = """
    _ActionBlock {
        background: $surface-darken-1;
        border: solid $primary-darken-2;
        margin: 1 1 0 1;
        padding: 1 2;
        height: auto;
    }
    .action-cmd {
        color: $success;
        text-style: bold;
    }
    .action-desc {
        color: $text-muted;
    }
    .action-buttons {
        height: 1;
        margin-top: 1;
    }
    """

    def __init__(self, command: str, description: str) -> None:
        super().__init__()
        self._command = command
        self._description = description

    def compose(self) -> ComposeResult:
        yield Static(f"$ {self._command}", classes="action-cmd")
        yield Static(self._description, classes="action-desc")
        with Horizontal(classes="action-buttons"):
            yield _ExecuteLabel(self._command)
            yield _ExplainLabel(self._command, self._description)
            yield _CopyLabel(self._command)


class _ToolRunSegment(Vertical):
    """One assistant-executed command (output stays in the terminal panel only)."""

    DEFAULT_CSS = """
    _ToolRunSegment {
        border-left: outer $accent;
        background: $surface-darken-1;
        margin: 0 1 1 1;
        padding: 0 1 1 1;
        height: auto;
    }
    .tool-run-label {
        color: $text-muted;
        text-style: italic;
        padding-top: 1;
    }
    .tool-run-cmd {
        color: $success;
        text-style: bold;
    }
    .tool-run-hint {
        color: $text-muted;
        padding-top: 0;
    }
    """

    def __init__(self, command: str) -> None:
        super().__init__()
        self._command = command

    def compose(self) -> ComposeResult:
        yield Static("Executing:", classes="tool-run-label", markup=False)
        yield Static(f"$ {self._command}", classes="tool-run-cmd", markup=False)
        #yield Static("Full output → Terminal panel", classes="tool-run-hint", markup=False)


# ---------------------------------------------------------------------------
# Main chat widget
# ---------------------------------------------------------------------------

class ChatWidget(ScrollableContainer):
    """Left panel — shows the conversation in plain English."""

    DEFAULT_CSS = """
    ChatWidget {
        border-title-align: left;
        overflow-y: auto;
    }
    """

    BORDER_TITLE = "Conversation"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._live_bubble: _AssistantBubble | None = None
        self._live_text: str = ""
        self._wait_bubble: StreamWaitBubble | None = None
        self._session_token_total: int = 0

    def compose(self) -> ComposeResult:
        return iter([])

    def set_conversation_tokens(self, total: int | None) -> None:
        """Show token usage in the panel border title (API or parsed from model text)."""
        if total is None:
            return
        self._session_token_total += total
        ls = format_tokens_compact(total)
        ts = format_tokens_compact(self._session_token_total)
        self.border_title = f"Conversation — Token (last: {ls} Total: {ts})"

    def add_user(self, text: str) -> None:
        self._finalize_live()
        bubble = _UserBubble(f"You: {text}")
        self.mount(bubble)
        self.scroll_end(animate=False)

    def add_assistant(self, text: str, *, total_tokens: int | None = None) -> None:
        """Render assistant text, parsing structured shell fences where applicable."""
        self._finalize_live()
        body, t_auto = split_trailing_token_usage(text)
        total = total_tokens if total_tokens is not None else t_auto
        body = body.strip()
        if not body and total is None:
            return
        if body:
            self._mount_parsed(body)
        self.set_conversation_tokens(total)
        self.scroll_end(animate=False)

    def add_error(self, text: str) -> None:
        self._finalize_live()
        bubble = _ErrorBubble(f"Error: {text}")
        self.mount(bubble)
        self.scroll_end(animate=False)

    def add_tool_run(self, command: str) -> None:
        """Append a compact tool-call line after an assistant tool call (output is in Terminal)."""
        self._finalize_live()
        self.mount(_ToolRunSegment(command))
        self.scroll_end(animate=False)

    def clear(self) -> None:
        """Remove all bubbles and reset streaming state."""
        self._live_bubble = None
        self._live_text = ""
        self._wait_bubble = None
        self.border_title = "Conversation"
        self._session_token_total = 0
        self.remove_children()

    def begin_stream_wait(self) -> None:
        """Show a waiting line until the first streamed model chunk (or end_stream_wait)."""
        if self._wait_bubble is not None or self._live_bubble is not None:
            return
        bubble = StreamWaitBubble()
        self._wait_bubble = bubble
        self.mount(bubble)
        self.scroll_end(animate=False)

    def end_stream_wait(self) -> None:
        if self._wait_bubble is None:
            return
        self._wait_bubble.remove()
        self._wait_bubble = None

    # --- Streaming API ---

    def begin_stream(self) -> None:
        self.end_stream_wait()
        self._finalize_live()
        self._live_text = ""
        self._live_bubble = _AssistantBubble("")
        self.mount(self._live_bubble)
        self.scroll_end(animate=False)

    def stream_token(self, token: str) -> None:
        if self._live_bubble is None:
            self.begin_stream()
        self._live_text += token
        self._live_bubble.update(self._live_text)
        self.scroll_end(animate=False)

    def finalize_stream(self, *, total_tokens_hint: int | None = None) -> str:
        """Close the live bubble and re-render through parsing pipeline."""
        text = self._live_text
        if self._live_bubble is not None:
            self._live_bubble.remove()
            self._live_bubble = None
        self._live_text = ""
        cleaned = _strip_trailing_tool_artifacts(text)
        body, total_text = split_trailing_token_usage(cleaned)
        body = body.strip()
        total = total_tokens_hint if total_tokens_hint is not None else total_text
        if body:
            self._mount_parsed(body)
        self.set_conversation_tokens(total)
        if body or total is not None:
            self.scroll_end(animate=False)
        return body

    def cancel_stream(self) -> None:
        """Discard the live bubble without mounting (legacy / rare)."""
        if self._live_bubble is not None:
            self._live_bubble.remove()
            self._live_bubble = None
            self._live_text = ""

    def flush_stream_before_tool(self) -> None:
        """Turn streamed preamble into a real bubble before a tool runs.

        Previously we called cancel_stream() here, which deleted text like
        "I will help you…" when the model started a tool call. We keep that
        text and drop only a trailing <tool_call> blob if present.
        """
        text = self._live_text
        if self._live_bubble is not None:
            self._live_bubble.remove()
            self._live_bubble = None
        self._live_text = ""
        cleaned = _strip_trailing_tool_artifacts(text)
        body, _total = split_trailing_token_usage(cleaned)
        body = body.strip()
        if body:
            self._mount_parsed(body)
        self.scroll_end(animate=False)

    # --- Internal ---

    def _mount_parsed(self, text: str) -> None:
        """Split on structured ```bash-style fences, then render each segment."""
        pos = 0
        for m in _SHELL_ACTION_FENCE_RE.finditer(text):
            before = text[pos : m.start()]
            if before.strip():
                self._mount_text_with_command_bars(before)
            body = m.group("body")
            if _body_is_desc_command_pairs(body):
                for cmd, desc in _parse_desc_command_fence(body):
                    self.mount(_ActionBlock(cmd, desc))
            else:
                self._mount_text_with_command_bars(m.group(0))
            pos = m.end()
        tail = text[pos:]
        if tail.strip():
            self._mount_text_with_command_bars(tail)

    def _mount_text_with_command_bars(self, text: str) -> None:
        stripped = text.strip()
        if not stripped:
            return
        self.mount(_AssistantBubble(stripped))

    def _finalize_live(self) -> None:
        self.end_stream_wait()
        if self._live_bubble is not None:
            self._live_bubble = None
            self._live_text = ""
