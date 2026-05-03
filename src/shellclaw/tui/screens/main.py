"""Main two-panel screen.

Layout:
  Header  (provider / model / version)
  HealthBanner
  +-- Reasoning trace (left, fixed height) — model thinking stream when supported
  +-- Conversation panel (left)  — chat + inline proposed actions
  +-- Terminal panel (right)     — command runner + agent output
  Input row (Clear F3 / Send F2 or Stop F2 · terminal Clear F4 / Send F2 / Stop F5)
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from textual import events, on, work
from textual.worker import Worker
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import Screen
from textual.widget import Widget
from textual.widgets import Static, TextArea


class HeaderSettingsPressed(Message):
    """Posted when the user clicks the compact Settings link in the header."""


class _HeaderSettingsLink(Static):
    """Single-line header control; Textual Buttons reserve ~3 rows of height."""

    DEFAULT_CSS = """
    _HeaderSettingsLink {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
        color: $text;
        background: transparent;
        content-align: center middle;
    }
    _HeaderSettingsLink:hover {
        background: $primary-darken-1;
        text-style: underline;
    }
    """

    def __init__(self) -> None:
        super().__init__(r"\[Settings]", id="settings-btn")

    def on_click(self) -> None:
        self.post_message(HeaderSettingsPressed())


class HeaderSimpleXPressed(Message):
    """Posted when the user clicks the SimpleX header link."""


class SimplexUserMessage(Message):
    """Inbound user text from the SimpleX bridge (same as typing in the chat bar)."""

    bubble = True

    def __init__(self, text: str) -> None:
        super().__init__()
        self.text = text


class _HeaderSimpleXLink(Static):
    """Toggle SimpleX bridge; opens first-time setup when ``chat_ref`` is unset."""

    DEFAULT_CSS = """
    _HeaderSimpleXLink {
        width: auto;
        height: 1;
        padding: 0 1;
        margin-right: 1;
        color: $text-muted;
        background: transparent;
        content-align: center middle;
    }
    _HeaderSimpleXLink.-on {
        color: $success;
        text-style: bold;
    }
    _HeaderSimpleXLink:hover {
        background: $primary-darken-1;
        text-style: underline;
    }
    """

    def __init__(self) -> None:
        super().__init__(r"\[SimpleX]", id="simplex-header-btn")

    def on_click(self) -> None:
        self.post_message(HeaderSimpleXPressed())

    def refresh_label(self, *, running: bool, configured: bool) -> None:
        if running:
            self.update(r"\[SimpleX on]")
            self.add_class("-on")
        elif configured:
            self.update(r"\[SimpleX off]")
            self.remove_class("-on")
        else:
            self.update(r"\[SimpleX setup]")
            self.remove_class("-on")


from ... import __version__
from ...agent.loop import (
    AgentLoop,
    EventDone,
    EventError,
    EventReasoning,
    EventThinking,
    EventToolOutput,
    EventToolResult,
    EventToolStart,
)
from ...agent.terminal_history import TerminalHistoryStore
from ...config import AppConfig, load_config, simplex_database_prefix, terminal_log_path
from ...pty_emulator import CommandEvent
from ...pty_sanitize import sanitize_pty_command_output
from ...safety.sandbox import format_shell_output
from ...health.snapshot import run_health_snapshot
from ...session.hardware import get_or_refresh_profile
from ...session.store import SessionStore
from ...simplex.bridge import SimpleXBridge, SimpleXError
from ...simplex.protocol import normalize_chat_ref
from ..clipboard import copy_and_notify
from ..widgets.chat import ChatWidget, CopyToTerminal, ExecuteAction, ExplainAction
from ..widgets.health_banner import HealthBanner
from ..widgets.reasoning import ReasoningTrace
from ..widgets.input_chrome import (
    CHAT_CLEAR_LABEL,
    ChromeAction,
    ChromePressed,
    SendStopChrome,
)
from ..widgets.nested_shell_notice import NestedShellInfoModal
from ..widgets.terminal import (
    EmbeddedTerminal,
    NestedShellSessionStarted,
    TerminalUserCommand,
    TerminalWidget,
)


class SessionQueryTextArea(TextArea):
    """Multiline user message; **F2** / **Ctrl+O** send; **F2** stops the agent when the bar is busy; **F3** clears session."""

    class Submitted(Message):
        bubble = True

        def __init__(self, value: str) -> None:
            super().__init__()
            self.value = value

    def __init__(self, *args: object, **kwargs: object) -> None:
        kwargs.setdefault("soft_wrap", True)
        kwargs.setdefault("tab_behavior", "focus")
        kwargs.setdefault("show_line_numbers", False)
        super().__init__(*args, **kwargs)

    def action_submit(self) -> None:
        self.post_message(self.Submitted(self.text))

    def key_f2(self) -> None:
        if self.disabled:
            cancel = getattr(self.screen, "cancel_agent_worker_if_running", None)
            if callable(cancel):
                cancel()
            return
        self.action_submit()

    def key_ctrl_o(self) -> None:
        self.action_submit()


class MainScreen(Screen):
    CSS_PATH = str(Path(__file__).parent / "main.tcss")

    BINDINGS = [
        Binding("ctrl+u", "show_undo_log", "Undo log", show=True),
        Binding("f3", "chat_clear", show=False),
        Binding("f4", "terminal_clear", show=False),
        Binding("f5", "terminal_stop", show=False),
    ]

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self._config = config
        self._terminal_log = TerminalHistoryStore(log_path=terminal_log_path(config))
        self._loop = AgentLoop(config, terminal_log=self._terminal_log)
        self._store = SessionStore()
        self._session_id: int | None = None
        self._distro_info: str = ""
        self._hardware_profile: dict | None = None
        self._agent_worker: Worker | None = None
        self._sel_screen_start: tuple[int, int] | None = None
        self._simplex_bridge: SimpleXBridge | None = None

    def compose(self) -> ComposeResult:
        _model_disp = self._config.provider.model or "?"
        provider_label = (
            f"{self._config.provider.name} / {_model_disp}  v{__version__}"
        )

        with Horizontal(id="header-row"):
            yield Static(
                f"[bold]shellclaw[/bold]  [dim]{provider_label}[/dim]",
                id="header-title",
            )
            yield _HeaderSimpleXLink()
            yield _HeaderSettingsLink()
        yield HealthBanner(id="health-banner")

        with Horizontal(id="panels"):
            with Vertical(id="chat-side"):
                yield ReasoningTrace(id="reasoning")
                yield ChatWidget(id="chat")
                with Vertical(id="input-stack"):
                    with Horizontal(id="input-title-row"):
                        yield Static("", id="input-title-spacer")
                        with Horizontal(id="chat-actions-right"):
                            yield ChromeAction(
                                CHAT_CLEAR_LABEL,
                                target="chat",
                                action="clear",
                                id="chat-clear-btn",
                            )
                            yield SendStopChrome(target="chat", id="chat-send-or-stop")
                    yield SessionQueryTextArea(
                        placeholder="Message — Enter newline · F2/Ctrl+O send · F2 stop agent · F3 clear session  (Ctrl+X quit)",
                        id="input-bar",
                    )
            yield TerminalWidget(id="terminal")

    def on_mount(self) -> None:
        self._run_startup_tasks()
        self.call_after_refresh(self._sync_chat_input_chrome_idle)
        self.call_after_refresh(lambda: self.query_one("#input-bar", SessionQueryTextArea).focus())
        self.call_after_refresh(self._wire_terminal_pty)
        self.call_after_refresh(self._refresh_simplex_header)

    async def on_unmount(self) -> None:
        await self._simplex_stop()

    def _sync_chat_input_chrome_idle(self) -> None:
        """After layout mount, hide Stop until the agent run starts (``visible`` is reliable vs ``display``)."""
        self._set_chat_input_chrome_busy(False)

    def _wire_terminal_pty(self) -> None:
        self.query_one("#terminal", TerminalWidget).set_pty_complete_handler(
            self._on_pty_command_complete
        )

    def _refresh_simplex_header(self) -> None:
        if not self.is_mounted:
            return
        try:
            link = self.query_one("#simplex-header-btn", _HeaderSimpleXLink)
        except Exception:
            return
        configured = bool((self._config.simplex.chat_ref or "").strip())
        running = self._simplex_bridge is not None and self._simplex_bridge.is_running
        link.refresh_label(running=running, configured=configured)

    async def _simplex_stop(self) -> None:
        if self._simplex_bridge is not None:
            await self._simplex_bridge.stop()
            self._simplex_bridge = None
        self._refresh_simplex_header()

    async def _simplex_start(self) -> None:
        cref = normalize_chat_ref(self._config.simplex.chat_ref)
        if not cref:
            return
        if self._simplex_bridge is not None and self._simplex_bridge.is_running:
            return
        await self._simplex_stop()
        bridge = SimpleXBridge(
            database_prefix=simplex_database_prefix(self._config),
            port=self._config.simplex.port,
            executable=self._config.simplex.executable,
            chat_ref=cref,
            accept_any_chat=False,
            on_inbound_user_text=self._on_simplex_inbound_user_text,
        )
        try:
            await bridge.start()
        except SimpleXError as exc:
            self.app.notify(str(exc), title="SimpleX", severity="error")
            await bridge.stop()
            return
        except Exception as exc:
            self.app.notify(f"{type(exc).__name__}: {exc}", title="SimpleX", severity="error")
            await bridge.stop()
            return
        self._simplex_bridge = bridge
        self._refresh_simplex_header()

    async def _on_simplex_inbound_user_text(self, text: str) -> None:
        self.post_message(SimplexUserMessage(text))

    @on(SimplexUserMessage)
    async def _handle_simplex_user_message(self, event: SimplexUserMessage) -> None:
        w = self._agent_worker
        if w is not None and not w.is_finished:
            if self._simplex_bridge is not None and self._simplex_bridge.is_running:
                try:
                    await self._simplex_bridge.send_chat_text(
                        "shellclaw is busy with a previous message; try again shortly."
                    )
                except SimpleXError:
                    pass
            return
        await self._start_chat_agent(event.text, from_simplex=True)

    @on(HeaderSimpleXPressed)
    def _handle_header_simple_x_pressed(self, _event: HeaderSimpleXPressed) -> None:
        self.run_worker(self._toggle_simplex(), exclusive=False, thread=False)

    async def _toggle_simplex(self) -> None:
        if self._simplex_bridge is not None and self._simplex_bridge.is_running:
            await self._simplex_stop()
            return
        if not (self._config.simplex.chat_ref or "").strip():
            from .simplex_setup import SimplexSetupScreen

            self.app.push_screen(SimplexSetupScreen(self._config), self._on_simplex_setup_closed)
            return
        await self._simplex_start()

    def _on_simplex_setup_closed(self, result: object | None) -> None:
        self._config = load_config()
        self._refresh_simplex_header()
        from .simplex_setup import SimplexSetupResult

        if isinstance(result, SimplexSetupResult):
            self.run_worker(self._simplex_start(), exclusive=False, thread=False)

    async def _maybe_mirror_simplex(self, text: str) -> None:
        b = self._simplex_bridge
        if b is None or not b.is_running or not (text or "").strip():
            return
        try:
            await b.send_chat_text(text.strip())
        except SimpleXError:
            pass

    @work(exclusive=False)
    async def _run_startup_tasks(self) -> None:
        snapshot_task = asyncio.create_task(run_health_snapshot())
        profile_task = asyncio.create_task(get_or_refresh_profile())

        items = await snapshot_task
        self._hardware_profile = await profile_task

        banner = self.query_one("#health-banner", HealthBanner)
        banner.set_items(items)

        from ...agent.tools import dispatch

        self._distro_info = await dispatch("get_distro_info", {})

    async def on_session_query_text_area_submitted(
        self, event: SessionQueryTextArea.Submitted,
    ) -> None:
        await self._start_chat_agent(event.value.strip())

    async def _start_chat_agent(self, message: str, *, from_simplex: bool = False) -> None:
        if not message:
            return

        if not from_simplex:
            await self._simplex_stop()

        input_bar = self.query_one("#input-bar", SessionQueryTextArea)
        input_bar.text = ""
        input_bar.disabled = True
        self._set_chat_input_chrome_busy(True)

        chat = self.query_one("#chat", ChatWidget)
        chat.add_user(message)
        chat.begin_stream_wait()

        reasoning = self.query_one("#reasoning", ReasoningTrace)
        reasoning.clear_trace()

        self._session_id = self._store.create_session(message)
        self._agent_worker = self.run_worker(
            self._run_agent_job(message),
            exclusive=True,
            thread=False,
            name="shellclaw_agent",
        )

    async def on_chrome_pressed(self, event: ChromePressed) -> None:
        if event.target != "chat":
            return
        event.stop()
        match event.action:
            case "clear":
                self._clear_chat_and_session()
            case "send":
                bar = self.query_one("#input-bar", SessionQueryTextArea)
                if bar.disabled:
                    return
                await self._start_chat_agent(bar.text.strip())
            case "stop":
                w = self._agent_worker
                if w is not None and not w.is_finished:
                    w.cancel()

    def _on_pty_command_complete(self, ev: CommandEvent) -> None:
        """Shell finished a line in the PTY — record user runs; agent ``run_safe`` is handled via tools."""
        if ev.source != "agent":
            clean = sanitize_pty_command_output(ev.command, ev.output)
            text = format_shell_output(clean, ev.exit_code)
            self.post_message(TerminalUserCommand(ev.command, text))
            if self._session_id is not None:
                self._store.add_command(
                    self._session_id,
                    ev.command,
                    text,
                    exit_code=ev.exit_code,
                    cwd=None,
                )

    async def _run_agent_job(self, message: str) -> None:
        chat = self.query_one("#chat", ChatWidget)
        terminal = self.query_one("#terminal", TerminalWidget)
        input_bar = self.query_one("#input-bar", SessionQueryTextArea)
        reasoning = self.query_one("#reasoning", ReasoningTrace)

        streaming = False
        pty_shell_tool = False

        async def _pty_runner(cmd: str, timeout: int) -> tuple[str, int]:
            return await terminal.run_pty_command(cmd, timeout)

        def _terminal_screen_snapshot() -> dict[str, str]:
            return terminal.screen_snapshot()

        def _pty_stop() -> str:
            return terminal.stop_pty_interrupt()

        try:
            async for event in self._loop.run(
                user_message=message,
                distro_info=self._distro_info,
                hardware_profile=self._hardware_profile,
                pty_runner=_pty_runner,
                terminal_snapshot=_terminal_screen_snapshot,
                pty_stop=_pty_stop,
            ):
                if isinstance(event, EventThinking):
                    if not streaming:
                        chat.begin_stream()
                        streaming = True
                    chat.stream_token(event.text)

                elif isinstance(event, EventReasoning):
                    chat.end_stream_wait()
                    reasoning.append(event.text)

                elif isinstance(event, EventToolStart):
                    chat.end_stream_wait()
                    if streaming:
                        chat.flush_stream_before_tool()
                        streaming = False
                    pty_shell_tool = event.tool_name == "run_safe"
                    if not pty_shell_tool:
                        terminal.add_pending(
                            _tool_start_label(event.tool_name, event.arguments)
                        )
                        terminal.mount_live_output_block()

                elif isinstance(event, EventToolOutput):
                    if not pty_shell_tool:
                        terminal.append_live_line(event.line)

                elif isinstance(event, EventToolResult):
                    chat.add_tool_run(event.command)
                    if not pty_shell_tool:
                        terminal.add_command(event.command, event.output)
                    if self._session_id is not None:
                        self._store.add_command(
                            self._session_id,
                            event.command,
                            event.output,
                        )
                    chat.begin_stream_wait()

                elif isinstance(event, EventDone):
                    chat.end_stream_wait()
                    if streaming:
                        done_text = chat.finalize_stream(
                            total_tokens_hint=event.total_tokens,
                        )
                        streaming = False
                        if not done_text.strip() and event.total_tokens is None:
                            fb = (
                                "I finished using tools but did not produce a summary. "
                                "Ask a follow-up if you need details."
                            )
                            chat.add_assistant(fb)
                            await self._maybe_mirror_simplex(fb)
                        elif done_text.strip():
                            await self._maybe_mirror_simplex(done_text)
                    elif event.text.strip():
                        chat.add_assistant(
                            event.text,
                            total_tokens=event.total_tokens,
                        )
                        await self._maybe_mirror_simplex(event.text)
                    elif event.total_tokens is not None:
                        chat.add_assistant("", total_tokens=event.total_tokens)
                    else:
                        fb = (
                            "I finished using tools but did not produce a summary. "
                            "Ask a follow-up if you need details."
                        )
                        chat.add_assistant(fb)
                        await self._maybe_mirror_simplex(fb)

                elif isinstance(event, EventError):
                    chat.end_stream_wait()
                    if streaming:
                        chat.finalize_stream()
                        streaming = False
                    chat.add_error(event.message)
                    await self._maybe_mirror_simplex(f"[error] {event.message}")
                    # Recovery turn streams next; show wait until first token (like after tool).
                    chat.begin_stream_wait()
        except asyncio.CancelledError:
            chat.end_stream_wait()
            if streaming:
                chat.finalize_stream()
                streaming = False
            raise
        except Exception as exc:
            chat.end_stream_wait()
            if streaming:
                chat.finalize_stream()
                streaming = False
            err = f"{type(exc).__name__}: {exc}"
            chat.add_error(err)
            await self._maybe_mirror_simplex(f"[error] {err}")
        finally:
            chat.end_stream_wait()
            if streaming:
                chat.finalize_stream()
            self._agent_worker = None
            input_bar.disabled = False
            self._set_chat_input_chrome_busy(False)
            input_bar.focus()

    # --- Global text-selection → clipboard ---

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self._sel_screen_start = (event.screen_x, event.screen_y)

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if event.button != 1 or self._sel_screen_start is None:
            return
        start = self._sel_screen_start
        self._sel_screen_start = None
        end = (event.screen_x, event.screen_y)
        if start == end:
            return
        start_widget, _ = self.get_widget_at(*start)
        end_widget, _ = self.get_widget_at(*end)
        # The EmbeddedTerminal manages its own selection; skip it here.
        if _is_inside_embedded_terminal(start_widget) or _is_inside_embedded_terminal(
            end_widget
        ):
            return
        try:
            chat = self.query_one("#chat", ChatWidget)
        except Exception:
            return
        # Anchor the selection to the conversation bubble (a direct child of
        # ChatWidget), not the specific sub-widget under the cursor. Markdown
        # renders each paragraph as its own Static, so using the sub-widget's
        # region for offset math produces negative row indices and silently
        # copies the bubble's first line instead of the selected range.
        start_bubble = _find_chat_bubble(start_widget, chat)
        end_bubble = _find_chat_bubble(end_widget, chat)
        if start_bubble is None or start_bubble is not end_bubble:
            return
        text = _extract_visible_text(self, start_bubble, start, end)
        if text.strip():
            asyncio.get_running_loop().create_task(copy_and_notify(text, self.app))

    def _set_chat_input_chrome_busy(self, busy: bool) -> None:
        """Idle: Clear + Send. Busy: Stop only (Send slot becomes Stop; Clear hidden)."""
        if not self.is_mounted:
            return
        clear = self.query_one("#chat-clear-btn", ChromeAction)
        dual = self.query_one("#chat-send-or-stop", SendStopChrome)
        clear.visible = not busy
        dual.set_busy(busy)

    def _clear_chat_and_session(self) -> None:
        self.query_one("#chat", ChatWidget).clear()
        self.query_one("#terminal", TerminalWidget).clear_history()
        self.query_one("#reasoning", ReasoningTrace).clear_trace()
        self._loop.clear_history()
        self._terminal_log.clear()
        self._session_id = None

    def on_header_settings_pressed(self, _event: HeaderSettingsPressed) -> None:
        from .settings import SettingsScreen

        self.app.push_screen(SettingsScreen(), self._on_settings_closed)

    # --- Handle messages from inline action blocks ---

    def on_execute_action(self, event: ExecuteAction) -> None:
        """User clicked Execute on a proposed command."""
        self._run_proposed_command(event.command)

    @work(exclusive=False, thread=False)
    async def _run_proposed_command(self, command: str) -> None:
        terminal = self.query_one("#terminal", TerminalWidget)
        await terminal.run_shell_streaming(command, timeout=60.0)

    def on_terminal_user_command(self, event: TerminalUserCommand) -> None:
        """User ran a command in the side terminal (typed or Execute) — record for terminal history."""
        self._terminal_log.append_user(event.command, event.output)

    def on_nested_shell_session_started(self, event: NestedShellSessionStarted) -> None:
        """User launched bash/zsh/etc. from the hooked shell — explain model visibility limits."""
        self.app.push_screen(NestedShellInfoModal(event.command))

    def on_explain_action(self, event: ExplainAction) -> None:
        """User clicked Explain on a proposed command."""
        from ..widgets.explain import ExplainModal

        self.app.push_screen(ExplainModal(command=event.command, output=""))

    def on_copy_to_terminal(self, event: CopyToTerminal) -> None:
        """User clicked Copy-to-terminal — paste command into terminal input."""
        terminal = self.query_one("#terminal", TerminalWidget)
        terminal.prefill_input(event.command)

    # --- Health banner ---

    def on_health_banner_item_clicked(self, event: HealthBanner.ItemClicked) -> None:
        input_bar = self.query_one("#input-bar", SessionQueryTextArea)
        input_bar.text = event.prompt
        input_bar.focus()

    # --- Undo log ---

    def action_show_undo_log(self) -> None:
        from ..widgets.undo_log import UndoLogModal

        self.app.push_screen(UndoLogModal())

    def action_chat_clear(self) -> None:
        self._clear_chat_and_session()

    def action_terminal_clear(self) -> None:
        self.query_one("#terminal", TerminalWidget).clear_history()

    def cancel_agent_worker_if_running(self) -> None:
        """Called when **F2** is pressed on the disabled chat bar (Stop replaces Send)."""
        w = self._agent_worker
        if w is not None and not w.is_finished:
            w.cancel()

    def action_terminal_stop(self) -> None:
        """**F5** — interrupt the side terminal foreground command (same as Stop F5)."""
        tw = self.query_one("#terminal", TerminalWidget)
        dual = tw.query_one("#terminal-send-or-stop", SendStopChrome)
        if not dual.is_stop_mode:
            return
        tw.post_message(ChromePressed("terminal", "stop"))

    def _on_settings_closed(self, saved: bool | None) -> None:
        if not saved:
            return
        self._config = load_config()
        self._terminal_log.log_path = terminal_log_path(self._config)
        self._loop = AgentLoop(self._config, terminal_log=self._terminal_log)
        _model_disp = self._config.provider.model or "?"
        provider_label = (
            f"{self._config.provider.name} / {_model_disp}  v{__version__}"
        )
        self.query_one("#header-title", Static).update(
            f"[bold]shellclaw[/bold]  [dim]{provider_label}[/dim]"
        )
        self._refresh_simplex_header()


def _tool_start_label(tool_name: str, arguments: dict) -> str:
    match tool_name:
        case "run_safe":
            return f"Running: {arguments.get('cmd', '...')}"
        case "read_file":
            return f"Reading: {arguments.get('path', '...')}"
        case "list_dir":
            return f"Listing: {arguments.get('path', '...')}"
        case "disk_usage":
            return f"Checking disk usage: {arguments.get('path', '/')}"
        case "process_list":
            return "Checking running programs..."
        case "journal_logs":
            mode = arguments.get("mode", "")
            return f"Reading system logs ({mode or '…'})..."
        case "service_status":
            return f"Checking service: {arguments.get('unit', '...')}"
        case "network_info":
            return "Checking network connections..."
        case "get_distro_info":
            return "Detecting your Linux version..."
        case "web_search":
            return f"Searching the web: {arguments.get('query', '...')}"
        case "terminal_history_summary":
            return "Listing terminal history…"
        case "terminal_history_fetch":
            return "Fetching terminal history entry…"
        case "terminal_latest":
            return "Fetching latest terminal entry…"
        case _:
            return f"Running {tool_name}..."


def _find_chat_bubble(widget: object, chat: ChatWidget) -> Widget | None:
    """Walk up the widget tree until the parent is ``chat``.

    Returns the direct child of the conversation panel (the "bubble"), or
    ``None`` if *widget* is not inside the conversation at all. This gives a
    stable anchor that does not shift based on which internal sub-widget the
    mouse happens to land on.
    """
    w = widget
    while w is not None:
        parent = getattr(w, "parent", None)
        if parent is chat:
            return w if isinstance(w, Widget) else None
        w = parent
    return None


def _extract_visible_text(
    screen: Screen,
    bubble: Widget,
    start: tuple[int, int],
    end: tuple[int, int],
) -> str:
    """Return the plain text visible inside *bubble* between two drag points.

    We read already-composited lines from the screen compositor and crop each
    line by cell column. This gives exactly what the user sees (markdown
    styling flattened), works uniformly for leaf Static bubbles and container
    Markdown bubbles, and honours the bubble's horizontal bounds — so the
    selection cannot bleed into neighbouring widgets.
    """
    compositor = getattr(screen, "_compositor", None)
    if compositor is None:
        return ""
    visible = compositor.visible_widgets
    if bubble not in visible:
        return ""
    region, _clip = visible[bubble]

    # Normalise so (x0, y0) is the earlier point in reading order.
    (x0, y0), (x1, y1) = sorted([start, end], key=lambda p: (p[1], p[0]))
    y0 = max(y0, region.y)
    y1 = min(y1, region.bottom - 1)
    if y0 > y1:
        return ""

    try:
        strips = compositor.render_strips()
    except Exception:
        return ""

    left, right = region.x, region.right  # right is exclusive
    lines: list[str] = []
    for y in range(y0, y1 + 1):
        if not 0 <= y < len(strips):
            continue
        line_start = left
        line_end = right
        if y == y0:
            line_start = max(left, x0)
        if y == y1:
            line_end = min(right, x1 + 1)
        if line_start >= line_end:
            lines.append("")
            continue
        lines.append(strips[y].crop(line_start, line_end).text.rstrip())

    # Drop padding / empty rows at the outer edges, preserve interior blanks.
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _is_inside_embedded_terminal(widget: object) -> bool:
    """Return True if *widget* is or is a descendant of an EmbeddedTerminal."""
    w = widget
    while w is not None:
        if isinstance(w, EmbeddedTerminal):
            return True
        w = getattr(w, "parent", None)
    return False
