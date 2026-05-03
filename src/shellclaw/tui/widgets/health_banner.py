"""Health banner widget.

Displayed at the top of the main screen as a compact single-line strip.
Shows clickable status items — clicking one pre-fills the input bar
with a diagnostic prompt for that issue.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import Static

from ...health.snapshot import HealthItem, HealthStatus


_STATUS_DOTS = {
    HealthStatus.OK: "●",
    HealthStatus.WARN: "●",
    HealthStatus.CRITICAL: "●",
}

_STATUS_COLOURS = {
    HealthStatus.OK: "green",
    HealthStatus.WARN: "yellow",
    HealthStatus.CRITICAL: "red",
}


class _HealthLabel(Static):
    """A single compact health status label, clickable if it has a prompt."""

    DEFAULT_CSS = """
    _HealthLabel {
        width: auto;
        height: 1;
        padding: 0 2;
    }

    _HealthLabel.clickable {
        text-style: bold;
    }

    _HealthLabel.clickable:hover {
        background: $surface-lighten-1;
    }
    """

    def __init__(self, text: str, prompt: str = "") -> None:
        super().__init__(text, classes="clickable" if prompt else "")
        self._prompt = prompt

    def on_click(self) -> None:
        if self._prompt:
            self.post_message(HealthBanner.ItemClicked(prompt=self._prompt))


class HealthBanner(Horizontal):
    """A compact horizontal row of health status items."""

    DEFAULT_CSS = """
    HealthBanner {
        height: 1;
        padding: 0;
        background: $surface;
    }

    #banner-loading {
        color: $text-muted;
        padding: 0 1;
        text-style: italic;
        width: auto;
        height: 1;
    }
    """

    class ItemClicked(Message):
        def __init__(self, prompt: str) -> None:
            super().__init__()
            self.prompt = prompt

    def compose(self) -> ComposeResult:
        yield Static("Scanning...", id="banner-loading")

    def set_items(self, items: list[HealthItem]) -> None:
        loading = self.query("#banner-loading")
        for widget in loading:
            widget.remove()

        for item in items:
            dot = _STATUS_DOTS.get(item.status, "○")
            colour = _STATUS_COLOURS.get(item.status, "white")
            text = f"[{colour}]{dot}[/{colour}] {item.label}: {item.message}"
            self.mount(_HealthLabel(text, prompt=item.diagnostic_prompt))
