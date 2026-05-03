"""Undo log modal widget.

Shows recent actions with [Undo] buttons for reversible entries.
Accessible via Ctrl+U on the main screen.
"""

from __future__ import annotations

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ...safety.undo import ActionKind, UndoLog


class UndoLogModal(ModalScreen):
    """Full-screen undo log."""

    DEFAULT_CSS = """
    UndoLogModal {
        align: center middle;
    }

    #undo-container {
        width: 80%;
        height: 70%;
        border: round $primary;
        background: $panel;
    }

    #undo-title {
        background: $primary;
        color: $text;
        padding: 1 2;
        text-style: bold;
        dock: top;
    }

    #undo-body {
        padding: 1 2;
        overflow-y: auto;
        height: 1fr;
    }

    .undo-entry {
        layout: horizontal;
        margin-bottom: 1;
        height: 3;
    }

    .undo-description {
        width: 1fr;
        padding: 1 0;
        color: $text;
    }

    .undo-irreversible {
        color: $text-muted;
        text-style: italic;
        padding: 1 0;
    }

    .btn-undo {
        width: auto;
        margin-left: 1;
    }

    #undo-footer {
        dock: bottom;
        height: 3;
        padding: 0 2;
        background: $surface;
        layout: horizontal;
        border-top: solid $primary-darken-2;
    }

    #undo-status {
        padding: 1;
        color: $success;
        width: 1fr;
    }

    #btn-close-undo {
        margin-top: 1;
    }

    .empty-message {
        color: $text-muted;
        padding: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._undo_log = UndoLog.load()

    def compose(self) -> ComposeResult:
        with Vertical(id="undo-container"):
            yield Static("Recent Actions", id="undo-title")

            with ScrollableContainer(id="undo-body"):
                entries = list(reversed(self._undo_log.entries[-50:]))
                if not entries:
                    yield Static("No actions recorded yet.", classes="empty-message")
                else:
                    for i, entry in enumerate(entries):
                        from textual.containers import Horizontal
                        time_str = entry.timestamp[:16].replace("T", " ") if entry.timestamp else ""
                        with Horizontal(classes="undo-entry", id=f"entry-{i}"):
                            yield Static(
                                f"[dim]{time_str}[/dim]  {entry.description}",
                                classes="undo-description",
                            )
                            if entry.reversible:
                                yield Button(
                                    "Undo",
                                    classes="btn-undo",
                                    id=f"undo-btn-{i}",
                                )
                            else:
                                yield Static(
                                    "Cannot undo",
                                    classes="undo-irreversible",
                                )

            with Horizontal(id="undo-footer"):
                yield Static("", id="undo-status")
                yield Button("Close", id="btn-close-undo")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-close-undo":
            self.dismiss()
            return

        if event.button.id and event.button.id.startswith("undo-btn-"):
            result = self._undo_log.undo_last()
            self.query_one("#undo-status", Static).update(result)
            # Disable the button after use
            event.button.disabled = True

    def action_dismiss(self) -> None:
        self.dismiss()
