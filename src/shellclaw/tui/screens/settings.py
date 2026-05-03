"""Settings modal — edit ~/.config/shellclaw/config.toml from the TUI."""

from __future__ import annotations

from pydantic import ValidationError
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Checkbox, Input, Label, Static

from ...config import (
    AgentConfig,
    AppConfig,
    CONFIG_PATH,
    HealthConfig,
    ProviderConfig,
    SafetyConfig,
    SimplexConfig,
    UIConfig,
    load_config,
    save_config,
)


class SettingsScreen(ModalScreen[bool]):
    """Full-screen settings editor; dismiss(True) after save, False on cancel."""

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }

    #settings-container {
        width: 88;
        height: 85%;
        max-height: 40;
        border: round $primary;
        background: $panel;
    }

    #settings-title {
        background: $primary;
        color: $text;
        padding: 1 2;
        text-style: bold;
        dock: top;
    }

    #settings-scroll {
        height: 1fr;
        padding: 1 2;
    }

    .settings-section {
        text-style: bold;
        margin-top: 1;
        margin-bottom: 0;
    }

    .settings-section:first-of-type {
        margin-top: 0;
    }

    .settings-hint {
        color: $text-muted;
        height: auto;
        margin-bottom: 1;
    }

    .settings-row {
        height: 3;
        margin-bottom: 0;
    }

    .settings-row Input {
        width: 1fr;
    }

    /* Footer row height matches main input bar; do not set height: 100% on Buttons here —
       inside a docked Horizontal, % height can zero out the label region (no visible text). */
    #settings-footer {
        dock: bottom;
        height: 3;
        layout: horizontal;
        margin: 0;
        padding: 0 2;
        background: $surface;
        border-top: solid $primary-darken-2;
        align-vertical: middle;
    }

    #settings-status {
        width: 1fr;
        height: auto;
        padding: 0 1 0 0;
        content-align: left middle;
        color: $warning;
    }

    #btn-settings-cancel,
    #btn-settings-save {
        width: auto;
        min-width: 8;
        margin-left: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Close", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._cfg = load_config()

    def compose(self) -> ComposeResult:
        p, a, u, h, s, x = (
            self._cfg.provider,
            self._cfg.agent,
            self._cfg.ui,
            self._cfg.health,
            self._cfg.safety,
            self._cfg.simplex,
        )
        temp_s = "" if p.temperature is None else str(p.temperature)

        with Vertical(id="settings-container"):
            yield Static("Settings", id="settings-title")
            yield Static(
                f"Config file: [dim]{CONFIG_PATH}[/dim]",
                classes="settings-hint",
            )

            with ScrollableContainer(id="settings-scroll"):
                yield Static("Provider", classes="settings-section")
                yield Label("name (ollama, openai, anthropic, groq, openrouter, …)")
                with Horizontal(classes="settings-row"):
                    yield Input(p.name, id="prov-name")
                yield Label("model (required)")
                with Horizontal(classes="settings-row"):
                    yield Input(p.model, id="prov-model")
                yield Label("base_url")
                with Horizontal(classes="settings-row"):
                    yield Input(p.base_url, id="prov-base-url")
                yield Label("api_key (ignored for Ollama)")
                with Horizontal(classes="settings-row"):
                    yield Input(p.api_key, id="prov-api-key", password=True)
                yield Label("temperature (empty = provider default)")
                with Horizontal(classes="settings-row"):
                    yield Input(temp_s, id="prov-temperature")
                yield Label("num_ctx (Ollama only — context length in tokens)")
                with Horizontal(classes="settings-row"):
                    yield Input(str(p.num_ctx), id="prov-num-ctx")

                yield Static("Agent", classes="settings-section")
                yield Label("max_iterations")
                with Horizontal(classes="settings-row"):
                    yield Input(str(a.max_iterations), id="agent-max-iter")
                yield Label("max_output_bytes")
                with Horizontal(classes="settings-row"):
                    yield Input(str(a.max_output_bytes), id="agent-max-out")
                yield Label("timeout_seconds")
                with Horizontal(classes="settings-row"):
                    yield Input(str(a.timeout_seconds), id="agent-timeout")
                yield Checkbox(
                    "Advanced tool set (full schemas; for large models)",
                    value=a.advanced_toolset,
                    id="agent-advanced-toolset",
                )
                yield Checkbox(
                    "Auto Compress context ",
                    value=a.tool_context_compression,
                    id="agent-tool-compression",
                )

                yield Static("UI", classes="settings-section")
                yield Checkbox("Show raw terminal output", value=u.show_raw_output, id="ui-show-raw")
                yield Label("theme (dark | light)")
                with Horizontal(classes="settings-row"):
                    yield Input(u.theme, id="ui-theme")

                yield Static("Health", classes="settings-section")
                yield Checkbox(
                    "Run health snapshot when shellclaw opens",
                    value=h.snapshot_on_launch,
                    id="health-snapshot",
                )

                yield Static("Safety", classes="settings-section")
                yield Checkbox(
                    "Auto-backup files before modifying",
                    value=s.auto_backup,
                    id="safety-backup",
                )
                yield Label("backup_retention_days")
                with Horizontal(classes="settings-row"):
                    yield Input(str(s.backup_retention_days), id="safety-retention")
                yield Label("shellclaw check model (required)")
                with Horizontal(classes="settings-row"):
                    yield Input(s.check_model or "", id="safety-check-model")
                yield Label(
                    "Extra run_safe command basenames (comma-separated, e.g. btop, ncdu)"
                )
                with Horizontal(classes="settings-row"):
                    yield Input(
                        s.extra_allowed_command_bases or "",
                        id="safety-extra-allowed-bases",
                    )

                yield Static("SimpleX (phone bridge)", classes="settings-section")
                yield Static(
                    "Use [SimpleX] on the main screen for first-time /connect. "
                    "chat_ref is usually @1 after linking your phone.",
                    classes="settings-hint",
                )
                yield Label("simplex port (local WebSocket)")
                with Horizontal(classes="settings-row"):
                    yield Input(str(x.port), id="simplex-port")
                yield Label("chat_ref (e.g. @1) — leave empty until setup completes")
                with Horizontal(classes="settings-row"):
                    yield Input(x.chat_ref or "", id="simplex-chat-ref")
                yield Label("simplex database_dir (empty = ~/.local/share/shellclaw/simplex/simplex_v1)")
                with Horizontal(classes="settings-row"):
                    yield Input(x.database_dir or "", id="simplex-db-dir")
                yield Label("simplex executable")
                with Horizontal(classes="settings-row"):
                    yield Input(x.executable or "simplex-chat", id="simplex-exe")

            with Horizontal(id="settings-footer"):
                yield Static("", id="settings-status")
                yield Button("Cancel", id="btn-settings-cancel")
                yield Button("Save", variant="primary", id="btn-settings-save")

    def on_mount(self) -> None:
        self.query_one("#prov-name", Input).focus()

    def action_cancel(self) -> None:
        self.dismiss(False)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-settings-cancel":
            self.dismiss(False)
        elif event.button.id == "btn-settings-save":
            self._save()

    def _save(self) -> None:
        status = self.query_one("#settings-status", Static)
        status.update("")

        def _strip_input(widget_id: str) -> str:
            return self.query_one(f"#{widget_id}", Input).value.strip()

        try:
            temp_raw = _strip_input("prov-temperature")
            temperature: float | None = None
            if temp_raw:
                temperature = float(temp_raw)

            num_ctx_raw = _strip_input("prov-num-ctx")
            num_ctx = (
                int(num_ctx_raw)
                if num_ctx_raw
                else self._cfg.provider.num_ctx
            )

            port_raw = _strip_input("simplex-port")
            simplex_port = int(port_raw) if port_raw else self._cfg.simplex.port

            cfg = AppConfig(
                provider=ProviderConfig(
                    name=_strip_input("prov-name"),
                    model=_strip_input("prov-model"),
                    base_url=_strip_input("prov-base-url"),
                    api_key=_strip_input("prov-api-key"),
                    temperature=temperature,
                    num_ctx=num_ctx,
                ),
                agent=AgentConfig(
                    max_iterations=int(_strip_input("agent-max-iter")),
                    max_output_bytes=int(_strip_input("agent-max-out")),
                    timeout_seconds=int(_strip_input("agent-timeout")),
                    advanced_toolset=self.query_one(
                        "#agent-advanced-toolset", Checkbox
                    ).value,
                    tool_context_compression=self.query_one(
                        "#agent-tool-compression", Checkbox
                    ).value,
                ),
                ui=UIConfig(
                    show_raw_output=self.query_one("#ui-show-raw", Checkbox).value,
                    theme=_strip_input("ui-theme") or "dark",
                ),
                health=HealthConfig(
                    snapshot_on_launch=self.query_one("#health-snapshot", Checkbox).value,
                ),
                safety=SafetyConfig(
                    auto_backup=self.query_one("#safety-backup", Checkbox).value,
                    backup_retention_days=int(_strip_input("safety-retention")),
                    check_model=_strip_input("safety-check-model") or "",
                    extra_allowed_command_bases=_strip_input(
                        "safety-extra-allowed-bases"
                    ),
                ),
                debug=self._cfg.debug,
                simplex=SimplexConfig(
                    port=simplex_port,
                    chat_ref=_strip_input("simplex-chat-ref"),
                    database_dir=_strip_input("simplex-db-dir"),
                    executable=_strip_input("simplex-exe") or "simplex-chat",
                ),
            )
        except ValueError as exc:
            status.update(f"Invalid number: {exc}")
            return
        except ValidationError as exc:
            parts = []
            for err in exc.errors():
                loc = ".".join(str(x) for x in err.get("loc", ()))
                parts.append(f"{loc}: {err.get('msg', 'invalid')}")
            status.update("; ".join(parts) if parts else str(exc))
            return

        try:
            save_config(cfg)
        except OSError as exc:
            status.update(f"Could not write config: {exc}")
            return

        self.dismiss(True)
