"""Explain modal — three-layer command explanation popup.

Triggered by the [?] button on any command block.
Shows:
  1. What the command does (tldr description)
  2. What the flags mean (parsed from tldr examples)
  3. What this output means in context (LLM-generated, streamed)
"""

from __future__ import annotations

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Markdown, Static

from ...wiki.commands import command_names_in_shell_line
from ...wiki.glossary import find_terms_for_shell_command, lookup as glossary_lookup
from ...wiki.parser import describe_flags
from ...wiki.tldr import format_for_shell_line, lookup as tldr_lookup

from ...utils import StreamWaitBubble


class _CloseLabel(Static):
    """Plain text control — matches chat action row (▶ run / ? explain / ⎘ copy)."""

    DEFAULT_CSS = "_CloseLabel { color: $primary-lighten-2; width: auto; height: 1; padding: 0 2; }"

    def __init__(self) -> None:
        super().__init__("✕ close")

    def on_click(self) -> None:
        self.screen.dismiss()


class ExplainModal(ModalScreen):
    """Full-screen explain panel for a command and its output."""

    DEFAULT_CSS = """
    ExplainModal {
        align: center middle;
    }

    #explain-container {
        width: 80%;
        height: 80%;
        border: round $primary;
        background: $panel;
        padding: 0;
    }

    #explain-title {
        background: $primary;
        color: $text;
        padding: 1 2;
        text-style: bold;
    }

    #explain-body {
        padding: 1 2;
        overflow-y: auto;
        height: 1fr;
    }

    .section-heading {
        text-style: bold;
        color: $primary-lighten-1;
        margin-top: 1;
        margin-bottom: 0;
    }

    #contextual-content {
        color: $text;
        display: none;
    }

    #explain-footer {
        height: auto;
        padding: 1 2;
        dock: bottom;
        background: $surface;
        layout: horizontal;
        align: left middle;
        border-top: solid $primary-darken-2;
    }
    """

    BINDINGS = [
        Binding("escape", "dismiss", "Close", show=True),
        Binding("q", "dismiss", "Close", show=False),
    ]

    def __init__(self, command: str, output: str) -> None:
        super().__init__()
        self._command = command
        self._output = output

    def compose(self) -> ComposeResult:
        names = command_names_in_shell_line(self._command)
        multi = len(names) > 1

        with Vertical(id="explain-container"):
            yield Static(
                f"Explaining: {self._command}",
                id="explain-title",
            )

            with ScrollableContainer(id="explain-body"):
                # Section 1: What the command does (per executable; sudo/pipes handled)
                yield Static("What this command does", classes="section-heading")
                if not names:
                    yield Static("Could not detect a command in this line.")
                else:
                    for name in names:
                        tldr_entry = tldr_lookup(name.lower())
                        label = f"{name}" if not multi else f"{name} (in this pipeline)"
                        if tldr_entry:
                            desc = tldr_entry.get("description", "No description available.")
                            yield Static(f"{label}: {desc}")
                        else:
                            yield Static(f"{label}: No local documentation found.")

                terms = find_terms_for_shell_command(self._command)
                if terms:
                    yield Static("Related glossary terms", classes="section-heading")
                    for term in terms:
                        definition = glossary_lookup(term)
                        if definition:
                            yield Static(f"  {term}: {definition}")

                # Section 2: Flag breakdown
                flags = describe_flags(self._command)
                if flags:
                    yield Static("Breaking down the flags", classes="section-heading")
                    for item in flags:
                        if item.get("multi_segment"):
                            prefix = f"[{item['stage']}] {item['command']}: "
                        else:
                            prefix = ""
                        yield Static(f"  {prefix}{item['flag']}  →  {item['description']}")

                # Section 3: Contextual LLM explanation (streamed)
                yield Static("What this means for you", classes="section-heading")
                with Vertical(id="contextual-stack"):
                    yield StreamWaitBubble(id="contextual-wait")
                    yield Markdown("", id="contextual-content")

            with Horizontal(id="explain-footer"):
                yield _CloseLabel()

    def on_mount(self) -> None:
        self._stream_contextual()

    @work(exclusive=False, thread=False)
    async def _stream_contextual(self) -> None:
        tldr_ctx = format_for_shell_line(self._command)

        try:
            app = self.app
            loop = None

            # Find the agent loop from the main screen
            from ..screens.main import MainScreen
            for screen in app.screen_stack:
                if isinstance(screen, MainScreen):
                    loop = screen._loop
                    distro_info = screen._distro_info
                    break

            if loop is None:
                self._set_contextual("Connect to a session to see contextual explanations.")
                return

            full_text = ""
            first_chunk = True
            async for chunk in loop.explain_command(
                command=self._command,
                output=self._output,
                tldr_context=tldr_ctx,
                distro_info=distro_info,
            ):
                if first_chunk:
                    first_chunk = False
                    self._end_contextual_wait()
                full_text += chunk
                try:
                    self.query_one("#contextual-content", Markdown).update(full_text)
                except Exception:
                    pass

            if not full_text.strip():
                self._set_contextual("No explanation was returned.")

        except Exception as exc:
            self._set_contextual(f"Could not generate explanation: {exc}")

    def _end_contextual_wait(self) -> None:
        """Hide spinner and show the contextual Markdown (idempotent)."""
        try:
            self.query_one("#contextual-wait", StreamWaitBubble).remove()
        except Exception:
            pass
        try:
            self.query_one("#contextual-content", Markdown).display = True
        except Exception:
            pass

    def _set_contextual(self, text: str) -> None:
        self._end_contextual_wait()
        try:
            self.query_one("#contextual-content", Markdown).update(text)
        except Exception:
            pass

    def action_dismiss(self) -> None:
        self.dismiss()
