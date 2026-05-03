"""First-time SimpleX linking: ``/connect`` invitation link and auto ``chat_ref``."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Label, Static

from ...config import AppConfig, load_config, save_config, simplex_database_prefix
from ...simplex.bridge import SimpleXBridge, SimpleXError
from ..clipboard import copy_and_notify


@dataclass
class SimplexSetupResult:
    """Returned when setup saved a ``chat_ref`` and the bridge is still running."""

    chat_ref: str


class SimplexSetupScreen(ModalScreen[SimplexSetupResult | None]):
    """Run ``/connect``, show the one-line link, then wait for phone activity to learn ``chat_ref``."""

    DEFAULT_CSS = """
    SimplexSetupScreen {
        align: center middle;
    }
    #simplex-setup-box {
        width: 90;
        max-width: 100;
        height: auto;
        max-height: 90%;
        border: round $primary;
        background: $panel;
        padding: 1 2;
    }
    #simplex-setup-title {
        text-style: bold;
        margin-bottom: 1;
    }
    #simplex-setup-link {
        height: auto;
        margin: 1 0;
        color: $accent;
    }
    #simplex-setup-status {
        color: $text-muted;
        height: auto;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._cfg = config
        self._bridge: SimpleXBridge | None = None
        self._link: str = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="simplex-setup-box"):
            yield Static("SimpleX — link your phone", id="simplex-setup-title")
            yield Static(
                "Starting…",
                id="simplex-setup-link",
            )
            yield Static(
                "On your phone: open SimpleX → add contact → paste the link.",
                id="simplex-setup-hint",
            )
            yield Static("", id="simplex-setup-status")
            with Horizontal():
                yield Button("Copy link", id="btn-simplex-copy", disabled=True)
                yield Button("Done — I linked", id="btn-simplex-done", disabled=True)
                yield Button("Cancel", id="btn-simplex-cancel")

    def on_mount(self) -> None:
        self._run_invite_phase()

    def action_cancel(self) -> None:
        asyncio.create_task(self._cleanup_and_dismiss(None))

    @work(exclusive=True, thread=False)
    async def _run_invite_phase(self) -> None:
        status = self.query_one("#simplex-setup-status", Static)
        link_w = self.query_one("#simplex-setup-link", Static)
        try:
            self._bridge = SimpleXBridge(
                database_prefix=simplex_database_prefix(self._cfg),
                port=self._cfg.simplex.port,
                executable=self._cfg.simplex.executable,
                chat_ref="",
                accept_any_chat=True,
                on_inbound_user_text=None,
            )
            await self._bridge.start()
            link = await self._bridge.request_connect_link()
            self._link = link
            link_w.update(link)
            status.update("Invitation ready — copy the link, then tap “Done — I linked”.")
            self.query_one("#btn-simplex-copy", Button).disabled = False
            self.query_one("#btn-simplex-done", Button).disabled = False
        except SimpleXError as exc:
            status.update(str(exc))
            link_w.update("(failed)")
            if self._bridge is not None:
                await self._bridge.stop()
                self._bridge = None
        except Exception as exc:
            status.update(f"{type(exc).__name__}: {exc}")
            link_w.update("(failed)")
            if self._bridge is not None:
                await self._bridge.stop()
                self._bridge = None

    @work(exclusive=True, thread=False)
    async def _wait_chat_ref_and_save(self) -> None:
        status = self.query_one("#simplex-setup-status", Static)
        if self._bridge is None or not self._bridge.is_running:
            status.update("Bridge is not running.")
            return
        status.update("Waiting for activity from your phone (up to 3 min)…")
        for _ in range(360):
            ref = self._bridge.detected_chat_ref
            if ref:
                cfg = load_config()
                new_sx = cfg.simplex.model_copy(update={"chat_ref": ref})
                new_cfg = cfg.model_copy(update={"simplex": new_sx})
                save_config(new_cfg)
                status.update(f"Saved chat_ref {ref}.")
                if self._bridge is not None:
                    await self._bridge.stop()
                    self._bridge = None
                await asyncio.sleep(0.05)
                self.dismiss(SimplexSetupResult(chat_ref=ref))
                return
            await asyncio.sleep(0.5)
        status.update("Timed out — send any message from the phone, then run setup again.")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-simplex-cancel":
            asyncio.create_task(self._cleanup_and_dismiss(None))
        elif event.button.id == "btn-simplex-copy":
            if self._link:
                asyncio.create_task(copy_and_notify(self._link, self.app))
        elif event.button.id == "btn-simplex-done":
            self._wait_chat_ref_and_save()

    async def _cleanup_and_dismiss(self, result: SimplexSetupResult | None) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None
        self.dismiss(result)

    async def on_unmount(self) -> None:
        if self._bridge is not None:
            await self._bridge.stop()
            self._bridge = None
