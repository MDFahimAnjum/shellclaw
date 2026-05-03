"""Stream-wait indicators: hardcoded constants, terminal spinner, TUI bubble."""

from __future__ import annotations

import asyncio
import sys

from textual.widgets import Static

# Not loaded from TOML — tweak here for TUI + `shellclaw check`.
WAIT_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
WAIT_SPINNER_TICK_SECONDS = 0.02
WAIT_MESSAGE_PREFIX = "Working…"


class TerminalStreamWait:
    """Animated working line on stdout until ``stop()`` (e.g. first stream chunk)."""

    def __init__(self, *, enabled: bool) -> None:
        self._enabled = enabled
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if not self._enabled:
            return
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
            self._task = None

    async def _run(self) -> None:
        i = 0
        n = len(WAIT_SPINNER_FRAMES)
        while not self._stop.is_set():
            sys.stdout.write(
                f"\r\033[2K{WAIT_MESSAGE_PREFIX} {WAIT_SPINNER_FRAMES[i % n]}"
            )
            sys.stdout.flush()
            i += 1
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=WAIT_SPINNER_TICK_SECONDS,
                )
                break
            except asyncio.TimeoutError:
                pass
        sys.stdout.write("\r\033[2K")
        sys.stdout.flush()


class StreamWaitBubble(Static):
    """Dim line with a Braille spinner until the first stream chunk (no italic)."""

    DEFAULT_CSS = """
    StreamWaitBubble {
        padding: 1 2;
        margin: 0 1 0 1;
        color: $text-muted;
    }
    """

    def __init__(self, *args, **kwargs) -> None:
        tick = kwargs.pop("tick_seconds", None)
        message_prefix = kwargs.pop("message_prefix", None)
        frames = kwargs.pop("frames", None)
        self._tick_seconds = (
            WAIT_SPINNER_TICK_SECONDS if tick is None else float(tick)
        )
        self._message_prefix = (
            WAIT_MESSAGE_PREFIX if message_prefix is None else message_prefix
        )
        self._frames = WAIT_SPINNER_FRAMES if frames is None else frames
        if not self._frames:
            self._frames = WAIT_SPINNER_FRAMES
        if not args:
            super().__init__(
                f"{self._message_prefix} {self._frames[0]}",
                **kwargs,
            )
        else:
            super().__init__(*args, **kwargs)

    def on_mount(self) -> None:
        self._spinner_i = 0
        self.set_interval(self._tick_seconds, self._tick_spinner)

    def _tick_spinner(self) -> None:
        n = len(self._frames)
        ch = self._frames[self._spinner_i % n]
        self._spinner_i += 1
        self.update(f"{self._message_prefix} {ch}")
