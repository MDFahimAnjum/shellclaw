"""shellclaw Textual application.

The App is the root of the TUI.  On startup it checks for a config file:
- If none exists, it pushes the OnboardingScreen.
- Otherwise it pushes the MainScreen directly.

Agent events from the loop arrive via worker tasks and are dispatched
to the active screen through Textual's message passing system.
"""

from __future__ import annotations

from textual.app import App
from textual.binding import Binding

from ..config import config_exists, load_config
from .screens.main import MainScreen
from .screens.onboarding import OnboardingScreen


class shellclawApp(App):
    """The root Textual application."""

    TITLE = "shellclaw"
    CSS_PATH = None  # Each screen provides its own CSS

    BINDINGS = [
        Binding("ctrl+x", "quit", "Quit", show=True, priority=True),
    ]

    def on_mount(self) -> None:
        config = load_config()
        if config_exists():
            self.push_screen(MainScreen(config=config))
        else:
            self.push_screen(OnboardingScreen())

    def action_quit(self) -> None:
        self.exit()
