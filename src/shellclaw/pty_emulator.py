"""Unix PTY + OSC shell hooks + pyte screen model for the Textual terminal panel."""

from __future__ import annotations

import asyncio
import errno
import fcntl
import os
import re
import shlex

from rich.color import ColorParseError
import shutil
import signal
import struct
import tempfile
import termios
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import pty
from rich.style import Style
from rich.text import Text

from pyte.screens import Char, HistoryScreen
from pyte.streams import ByteStream

START_RE = re.compile(rb"\x1b\]777;shellclaw_START;cmd=([^\x07]*)\x07")
END_RE = re.compile(rb"\x1b\]777;shellclaw_END;exit=(\d+)\x07")

# Heuristic: common TTY password / PIN prompts (sudo, su, SSH, gpg, git https, …).
_PASSWORD_PROMPT_INLINE = re.compile(
    r"(?:\[sudo\]\s+password\s+for\s|"
    r"\bpassword\s+for\s|"
    r"\bpassphrase\s+for\s|"
    r"\bpin\s+for\s|"
    r"\benter\s+(?:same\s+)?passphrase\b|"
    r"\bCurrent\s+Kerberos\s+password\b|"
    r"\bLDAP\s+password\b|"
    r"^\s*\S+@[^:]+:\s+password:\s*$)",
    re.I,
)
_PASSWORD_PROMPT_END = re.compile(
    r"(?i)(?:^|\s)(?:verification\s+code|password|passphrase|pin)\s*:\s*$"
)

SourceKind = Literal["user", "agent", "unknown"]


@dataclass
class CommandEvent:
    command: str
    output: str
    exit_code: int
    context_before: str
    source: SourceKind = "unknown"


def _hooks_package_dir() -> Path:
    return Path(__file__).resolve().parent / "hooks"


def _write_session_hooks(session_dir: Path) -> tuple[Path, Path]:
    """Lay out ``shellclaw_shell.sh``, hooked ``bashrc``, and ``zdot/.zshrc`` for this PTY.

    ``zdot`` is always created so a bash-primary session can still spawn hooked zsh
    (and vice versa) using ``SHELLCLAW_*`` exports + wrapper functions in the hook script.
    """
    core_src = _hooks_package_dir() / "shellclaw_shell.sh"
    core = session_dir / "shellclaw_shell.sh"
    shutil.copyfile(core_src, core, follow_symlinks=True)
    os.chmod(core, 0o644)

    zdot = session_dir / "zdot"
    zdot.mkdir(exist_ok=True)
    bash_rc = session_dir / "bashrc"
    q_session = shlex.quote(str(session_dir.resolve()))
    q_core = shlex.quote(str(core.resolve()))
    q_bashrc = shlex.quote(str(bash_rc.resolve()))
    q_zdot = shlex.quote(str(zdot.resolve()))
    exports = (
        f"export SHELLCLAW_SESSION_DIR={q_session}\n"
        f"export SHELLCLAW_CORE={q_core}\n"
        f"export SHELLCLAW_BASHRC={q_bashrc}\n"
        f"export SHELLCLAW_ZDOTDIR={q_zdot}\n"
    )
    qdot = shlex.quote(str(core.resolve()))
    bash_rc.write_text(
        "# shellclaw bash session\n"
        + exports
        + '[ -f "$HOME/.bashrc" ] && . "$HOME/.bashrc"\n'
        f". {qdot}\n",
        encoding="utf-8",
    )
    (zdot / ".zshrc").write_text(
        "# shellclaw zsh session\n"
        + exports
        + '[ -f "$HOME/.zshrc" ] && . "$HOME/.zshrc"\n'
        f". {qdot}\n",
        encoding="utf-8",
    )
    return bash_rc, zdot


def _detect_shell() -> tuple[str, str]:
    exe = os.environ.get("SHELL", "")
    if not exe:
        try:
            import pwd

            exe = pwd.getpwuid(os.getuid()).pw_shell or "/bin/bash"
        except Exception:
            exe = "/bin/bash"
    base = Path(exe).name.lower()
    if "fish" in base:
        return "fish", shutil.which("fish") or exe
    if "zsh" in base:
        return "zsh", shutil.which("zsh") or exe
    if "bash" in base:
        return "bash", shutil.which("bash") or exe
    return "bash", shutil.which("bash") or "/bin/bash"


def _char_data(ch: Any) -> str:
    if isinstance(ch, Char):
        return ch.data
    return str(getattr(ch, "data", ch[0]))


def _row_to_str(screen: HistoryScreen, row: dict[int, Any]) -> str:
    parts: list[str] = []
    for x in range(screen.columns):
        ch = row.get(x, screen.default_char)
        parts.append(_char_data(ch))
    return "".join(parts).rstrip("\n")


def line_suggests_password_prompt(line: str) -> bool:
    """True if a screen row looks like a password / PIN / passphrase prompt."""
    t = (line or "").rstrip()
    if not t:
        return False
    if _PASSWORD_PROMPT_INLINE.search(t):
        return True
    if _PASSWORD_PROMPT_END.search(t):
        return True
    return False


def probe_line_for_password_prompt(screen: HistoryScreen) -> str:
    """The one terminal line used to decide password mode: cursor row, else last non-empty row.

    Only this line is checked so a stale ``[sudo] password`` row above the shell prompt
    does not keep the input bar in password mode after authentication.
    """
    cur = getattr(screen, "cursor", None)
    if cur is not None:
        cy = int(getattr(cur, "y", 0))
        line = screen.buffer.get(cy)
        if line:
            t = _row_to_str(screen, line).rstrip()
            if t.strip():
                return t
    disp = getattr(screen, "display", None)
    if disp:
        for row_str in reversed(disp):
            rt = row_str.rstrip("\n").rstrip()
            if rt.strip():
                return rt
    return ""


def _flatten_scrollback(screen: HistoryScreen) -> list[str]:
    rows: list[str] = []
    for line_dict in screen.history.top:
        rows.append(_row_to_str(screen, line_dict))
    rows.extend(screen.display)
    return rows


def _strip_blank_edges(lines: Iterable[str]) -> str:
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    return "\n".join(body)


def _norm_cmd_line(s: str) -> str:
    """Collapse whitespace so OSC preexec text matches model-supplied lines."""
    return " ".join((s or "").strip().split())


def _commands_match(expected: str, got: str) -> bool:
    a = _norm_cmd_line(expected)
    b = _norm_cmd_line(got)
    if not a:
        return True
    if not b:
        return False
    return a == b or b.endswith(a) or a in b or b in a


# Lines that only launch another interactive shell — must stay in sync with
# ``hooks/shellclaw_shell.sh`` / ``shellclaw.fish`` (synthetic OSC END + UI).
_NESTED_SHELL_EXACT: frozenset[str] = frozenset(
    {
        "bash",
        "bash -l",
        "bash --login",
        "bash -i",
        "bash --norc",
        "exec bash",
        "exec bash -l",
        "exec bash --login",
        "exec bash -i",
        "command bash",
        "command bash -l",
        "/bin/bash",
        "/usr/bin/bash",
        "/usr/local/bin/bash",
        "env bash",
        "/usr/bin/env bash",
        "zsh",
        "zsh -l",
        "zsh -i",
        "zsh --login",
        "exec zsh",
        "exec zsh -l",
        "command zsh",
        "/bin/zsh",
        "/usr/bin/zsh",
        "/usr/local/bin/zsh",
        "sh",
        "/bin/sh",
        "/usr/bin/sh",
        "dash",
        "/bin/dash",
        "/usr/bin/dash",
        "fish",
        "/usr/bin/fish",
        "/bin/fish",
        "ksh",
        "mksh",
        "/bin/ksh",
        "exec sh",
        "exec dash",
        "exec fish",
    }
)


def is_nested_interactive_shell_command(cmd: str) -> bool:
    """True if *cmd* only starts another interactive shell (plain bash/zsh, etc.).

    Used for the nested-shell notice and Stop-button heuristics; plain bash/zsh
    are re-run with session hooks so history still captures when switching shells.
    """
    t = _norm_cmd_line(cmd)
    if not t:
        return False
    if any(x in t for x in ("-c", "&&", "||", "|", ";", '"')):
        return False
    return t in _NESTED_SHELL_EXACT


def _osc_segment_output(screen: TrackedHistoryScreen, start_idx: int | None) -> str:
    """Visible output for the current command segment (between OSC START and END)."""
    flat = _flatten_scrollback(screen)
    if screen.in_alt_screen:
        return "\n".join(screen.display).rstrip()
    if start_idx is None:
        return ""
    if start_idx >= len(flat):
        return _strip_blank_edges(screen.display)
    return _strip_blank_edges(flat[start_idx:])


class TrackedHistoryScreen(HistoryScreen):
    """HistoryScreen that tracks DEC alternate screen (mode 1049)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.in_alt_screen = False

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        super().set_mode(*modes, **kwargs)
        if kwargs.get("private") and 1049 in modes:
            self.in_alt_screen = True

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        super().reset_mode(*modes, **kwargs)
        if kwargs.get("private") and 1049 in modes:
            self.in_alt_screen = False


class OscInterceptor:
    """Strip shellclaw OSC markers, feed pyte, segment commands."""

    def __init__(
        self,
        screen: TrackedHistoryScreen,
        stream: ByteStream,
        *,
        on_command_start: Callable[[str], None] | None = None,
        on_command_complete: Callable[[CommandEvent], None] | None = None,
    ) -> None:
        self._screen = screen
        self._stream = stream
        self._on_command_start = on_command_start
        self._on_command_complete = on_command_complete
        self._carry = b""
        self._raw_buf: list[bytes] = []
        self._osc_depth: int = 0          # nesting counter for subshell START/END pairs
        self._seg_start_idx: int | None = None
        self._context_before: str = ""
        self._pending_cmd: str | None = None
        self._nested_interactive_shell_session: bool = False

    def set_callbacks(
        self,
        on_command_start: Callable[[str], None] | None,
        on_command_complete: Callable[[CommandEvent], None] | None,
    ) -> None:
        self._on_command_start = on_command_start
        self._on_command_complete = on_command_complete

    def command_active(self) -> bool:
        """True between shellclaw OSC START and END for the current shell line."""
        return self._pending_cmd is not None

    @property
    def nested_interactive_shell_session(self) -> bool:
        """True while the outermost START line was only ``bash``/``zsh``/… (hooks inactive inside)."""
        return self._nested_interactive_shell_session

    def _feed_pyte(self, data: bytes) -> None:
        if data:
            if self._pending_cmd is not None:
                self._raw_buf.append(data)
            self._stream.feed(data)

    def _flush_carry_to_buf(self) -> None:
        """Move any bytes still in ``_carry`` into ``_raw_buf`` before draining.

        ``_carry`` holds the tail of the last chunk that *might* be an
        incomplete OSC sequence.  On ``force_command_end`` (timeout / interrupt)
        these bytes would otherwise be silently lost; we flush them so the
        caller gets the most complete output possible.  The sanitiser will
        strip any resulting incomplete escape fragment.
        """
        if self._carry and self._pending_cmd is not None:
            self._raw_buf.append(self._carry)
            self._carry = b""

    def _drain_raw_buf(self) -> str:
        """Decode and clear the raw PTY byte buffer accumulated since START."""
        raw = b"".join(self._raw_buf)
        self._raw_buf = []
        return raw.decode("utf-8", errors="replace")

    def feed_raw(self, data: bytes) -> None:
        buf = self._carry + data
        self._carry = b""
        while buf:
            m_start = START_RE.search(buf)
            m_end = END_RE.search(buf)
            if not m_start and not m_end:
                tail = self._osc_safe_tail(buf)
                self._feed_pyte(buf[: len(buf) - len(tail)])
                self._carry = tail
                return
            pos_s = m_start.start() if m_start else len(buf) + 1
            pos_e = m_end.start() if m_end else len(buf) + 1
            if m_start and pos_s <= pos_e:
                self._feed_pyte(buf[:pos_s])
                cmd = m_start.group(1).decode("utf-8", errors="replace")
                self._handle_start(cmd)
                buf = buf[m_start.end() :]
                continue
            if m_end:
                self._feed_pyte(buf[:pos_e])
                try:
                    exit_code = int(m_end.group(1))
                except ValueError:
                    exit_code = -1
                self._handle_end(exit_code)
                buf = buf[m_end.end() :]
                continue
            tail = self._osc_safe_tail(buf)
            self._feed_pyte(buf[: len(buf) - len(tail)])
            self._carry = tail
            return

    def _osc_safe_tail(self, data: bytes) -> bytes:
        if b"\x1b]" not in data:
            return b""
        idx = data.rfind(b"\x1b]")
        frag = data[idx:]
        if len(frag) > 4096:
            return frag[-2048:]
        return frag

    def _handle_start(self, cmd: str) -> None:
        if self._osc_depth == 0:
            # Outermost command start — initialise capture state.
            flat = _flatten_scrollback(self._screen)
            self._seg_start_idx = len(flat)
            self._context_before = "\n".join(flat[-5:]) if flat else ""
            self._pending_cmd = cmd
            self._raw_buf = []  # fresh buffer for this command's output
            self._nested_interactive_shell_session = is_nested_interactive_shell_command(cmd)
            if self._on_command_start:
                self._on_command_start(cmd)
        # Always increment so nested subshell END markers are ignored.
        self._osc_depth += 1

    def _handle_end(self, exit_code: int) -> None:
        if self._osc_depth > 0:
            self._osc_depth -= 1
        if self._osc_depth > 0:
            # Still inside a nested subshell — absorb this END and keep capturing.
            return
        if self._pending_cmd is None:
            # Stray END (e.g. shell hook after we already force-completed on timeout).
            return
        cmd = self._pending_cmd
        self._pending_cmd = None
        self._seg_start_idx = None
        # Alt-screen programs (vim, htop, …) own the full display; raw bytes are
        # not meaningful plain text, so fall back to the pyte viewport.
        if self._screen.in_alt_screen:
            output = "\n".join(self._screen.display).rstrip()
        else:
            output = self._drain_raw_buf()
        ev = CommandEvent(
            command=cmd,
            output=output,
            exit_code=exit_code,
            context_before=self._context_before,
        )
        self._context_before = ""
        if self._on_command_complete:
            self._on_command_complete(ev)

    def force_command_end(self, exit_code: int) -> bool:
        """Finalize the in-flight command as if OSC END arrived (shell hook missing).

        Returns True if a command was pending and completion was emitted.
        """
        if self._pending_cmd is None:
            return False
        cmd = self._pending_cmd
        self._osc_depth = 0  # reset nesting — we are forcibly ending the outer command
        self._nested_interactive_shell_session = False
        if self._screen.in_alt_screen:
            self._pending_cmd = None
            self._seg_start_idx = None
            output = "\n".join(self._screen.display).rstrip()
        else:
            # Flush carry BEFORE clearing _pending_cmd so the guard in
            # _flush_carry_to_buf still sees an active command.
            self._flush_carry_to_buf()
            self._pending_cmd = None
            self._seg_start_idx = None
            output = self._drain_raw_buf()
        ev = CommandEvent(
            command=cmd,
            output=output,
            exit_code=exit_code,
            context_before=self._context_before,
        )
        self._context_before = ""
        if self._on_command_complete:
            self._on_command_complete(ev)
        return True


_HEX6 = re.compile(r"^[0-9a-fA-F]{6}$")
_HEX3 = re.compile(r"^[0-9a-fA-F]{3}$")


def _normalize_pyte_color_for_rich(color: str) -> str | None:
    """Map pyte SGR color strings to values :class:`rich.style.Style` accepts."""
    if not color or color == "default":
        return None
    c = color.strip()
    if _HEX6.fullmatch(c):
        return "#" + c.lower()
    if _HEX3.fullmatch(c):
        low = c.lower()
        return "#" + "".join(ch * 2 for ch in low)
    if c.startswith("#"):
        return c
    # pyte may emit ``rgb:RR/GG/BB`` (xterm); Rich wants ``#rrggbb`` or ``rgb(r,g,b)``
    if c.startswith("rgb:") and c.count("/") == 2:
        parts = c[4:].split("/")
        try:
            r, g, b = (int(p, 16) for p in parts)
            return f"#{r:02x}{g:02x}{b:02x}"
        except ValueError:
            return None
    return c


def _char_style(ch: Any) -> Style:
    if not isinstance(ch, Char):
        return Style()
    kwargs: dict[str, Any] = {}
    c_fg = _normalize_pyte_color_for_rich(ch.fg)
    c_bg = _normalize_pyte_color_for_rich(ch.bg)
    if c_fg:
        kwargs["color"] = c_fg
    if c_bg:
        kwargs["bgcolor"] = c_bg
    if ch.bold:
        kwargs["bold"] = True
    if ch.italics:
        kwargs["italic"] = True
    if ch.underscore:
        kwargs["underline"] = True
    if ch.strikethrough:
        kwargs["strike"] = True
    if ch.reverse:
        kwargs["reverse"] = True
    if ch.blink:
        kwargs["blink"] = True
    if not kwargs:
        return Style()
    try:
        return Style(**kwargs)
    except ColorParseError:
        # Drop colors; keep attributes so a bad pyte string never kills the TUI.
        safe = {k: v for k, v in kwargs.items() if k not in ("color", "bgcolor")}
        return Style(**safe) if safe else Style()


# Cap rows rendered into the TUI (pyte history can be thousands of lines).
_MAX_TERM_SCROLLBACK_ROWS = 8000


def _append_buffer_row_rich(
    screen: TrackedHistoryScreen,
    line: dict[int, Any] | None,
    out: Text,
) -> None:
    """Append one screen row from a pyte buffer line dict to *out* (no trailing newline)."""
    if line is None:
        return
    last: Style | None = None
    buf_chars: list[str] = []
    for x in range(screen.columns):
        ch = line.get(x, screen.default_char)
        st = _char_style(ch)
        if last is not None and st != last and buf_chars:
            out.append("".join(buf_chars), style=last)
            buf_chars = []
        last = st
        buf_chars.append(_char_data(ch))
    if buf_chars:
        out.append("".join(buf_chars), style=last or Style())


def _append_buffer_row_rich_password_mask(
    screen: TrackedHistoryScreen,
    line: dict[int, Any] | None,
    out: Text,
    *,
    mask_start_x: int,
    mask_len: int,
) -> None:
    """Like ``_append_buffer_row_rich`` but draws ``*`` over ``[mask_start_x, mask_start_x+mask_len)``."""
    if line is None or mask_len <= 0:
        _append_buffer_row_rich(screen, line, out)
        return
    cols = screen.columns
    sx = max(0, min(cols, mask_start_x))
    ex = min(cols, sx + mask_len)
    last: Style | None = None
    buf_chars: list[str] = []
    for x in range(cols):
        if sx <= x < ex:
            if buf_chars:
                out.append("".join(buf_chars), style=last or Style())
                buf_chars = []
                last = None
            out.append("*", style=Style())
            continue
        ch = line.get(x, screen.default_char)
        st = _char_style(ch)
        if last is not None and st != last and buf_chars:
            out.append("".join(buf_chars), style=last)
            buf_chars = []
        last = st
        buf_chars.append(_char_data(ch))
    if buf_chars:
        out.append("".join(buf_chars), style=last or Style())


def build_screen_rich(screen: TrackedHistoryScreen) -> Text:
    """Render visible pyte buffer only (viewport ``screen.lines`` rows)."""
    out = Text()
    for y in range(screen.lines):
        if y:
            out.append("\n")
        _append_buffer_row_rich(screen, screen.buffer.get(y), out)
    return out


def build_full_buffer_rich(
    screen: TrackedHistoryScreen,
    *,
    max_rows: int = _MAX_TERM_SCROLLBACK_ROWS,
    password_mask: tuple[int, int, int] | None = None,
) -> Text:
    """History + current buffer as one ``Text`` for a scrollable terminal view.

    *password_mask*, when set, is ``(global_row_index, start_column, length)`` for
    one row where typed characters should appear as ``*`` (password prompts).
    """
    rows: list[dict[int, Any] | None] = []
    for line_dict in screen.history.top:
        rows.append(line_dict)
    for y in range(screen.lines):
        rows.append(screen.buffer.get(y))
    mask_row: int | None = None
    mask_sx = 0
    mask_ln = 0
    if password_mask is not None:
        mask_row, mask_sx, mask_ln = password_mask
    if len(rows) > max_rows:
        dropped = len(rows) - max_rows
        rows = rows[-max_rows:]
        if mask_row is not None and mask_ln > 0:
            mask_row -= dropped
            if mask_row < 0 or mask_row >= len(rows):
                mask_row, mask_sx, mask_ln = None, 0, 0
    out = Text()
    for i, line in enumerate(rows):
        if i:
            out.append("\n")
        if mask_row is not None and mask_ln > 0 and i == mask_row:
            _append_buffer_row_rich_password_mask(
                screen, line, out, mask_start_x=mask_sx, mask_len=mask_ln
            )
        else:
            _append_buffer_row_rich(screen, line, out)
    return out


def _child_exec_shell(
    kind: str,
    shell_exe: str,
    bash_rc: Path,
    zdot: Path,
) -> None:
    os.environ["TERM"] = "xterm-256color"
    if kind == "fish":
        fish_hook = _hooks_package_dir() / "shellclaw.fish"
        os.execl(shell_exe, "fish", "-i", "-C", f"source {shlex.quote(str(fish_hook))}")
    if kind == "zsh":
        os.environ["ZDOTDIR"] = str(zdot)
        zsh = shutil.which("zsh") or shell_exe
        os.execl(zsh, "zsh", "-i")
    bash = shutil.which("bash") or "/bin/bash"
    os.execl(bash, "bash", "--rcfile", str(bash_rc), "-i")


class PtyEmulator:
    """One interactive shell on a PTY, pyte-backed, with optional inject/wait."""

    def __init__(
        self,
        rows: int,
        cols: int,
        *,
        on_command_start: Callable[[str], None] | None = None,
        on_command_complete: Callable[[CommandEvent], None] | None = None,
    ) -> None:
        self._rows = max(rows, 4)
        self._cols = max(cols, 20)
        self._session_dir = tempfile.mkdtemp(prefix="shellclaw_pty_")
        self._child_pid: int | None = None
        self._master_fd: int | None = None
        self._screen = TrackedHistoryScreen(self._cols, self._rows, history=5000)
        self._stream = ByteStream(self._screen, strict=False)
        self._external_on_complete = on_command_complete
        self._interceptor = OscInterceptor(
            self._screen,
            self._stream,
            on_command_start=on_command_start,
            on_command_complete=self._dispatch_command_event,
        )
        self._inject_lock = asyncio.Lock()
        self._inject_waiter: asyncio.Future[CommandEvent] | None = None
        self._inject_expected: str | None = None
        self._inject_source: SourceKind = "unknown"
        self._loop: asyncio.AbstractEventLoop | None = None
        self._reader_registered = False
        self._dirty = True
        self._closed = False
        self._pwd_mode = False
        self._pwd_mask_len = 0

    @property
    def interceptor(self) -> OscInterceptor:
        return self._interceptor

    @property
    def screen(self) -> TrackedHistoryScreen:
        return self._screen

    def command_active(self) -> bool:
        return self._interceptor.command_active()

    @property
    def nested_interactive_shell_session(self) -> bool:
        return self._interceptor.nested_interactive_shell_session

    def stop_button_visible(self) -> bool:
        """Whether the Stop control should show (hide during nested bash/zsh, etc.)."""
        return self.command_active() and not self.nested_interactive_shell_session

    def send_interrupt(self) -> None:
        """Send INTR (^C) to the PTY session (foreground job), same as Ctrl+C in a real terminal."""
        self.send_bytes(b"\x03")

    def _clear_password_mask_state(self) -> None:
        self._pwd_mode = False
        self._pwd_mask_len = 0

    def _sync_password_prompt_state(self) -> None:
        if self._screen.in_alt_screen:
            self._clear_password_mask_state()
            return

        probe = probe_line_for_password_prompt(self._screen)
        matched = bool(probe) and line_suggests_password_prompt(probe)

        if matched:
            was = self._pwd_mode
            self._pwd_mode = True
            if not was:
                self._pwd_mask_len = 0
        else:
            self._clear_password_mask_state()

    def password_prompt_active(self) -> bool:
        """True while the PTY viewport looks like a password / PIN prompt line."""
        return self._pwd_mode

    def _password_mask_overlay_tuple(self) -> tuple[int, int, int] | None:
        if not self._pwd_mode or self._pwd_mask_len <= 0 or self._screen.in_alt_screen:
            return None
        cur = getattr(self._screen, "cursor", None)
        if cur is None:
            return None
        cy = int(getattr(cur, "y", 0))
        cx = int(getattr(cur, "x", 0))
        gri = len(self._screen.history.top) + cy
        return (gri, cx, self._pwd_mask_len)

    def password_mask_on_printable(self, character: str) -> None:
        if not self._pwd_mode or not character:
            return
        self._pwd_mask_len += len(character)
        self.mark_dirty()

    def password_mask_on_backspace(self) -> None:
        if not self._pwd_mode or self._pwd_mask_len <= 0:
            return
        self._pwd_mask_len -= 1
        self.mark_dirty()

    def password_mask_on_enter(self) -> None:
        if not self._pwd_mode:
            return
        self._pwd_mask_len = 0
        self.mark_dirty()

    def set_callbacks(
        self,
        on_command_start: Callable[[str], None] | None,
        on_command_complete: Callable[[CommandEvent], None] | None,
    ) -> None:
        self._interceptor.set_callbacks(on_command_start, self._dispatch_command_event)
        self._external_on_complete = on_command_complete

    def _dispatch_command_event(self, ev: CommandEvent) -> None:
        out = ev
        if self._inject_waiter is not None and not self._inject_waiter.done():
            exp = self._inject_expected
            if exp is not None:
                matched = _commands_match(exp, ev.command)
                # Agent inject uses a single shared PTY: the OSC END after our write always
                # closes that segment, but zsh preexec's cmd= payload can differ from the exact
                # bytes we wrote (wrapping, rare charset edge cases). If we only accepted an
                # exact _commands_match, the inject future would never resolve and run_safe would
                # hit timeout_seconds then SIGINT despite a finished shell command.
                if matched or self._inject_source == "agent":
                    out = CommandEvent(
                        command=ev.command,
                        output=ev.output,
                        exit_code=ev.exit_code,
                        context_before=ev.context_before,
                        source=self._inject_source,
                    )
                    self._inject_waiter.set_result(out)
                    self._inject_waiter = None
                    self._inject_expected = None
                    self._inject_source = "unknown"
        if self._external_on_complete:
            self._external_on_complete(out)

    def build_rich_text(self) -> Text:
        """Full scrollback + viewport (for a scrollable UI); capped for performance."""
        return build_full_buffer_rich(
            self._screen, password_mask=self._password_mask_overlay_tuple()
        )

    def extract_selected_text(
        self,
        row_start: int,
        col_start: int,
        row_end: int,
        col_end: int,
    ) -> str:
        """Return plain text for the cell range [row_start,col_start]..[row_end,col_end].

        Coordinates are 0-indexed display rows in the same order as
        ``build_full_buffer_rich`` (scrollback history first, then the
        visible viewport).  Automatically normalises reversed selections.
        """
        if (row_start, col_start) > (row_end, col_end):
            row_start, col_start, row_end, col_end = row_end, col_end, row_start, col_start

        rows: list[dict[int, Any] | None] = list(self._screen.history.top)
        for y in range(self._screen.lines):
            rows.append(self._screen.buffer.get(y))
        if len(rows) > _MAX_TERM_SCROLLBACK_ROWS:
            rows = rows[-_MAX_TERM_SCROLLBACK_ROWS:]

        def _row_text(row: int) -> str:
            if row < 0 or row >= len(rows):
                return ""
            line = rows[row]
            if line is None:
                return ""
            buf: list[str] = []
            for x in range(self._screen.columns):
                ch = line.get(x, self._screen.default_char)
                buf.append(_char_data(ch))
            return "".join(buf)

        if row_start == row_end:
            return _row_text(row_start)[col_start : col_end + 1].rstrip()

        parts: list[str] = []
        parts.append(_row_text(row_start)[col_start:].rstrip())
        for r in range(row_start + 1, row_end):
            parts.append(_row_text(r).rstrip())
        parts.append(_row_text(row_end)[: col_end + 1].rstrip())
        return "\n".join(parts)

    def get_snapshot(self) -> dict[str, str]:
        content = "\n".join(self._screen.display).rstrip()
        mode = "interactive" if self._screen.in_alt_screen else "normal"
        return {"mode": mode, "content": content}

    def mark_dirty(self) -> None:
        self._dirty = True

    def take_dirty(self) -> bool:
        d = self._dirty
        self._dirty = False
        return d

    def attach_reader(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._master_fd is None or self._reader_registered:
            return
        self._loop = loop
        loop.add_reader(self._master_fd, self._sync_readable)
        self._reader_registered = True

    def detach_reader(self) -> None:
        if not self._reader_registered or self._master_fd is None or self._loop is None:
            return
        try:
            self._loop.remove_reader(self._master_fd)
        except Exception:
            pass
        self._reader_registered = False

    def _sync_readable(self) -> None:
        if self._master_fd is None:
            return
        try:
            chunk = os.read(self._master_fd, 65536)
        except OSError as exc:
            if exc.errno in (errno.EIO, errno.EAGAIN):
                chunk = b""
            else:
                raise
        if not chunk:
            self.detach_reader()
            return
        self._interceptor.feed_raw(chunk)
        self._sync_password_prompt_state()
        self._dirty = True

    def _set_winsize(self, fd: int, rows: int, cols: int) -> None:
        wins = struct.pack("HHHH", rows, cols, 0, 0)
        try:
            fcntl.ioctl(fd, termios.TIOCSWINSZ, wins)
        except OSError:
            pass

    def start(self) -> None:
        if self._child_pid is not None:
            return
        kind, shell_exe = _detect_shell()
        bash_rc, zdot = _write_session_hooks(Path(self._session_dir))

        pid, master_fd = pty.fork()
        if pid == 0:
            try:
                _child_exec_shell(kind, shell_exe, bash_rc, zdot)
            except Exception:
                os._exit(127)
        self._child_pid = pid
        self._master_fd = master_fd
        os.set_blocking(master_fd, False)
        self._set_winsize(master_fd, self._rows, self._cols)

    def resize(self, rows: int, cols: int) -> None:
        self._rows = max(rows, 4)
        self._cols = max(cols, 20)
        self._screen.resize(lines=self._rows, columns=self._cols)
        if self._master_fd is not None:
            self._set_winsize(self._master_fd, self._rows, self._cols)
            if self._child_pid:
                try:
                    os.kill(self._child_pid, signal.SIGWINCH)
                except (OSError, ProcessLookupError):
                    pass
        self._dirty = True

    def send_bytes(self, data: bytes) -> None:
        if self._master_fd is None or self._closed:
            return
        if b"\x03" in data:
            self._clear_password_mask_state()
        os.write(self._master_fd, data)

    @staticmethod
    def _command_event_with_timeout_note(ev: CommandEvent, timeout: int) -> CommandEvent:
        note = f"\n[command timed out after {timeout}s]"
        new_out = (ev.output.rstrip() + note) if ev.output.strip() else note.strip()
        return CommandEvent(
            command=ev.command,
            output=new_out,
            exit_code=ev.exit_code,
            context_before=ev.context_before,
            source=ev.source,
        )

    async def run_command_and_wait(
        self,
        cmd: str,
        *,
        timeout: int = 120,
        source: SourceKind = "agent",
    ) -> CommandEvent:
        if self._master_fd is None or self._closed:
            raise RuntimeError("PTY is not running")
        async with self._inject_lock:
            loop = asyncio.get_running_loop()
            fut: asyncio.Future[CommandEvent] = loop.create_future()
            self._inject_waiter = fut
            self._inject_expected = cmd.strip()
            self._inject_source = source
            line = (cmd.rstrip("\r\n") + "\n").encode("utf-8", errors="replace")
            intr_exit = 128 + signal.SIGINT
            try:
                await asyncio.to_thread(os.write, self._master_fd, line)
                soft_timeout = False
                try:
                    ev = await asyncio.wait_for(fut, timeout=timeout)
                except asyncio.TimeoutError:
                    soft_timeout = True
                    grace = min(30, max(4, timeout // 2))
                    self.send_interrupt()
                    try:
                        ev = await asyncio.wait_for(fut, timeout=grace)
                    except asyncio.TimeoutError:
                        if not self._interceptor.force_command_end(intr_exit):
                            syn = CommandEvent(
                                command=(self._inject_expected or "").strip(),
                                output="\n".join(self._screen.display).rstrip(),
                                exit_code=intr_exit,
                                context_before="",
                            )
                            self._dispatch_command_event(syn)
                        if not fut.done():
                            raise RuntimeError(
                                "PTY inject waiter not resolved after timeout recovery"
                            )
                        ev = fut.result()
                if soft_timeout:
                    ev = self._command_event_with_timeout_note(ev, timeout)
                return ev
            except BaseException:
                if self._inject_waiter is not None and not self._inject_waiter.done():
                    self._inject_waiter.cancel()
                raise
            finally:
                self._inject_waiter = None
                self._inject_expected = None
                self._inject_source = "unknown"

    def kill(self) -> None:
        self.detach_reader()
        if self._master_fd is not None:
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = None
        if self._child_pid:
            try:
                os.kill(self._child_pid, signal.SIGTERM)
            except (OSError, ProcessLookupError):
                pass
            try:
                os.waitpid(self._child_pid, 0)
            except (OSError, ChildProcessError):
                pass
            self._child_pid = None
        if os.path.isdir(self._session_dir):
            shutil.rmtree(self._session_dir, ignore_errors=True)
        self._closed = True
