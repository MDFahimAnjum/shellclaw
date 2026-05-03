"""Terminal panel: one PTY + pyte-backed live shell (user and agent ``run_safe``)."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Callable

from textual import events
from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.message import Message
from textual.widgets import Input, Static, TextArea

from ...pty_emulator import CommandEvent, PtyEmulator, is_nested_interactive_shell_command
from ...safety.sandbox import format_shell_output
from ..clipboard import copy_and_notify
from .input_chrome import (
    TERM_CLEAR_LABEL,
    TERM_SEND_KEY,
    TERM_STOP_KEY,
    ChromeAction,
    ChromePressed,
    SendStopChrome,
)
class TerminalUserCommand(Message):
    """User ran a shell command in the side terminal (typed or via Execute)."""

    def __init__(self, command: str, output: str) -> None:
        super().__init__()
        self.command = command
        self.output = output


class NestedShellSessionStarted(Message):
    """Posted when the user launches bash/zsh/etc. from the hooked outer shell."""

    def __init__(self, command: str) -> None:
        super().__init__()
        self.command = command


class PasswordPromptActive(Message):
    """PTY viewport shows a password-style prompt (sudo, su, …)."""

    def __init__(self, active: bool) -> None:
        super().__init__()
        self.active = active


class ShellCommandTextArea(TextArea):
    """Multiline shell input; **F2** / **Ctrl+O** send; **F5** stops the PTY command (see chrome); **F4** clears terminal."""

    class Submitted(Message):
        """Posted when the user presses F2 or Ctrl+O."""

        bubble = True

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("tab_behavior", "focus")
        kwargs.setdefault("show_line_numbers", False)
        super().__init__(*args, **kwargs)

    def action_submit_shell(self) -> None:
        self.post_message(self.Submitted(self.text))

    def key_f2(self) -> None:
        self.action_submit_shell()

    def key_ctrl_o(self) -> None:
        self.action_submit_shell()


_CTRL_RE = re.compile(r"ctrl\+(.+)", re.I)


class EmbeddedTerminal(Static):
    """Renders a ``PtyEmulator`` screen via pyte → Rich (``markup=False``).

    Subclasses :class:`~textual.widgets.Static` so ``update()`` invalidates
    content-height cache; plain ``Widget`` + ``render()`` would keep a stale
    height when lines grow at the same width (no scrollbar, clipped output).
    """

    can_focus = True

    DEFAULT_CSS = """
    EmbeddedTerminal {
        width: 100%;
        min-height: 4;
        min-width: 10;
    }
    """

    def __init__(self, *_args: object, **kwargs: object) -> None:
        on_command_start = kwargs.pop("on_command_start", None)
        on_command_complete = kwargs.pop("on_command_complete", None)
        kwargs.setdefault("markup", False)
        super().__init__("", **kwargs)
        self._on_command_start: Callable[[str], None] | None = on_command_start
        self._on_command_complete: Callable[[CommandEvent], None] | None = (
            on_command_complete
        )
        self._emu: PtyEmulator | None = None
        self._sel_start: tuple[int, int] | None = None
        self._last_pwd_announced: bool = False

    @property
    def emulator(self) -> PtyEmulator | None:
        return self._emu

    def _viewport_cell_size(self) -> tuple[int, int]:
        """Shell geometry follows the scroll viewport, not the tall virtual render."""
        p = self.parent
        if isinstance(p, ScrollableContainer):
            if p.size.width > 0 and p.size.height > 0:
                return max(20, p.size.width), max(4, p.size.height)
        if self.size.width > 0 and self.size.height > 0:
            return max(20, self.size.width), max(4, self.size.height)
        return 80, 24

    def on_mount(self) -> None:
        cols, rows = self._viewport_cell_size()
        rows = max(4, rows)
        cols = max(20, cols)
        self._emu = PtyEmulator(
            rows,
            cols,
            on_command_start=self._on_command_start,
            on_command_complete=self._on_command_complete,
        )
        self._emu.start()
        loop = asyncio.get_running_loop()
        self._emu.attach_reader(loop)
        self.set_interval(0.1, self._tick)
        self.update(self._emu.build_rich_text())
        # Resize to the actual panel dimensions after the first compositor pass
        # so the shell sees the correct $COLUMNS from its very first prompt draw
        # rather than the 80×24 fallback used when sizes are not yet available.
        self.call_after_refresh(self._apply_real_size)

    def _apply_real_size(self) -> None:
        if self._emu is None:
            return
        w, h = self._viewport_cell_size()
        # Only send SIGWINCH when the size differs from what was used at start,
        # preventing a spurious prompt redraw if the fallback happened to be correct.
        if w != self._emu._cols or h != self._emu._rows:
            self._emu.resize(max(4, h), max(20, w))
            self.update(self._emu.build_rich_text())

    def on_unmount(self) -> None:
        if self._emu is not None:
            self._emu.detach_reader()
            self._emu.kill()
            self._emu = None

    def _tick(self) -> None:
        if self._emu is None:
            return
        pwd = self._emu.password_prompt_active()
        if pwd != self._last_pwd_announced:
            self._last_pwd_announced = pwd
            self.post_message(PasswordPromptActive(pwd))
        if self._emu.take_dirty():
            self.update(self._emu.build_rich_text())
            self.call_after_refresh(self._scroll_parent_to_tail)

    def _scroll_parent_to_tail(self) -> None:
        p = self.parent
        if isinstance(p, ScrollableContainer):
            p.scroll_end(animate=False)

    def on_resize(self, _event: events.Resize) -> None:
        if self._emu is None:
            return
        w, h = self._viewport_cell_size()
        h = max(4, h)
        w = max(20, w)
        self._emu.resize(h, w)
        self.update(self._emu.build_rich_text())

    def _send_bytes(self, data: bytes) -> None:
        if self._emu is not None:
            self._emu.send_bytes(data)

    def on_key(self, event: events.Key) -> None:
        if not self.has_focus or self._emu is None:
            return
        key = event.key
        if key == "enter":
            self._emu.password_mask_on_enter()
            self._send_bytes(b"\r")
        elif key == "tab":
            self._send_bytes(b"\t")
        elif key == "backspace":
            self._emu.password_mask_on_backspace()
            self._send_bytes(b"\x7f")
        elif key == "delete":
            self._send_bytes(b"\x1b[3~")
        elif key == "escape":
            self._send_bytes(b"\x1b")
        elif key == "up":
            self._send_bytes(b"\x1b[A")
        elif key == "down":
            self._send_bytes(b"\x1b[B")
        elif key == "right":
            self._send_bytes(b"\x1b[C")
        elif key == "left":
            self._send_bytes(b"\x1b[D")
        elif key == "home":
            self._send_bytes(b"\x1b[H")
        elif key == "end":
            self._send_bytes(b"\x1b[F")
        elif key == "pageup":
            self._send_bytes(b"\x1b[5~")
        elif key == "pagedown":
            self._send_bytes(b"\x1b[6~")
        elif key in ("f3", "f4", "f5"):
            return
        elif m := _CTRL_RE.fullmatch(str(key)):
            ch = m.group(1).lower()
            if len(ch) == 1 and "a" <= ch <= "z":
                self._send_bytes(bytes([ord(ch) - ord("a") + 1]))
            elif ch == "space":
                self._send_bytes(b"\x00")
            else:
                event.stop()
                return
        elif event.character and event.character.isprintable():
            self._emu.password_mask_on_printable(event.character)
            self._send_bytes(event.character.encode("utf-8", errors="replace"))
        else:
            event.stop()
            return
        event.stop()

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self._sel_start = (event.y, event.x)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button != 1 or self._sel_start is None:
            return
        start = self._sel_start
        self._sel_start = None
        end = (event.y, event.x)
        if start == end:
            return
        if self._emu is None:
            return
        r0, c0 = start
        r1, c1 = end
        selected = self._emu.extract_selected_text(r0, c0, r1, c1)
        stripped = selected.strip()
        if not stripped:
            return
        asyncio.get_running_loop().create_task(copy_and_notify(stripped, self.app))

    def snapshot(self) -> dict[str, str]:
        if self._emu is None:
            return {"mode": "normal", "content": ""}
        return self._emu.get_snapshot()


class TerminalWidget(Vertical):
    """Right panel — one live PTY (user + agent shell commands)."""

    BORDER_TITLE = "Terminal"

    DEFAULT_CSS = """
    TerminalWidget {
        border-title-align: left;
    }
    #terminal-scroll {
        width: 1fr;
        height: 1fr;
        min-height: 1;
        overflow-y: auto;
        scrollbar-size-vertical: 1;
    }
    #terminal-scroll > EmbeddedTerminal {
        width: 100%;
        height: auto;
    }
    #terminal-input-stack {
        dock: bottom;
        height: 8;
        layout: vertical;
    }
    #terminal-input-title-row {
        height: 1;
        width: 100%;
        layout: horizontal;
    }
    #terminal-title-spacer {
        width: 1fr;
        height: 1;
    }
    #terminal-actions-right {
        width: auto;
        height: 1;
        layout: horizontal;
        content-align: right middle;
    }
    #terminal-input-body {
        height: 1fr;
        min-height: 4;
        layout: horizontal;
        width: 100%;
    }
    #terminal-input-main {
        width: 1fr;
        height: 100%;
        min-height: 2;
    }
    #terminal-input-password {
        width: 1fr;
        height: 100%;
    }
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._on_pty_cc: Callable[[CommandEvent], None] | None = None
        self._history: list[tuple[str, str]] = []

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="terminal-scroll"):
            yield EmbeddedTerminal(id="embedded-terminal")
        with Vertical(id="terminal-input-stack"):
            with Horizontal(id="terminal-input-title-row"):
                yield Static("", id="terminal-title-spacer")
                with Horizontal(id="terminal-actions-right"):
                    yield ChromeAction(
                        TERM_CLEAR_LABEL,
                        target="terminal",
                        action="clear",
                        id="terminal-clear-btn",
                    )
                    yield SendStopChrome(
                        target="terminal",
                        key_label=TERM_SEND_KEY,
                        stop_key_label=TERM_STOP_KEY,
                        id="terminal-send-or-stop",
                    )
            with Horizontal(id="terminal-input-body"):
                yield ShellCommandTextArea(
                    id="terminal-input-main",
                    placeholder="Shell — Enter newline · F2/Ctrl+O send · F5 stop shell · F4 clear terminal",
                )
                yield Input(
                    id="terminal-input-password",
                    password=True,
                    placeholder="Password — Enter to send to terminal",
                )

    def screen_snapshot(self) -> dict[str, str]:
        """Current pyte viewport for the live PTY (``mode`` / ``content``); see ``EmbeddedTerminal.snapshot``."""
        et = self.query_one("#embedded-terminal", EmbeddedTerminal)
        return et.snapshot()

    def set_pty_complete_handler(self, fn: Callable[[CommandEvent], None] | None) -> None:
        """Called from ``MainScreen`` after mount to wire Recall / ``TerminalUserCommand``."""
        self._on_pty_cc = fn
        self._apply_pty_callbacks()

    def _apply_pty_callbacks(self) -> None:
        et = self.query_one("#embedded-terminal", EmbeddedTerminal)
        emu = et.emulator
        if emu is None:
            self._sync_stop_visibility_to_pty()
            return

        def on_start(cmd: str) -> None:
            self._sync_stop_visibility_to_pty()
            if is_nested_interactive_shell_command(cmd):
                self.post_message(NestedShellSessionStarted(cmd))

        def on_complete(ev: CommandEvent) -> None:
            self._sync_stop_visibility_to_pty()
            if self._on_pty_cc is not None:
                self._on_pty_cc(ev)

        emu.set_callbacks(on_start, on_complete)
        self._sync_stop_visibility_to_pty()

    def _set_stop_visible(self, visible: bool) -> None:
        if not self.is_mounted:
            return
        clear = self.query_one("#terminal-clear-btn", ChromeAction)
        dual = self.query_one("#terminal-send-or-stop", SendStopChrome)
        clear.visible = not visible
        dual.set_busy(visible)

    def _sync_stop_visibility_to_pty(self) -> None:
        """Keep Stop aligned with OSC segment state (PTY can get bytes before callbacks exist)."""
        if not self.is_mounted:
            return
        et = self.query_one("#embedded-terminal", EmbeddedTerminal)
        emu = et.emulator
        if emu is None:
            self._set_stop_visible(False)
            return
        self._set_stop_visible(emu.stop_button_visible())

    def on_mount(self) -> None:
        self._apply_pty_callbacks()
        self._sync_stop_visibility_to_pty()
        self.call_after_refresh(self._sync_stop_visibility_to_pty)
        # ``Input`` does not accept ``display=`` in ``__init__`` on older Textual.
        self.query_one("#terminal-input-password", Input).display = False
        self.call_after_refresh(lambda: self._focus_default_shell_input())

    def _focus_default_shell_input(self) -> None:
        if not self.is_mounted:
            return
        ta = self.query_one("#terminal-input-main", ShellCommandTextArea)
        if ta.display:
            ta.focus()
        else:
            self.query_one("#terminal-input-password", Input).focus()

    def on_password_prompt_active(self, event: PasswordPromptActive) -> None:
        """Drive the bottom bar: it stays focused by default, so mask there for sudo passwords."""
        if not self.is_mounted:
            return
        main = self.query_one("#terminal-input-main", ShellCommandTextArea)
        secret = self.query_one("#terminal-input-password", Input)
        if event.active:
            main.display = False
            secret.display = True  # overrides DEFAULT_CSS display:none while active
            secret.placeholder = "Password — masked · Enter to send to terminal"
            secret.focus()
        else:
            secret.display = False
            main.display = True
            secret.value = ""
            main.focus()

    def stop_pty_interrupt(self) -> str:
        """Send ``^C`` to the shared PTY when a shell line is active (same as the Stop button)."""
        if not self.is_mounted:
            return "[error] Terminal is not mounted."
        et = self.query_one("#embedded-terminal", EmbeddedTerminal)
        emu = et.emulator
        if emu is None:
            return "[error] Terminal is not ready."
        if not emu.command_active():
            return (
                "[info] No shell command is currently running in the side terminal; "
                "nothing was sent."
            )
        emu.send_interrupt()
        return (
            "Sent interrupt (^C) to the live terminal foreground command "
            "(same as the Stop button)."
        )

    def on_chrome_pressed(self, event: ChromePressed) -> None:
        if event.target != "terminal":
            return
        event.stop()
        match event.action:
            case "clear":
                self.clear_history()
            case "send":
                main = self.query_one("#terminal-input-main", ShellCommandTextArea)
                secret = self.query_one("#terminal-input-password", Input)
                if main.display:
                    self._submit_shell_to_pty(main.text)
                    main.text = ""
                elif secret.display:
                    self._submit_shell_to_pty(secret.value)
                    secret.value = ""
            case "stop":
                self.stop_pty_interrupt()

    def _submit_shell_to_pty(self, raw: str) -> None:
        cmd = raw.strip()
        if not cmd:
            return
        et = self.query_one("#embedded-terminal", EmbeddedTerminal)
        if et.emulator is not None:
            et.emulator.send_bytes((cmd + "\n").encode("utf-8", errors="replace"))

    def on_shell_command_text_area_submitted(self, event: ShellCommandTextArea.Submitted) -> None:
        self._submit_shell_to_pty(event.value)
        self.query_one("#terminal-input-main", ShellCommandTextArea).text = ""

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "terminal-input-password":
            return
        self._submit_shell_to_pty(event.value)
        event.input.value = ""

    def add_pending(self, label: str) -> None:
        """Legacy hook — shell tools use the PTY; non-shell tools skip the panel."""

    def add_command(self, command: str, result: str) -> None:
        """Record agent tool output when not shown in the PTY (non-``run_safe`` tools)."""
        self._history.append((command, result))

    def mount_live_output_block(self) -> None:
        """Legacy — no-op when output is not streamed into this panel."""

    def append_live_line(self, line: str) -> None:
        """Legacy — no-op when output is not streamed into this panel."""

    async def run_shell_streaming(
        self,
        command: str,
        *,
        timeout: float,
        pending_label: str | None = None,
    ) -> str:
        """Submit a shell line to the shared PTY (same path as the input bar)."""
        del timeout, pending_label  # completion is tracked via OSC, not this await
        cmd = command.strip()
        if not cmd:
            return ""
        emu = self.query_one("#embedded-terminal", EmbeddedTerminal).emulator
        if emu is None:
            return "(terminal not ready)"
        emu.send_bytes((cmd + "\n").encode("utf-8", errors="replace"))
        return ""

    async def run_pty_command(self, cmd: str, timeout: int) -> tuple[str, int]:
        """Agent ``run_safe`` path — returns raw output text and exit code."""
        emu = self.query_one("#embedded-terminal", EmbeddedTerminal).emulator
        if emu is None:
            return ("", -1)
        ev = await emu.run_command_and_wait(cmd, timeout=timeout, source="agent")
        return ev.output, ev.exit_code

    def clear_history(self) -> None:
        self._history.clear()
        if self.is_mounted:
            et = self.query_one("#embedded-terminal", EmbeddedTerminal)
            if et.emulator is not None:
                et.emulator.send_bytes(b"clear\n")

    def prefill_input(self, text: str) -> None:
        """Put text in the shell input bar (Copy-to-terminal / Execute prep)."""
        if not self.is_mounted:
            return
        main = self.query_one("#terminal-input-main", ShellCommandTextArea)
        if main.display:
            main.text = text
            main.focus()
        else:
            pw = self.query_one("#terminal-input-password", Input)
            pw.value = text
            pw.focus()
