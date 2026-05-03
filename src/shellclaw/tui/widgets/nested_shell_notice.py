"""Modal shown when the user starts bash/zsh/etc. from inside the hooked shell."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static

_NESTED_SHELL_MESSAGE = (
    "You started a nested interactive shell. Note that ShellClaw will have a continuous terminal history regardless of the shell you use."
)


class NestedShellInfoModal(ModalScreen[None]):
    """Short informational dialog; dismissed with OK."""

    def __init__(self, command: str = "") -> None:
        super().__init__()
        self._command = (command or "").strip()

    DEFAULT_CSS = """
    NestedShellInfoModal {
        align: center middle;
    }
    #nested-shell-wrap {
        width: 70;
        max-width: 90%;
        height: auto;
        border: round $primary;
        background: $panel;
        padding: 1 2;
    }
    #nested-shell-text {
        margin-bottom: 1;
    }
    #nested-shell-ok {
        width: 100%;
    }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="nested-shell-wrap"):
            if self._command:
                yield Static(f"[dim]Command:[/dim] {self._command}", id="nested-shell-cmd")
            yield Static(_NESTED_SHELL_MESSAGE, id="nested-shell-text")
            yield Button("OK", variant="primary", id="nested-shell-ok")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "nested-shell-ok":
            self.dismiss()
