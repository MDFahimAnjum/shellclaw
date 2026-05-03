"""Fixed-height scrollable pane for model reasoning / thinking streams."""

from __future__ import annotations

from rich.markup import escape

from textual.app import ComposeResult
from textual.containers import ScrollableContainer
from textual.widgets import Static

# Cap memory; keep the tail so the viewport shows the latest reasoning.
_MAX_REASONING_CHARS = 48_000


class ReasoningTrace(ScrollableContainer):
    """Shows streaming reasoning in a short viewport; older text scrolls up and out of view."""

    DEFAULT_CSS = """
    ReasoningTrace {
        height: 6;
        border: solid $primary-darken-2;
        border-title-color: $primary;
        border-title-align: left;
        background: $surface-darken-1;
        scrollbar-size-vertical: 1;
    }
    ReasoningTrace > Static {
        width: 100%;
        height: auto;
    }
    """

    BORDER_TITLE = "Reasoning"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._buffer = ""

    def compose(self) -> ComposeResult:
        yield Static("", id="reasoning-text")

    def clear_trace(self) -> None:
        self._buffer = ""
        if self.is_mounted:
            self.query_one("#reasoning-text", Static).update("")

    def append(self, chunk: str) -> None:
        if not chunk:
            return
        self._buffer += chunk
        if len(self._buffer) > _MAX_REASONING_CHARS:
            self._buffer = self._buffer[-_MAX_REASONING_CHARS:]
        body = self.query_one("#reasoning-text", Static)
        body.update(f"[dim italic]{escape(self._buffer)}[/dim italic]")
        self.scroll_end(animate=False)
