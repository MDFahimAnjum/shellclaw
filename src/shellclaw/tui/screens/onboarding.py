"""First-run onboarding wizard.

Two steps: (1) choose provider in a scrollable list, (2) API URL and model ids.
Chat and check model ids are required for every provider (Ollama: pick from list or
“Same as chat model” for check, stored as the same id).
On completion, writes the config file and pushes the MainScreen.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.screen import Screen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Select, Static

from ...config import AppConfig, AgentConfig, HealthConfig, ProviderConfig, SafetyConfig, UIConfig, save_config
from ...providers.manager import PROVIDER_BASE_URLS

_PROVIDER_OPTIONS = [
    ("ollama", "Ollama - local & cloud service"),
    ("openai", "OpenAI - (ChatGPT) cloud service"),
    ("anthropic", "Anthropic - (Claude) cloud service"),
    ("groq", "Groq - (xAI) cloud service"),
    ("openrouter", "OpenRouter - (hub) cloud service"),
    ("lmstudio", "LM Studio - local service"),
    ("custom", "Custom - Let me set it manually"),
]

# API key is required to continue for these; custom / LM Studio may use a blank key.
_API_KEY_REQUIRED = {"openai", "anthropic", "groq", "openrouter"}
# Every provider except Ollama uses the API key field when shown.
_SHOW_API_KEY = {
    "openai",
    "anthropic",
    "groq",
    "openrouter",
    "custom",
    "lmstudio",
}
_SHOW_BASE_URL = {"custom", "lmstudio"}
_SHOW_REMOTE_MODEL_FIELDS = _SHOW_API_KEY

_OLLAMA_TAGS_URL = "http://127.0.0.1:11434/api/tags"
_OLLAMA_SHOW_URL = "http://127.0.0.1:11434/api/show"
_PLACEHOLDER_LOADING = "__loading__"
_PLACEHOLDER_EMPTY = "__empty__"
_PLACEHOLDER_ERROR = "__error__"
# When chosen, check model is stored as the same id as the chat model (non-empty).
_CHECK_SAME_AS_CHAT = "__check_same_as_chat__"


def _ollama_check_model_select_options(
    models: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    return [("Same as chat model", _CHECK_SAME_AS_CHAT), *models]


def _format_bytes(num: int) -> str:
    n = float(num)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    i = 0
    while n >= 1024 and i < len(units) - 1:
        n /= 1024
        i += 1
    if i == 0:
        return f"{int(n)} {units[i]}"
    return f"{n:.1f} {units[i]}"


def _models_from_tags_payload(data: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for entry in data.get("models") or []:
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        size = entry.get("size")
        if isinstance(size, int) and size > 0:
            label = f"{name} ({_format_bytes(size)})"
        else:
            label = name
        out.append((label, name))
    out.sort(key=lambda t: t[1].lower())
    return out


def _list_ollama_models_via_api() -> list[tuple[str, str]] | None:
    try:
        r = httpx.get(_OLLAMA_TAGS_URL, timeout=5.0)
        r.raise_for_status()
        return _models_from_tags_payload(r.json())
    except (httpx.HTTPError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None


def _parse_ollama_list_stdout(stdout: str) -> list[tuple[str, str]]:
    lines = [ln.rstrip() for ln in stdout.strip().splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    out: list[tuple[str, str]] = []
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[0]
        label = name
        if len(parts) >= 4 and parts[2][0].isdigit() and parts[3] in (
            "B",
            "KB",
            "MB",
            "GB",
            "TB",
            "KiB",
            "MiB",
            "GiB",
            "TiB",
        ):
            label = f"{name} ({parts[2]} {parts[3]})"
        out.append((label, name))
    out.sort(key=lambda t: t[1].lower())
    return out


def _list_ollama_models_via_cli() -> list[tuple[str, str]] | None:
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return _parse_ollama_list_stdout(result.stdout)


def discover_ollama_models() -> tuple[list[tuple[str, str]], str | None]:
    """Return (options, error_message). options may be empty if Ollama runs but has no models."""
    via_api = _list_ollama_models_via_api()
    if via_api is not None:
        return via_api, None
    via_cli = _list_ollama_models_via_cli()
    if via_cli is not None:
        return via_cli, None
    return [], "Could not reach Ollama (http://127.0.0.1:11434) or run `ollama list`."


def _model_in_tag_list(model: str, models: list[tuple[str, str]]) -> bool:
    names = {m[1] for m in models}
    if model in names:
        return True
    if model.endswith(":latest") and model[:-7] in names:
        return True
    return f"{model}:latest" in names


def verify_ollama_model_available(model: str) -> bool:
    """Confirm the model exists (prefer /api/show, fall back to tag list)."""
    try:
        r = httpx.post(_OLLAMA_SHOW_URL, json={"name": model}, timeout=10.0)
        if r.status_code == 200:
            return True
        if r.status_code == 404:
            return False
    except (httpx.HTTPError, OSError, TypeError, ValueError):
        pass
    listed, _err = discover_ollama_models()
    return _model_in_tag_list(model, listed)


class OnboardingScreen(Screen):
    CSS_PATH = str(Path(__file__).parent / "onboarding.tcss")

    BINDINGS = [
        Binding("ctrl+c", "app.quit", "Quit", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._selected_provider = "ollama"
        self._wizard_step = 1

    def compose(self) -> ComposeResult:
        from textual.containers import Container, Horizontal, ScrollableContainer, Vertical

        with Vertical(id="onboarding-container"):
            yield Static("Welcome to shellclaw", id="title")
            yield Static("", id="subtitle")

            with Vertical(id="onboarding-main"):
                with Vertical(id="pane-step1"):
                    with ScrollableContainer(id="provider-scroll"):
                        with RadioSet(id="provider-set"):
                            for value, label in _PROVIDER_OPTIONS:
                                yield RadioButton(
                                    label, value=(value == "ollama"), id=f"radio-{value}"
                                )
                    yield Static(
                        "💡 Tips:\n  • Ollama is recommended — it's free and private\n  • You can use your mouse pointer/cursor",
                        id="recommendation",
                   
                    )

                with Vertical(id="pane-step2", classes="hidden"):
                    yield Static("Connection & models", id="step2-heading")
                    with ScrollableContainer(id="step2-form-scroll"):
                        with Vertical(id="api-key-section"):
                            yield Label("API key:", id="api-key-label")
                            yield Input(
                                placeholder="Paste your API key here",
                                password=True,
                                id="api-key-input",
                            )

                        with Vertical(id="base-url-section"):
                            yield Label("API base URL:", id="base-url-label")
                            yield Input(placeholder="", id="base-url-input")

                        with Vertical(id="remote-model-section"):
                            yield Label("Chat model id:", id="remote-chat-label")
                            yield Input(
                                placeholder="Required — e.g. gpt-4o",
                                id="remote-chat-model-input",
                            )
                            yield Label(
                                "Check model id — shellclaw check:",
                                id="remote-check-label",
                            )
                            yield Input(
                                placeholder="Required — can match chat model",
                                id="remote-check-model-input",
                            )

                        with Vertical(id="ollama-model-section", classes="visible"):
                            yield Label("Chat model:", id="ollama-model-label")
                            yield Select(
                                [("— Loading models… —", _PLACEHOLDER_LOADING)],
                                id="ollama-model-select",
                                prompt="Pick a model",
                                allow_blank=False,
                            )
                            yield Label("Check model (shellclaw check):", id="ollama-check-model-label")
                            yield Select(
                                [("— Loading models… —", _PLACEHOLDER_LOADING)],
                                id="ollama-check-model-select",
                                prompt="Pick a model",
                                allow_blank=False,
                            )

                        yield Static("", id="status-message")

            with Horizontal(id="button-row"):
                yield Button("Back", variant="default", id="btn-back", classes="hidden")
                yield Button("Continue", variant="primary", id="btn-continue")
                yield Button("I'll configure later", variant="default", id="btn-skip")

    def on_mount(self) -> None:
        self._set_step(1)
        self._sync_section_visibility()

    def _sync_section_visibility(self) -> None:
        p = self._selected_provider
        api_section = self.query_one("#api-key-section")
        base_section = self.query_one("#base-url-section")
        remote_section = self.query_one("#remote-model-section")
        ollama_section = self.query_one("#ollama-model-section")

        if p in _SHOW_API_KEY:
            api_section.add_class("visible")
        else:
            api_section.remove_class("visible")

        if p in _SHOW_BASE_URL:
            base_section.add_class("visible")
            base_input = self.query_one("#base-url-input", Input)
            base_label = self.query_one("#base-url-label", Label)
            if p == "lmstudio":
                base_input.placeholder = PROVIDER_BASE_URLS["lmstudio"]
                base_label.update("API base URL (optional — default if blank):")
            else:
                base_input.placeholder = "https://…"
                base_label.update("API base URL (required):")
        else:
            base_section.remove_class("visible")

        if p in _SHOW_REMOTE_MODEL_FIELDS:
            remote_section.add_class("visible")
        else:
            remote_section.remove_class("visible")

        if p == "ollama":
            ollama_section.add_class("visible")
        else:
            ollama_section.remove_class("visible")

    def _set_step(self, step: int) -> None:
        self._wizard_step = step
        p1 = self.query_one("#pane-step1")
        p2 = self.query_one("#pane-step2")
        back_btn = self.query_one("#btn-back", Button)
        sub = self.query_one("#subtitle", Static)
        if step == 1:
            p1.remove_class("hidden")
            p2.add_class("hidden")
            back_btn.add_class("hidden")
            sub.update(
                "First, Pick your AI Provider"
            )
        else:
            p1.add_class("hidden")
            p2.remove_class("hidden")
            back_btn.remove_class("hidden")
            sub.update(
                "Step 2 — Enter your API key, base URL (if needed), and both model ids.\n"
                "Chat and check model names are required."
            )

    def on_radio_set_changed(self, event: RadioSet.Changed) -> None:
        # Map button index to provider name
        index = event.index
        self._selected_provider = _PROVIDER_OPTIONS[index][0]
        self._sync_section_visibility()
        if self._wizard_step == 2 and self._selected_provider == "ollama":
            self._load_ollama_models()

    def _go_step_2(self) -> None:
        self._set_step(2)
        self._sync_section_visibility()
        if self._selected_provider == "ollama":
            self._load_ollama_models()

        def _focus_step2() -> None:
            if self._selected_provider == "ollama":
                self.query_one("#ollama-model-select", Select).focus()
            else:
                self.query_one("#api-key-input", Input).focus()

        self.call_after_refresh(_focus_step2)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn-skip":
            self._save_default_config()
            self._open_main()
            return

        if event.button.id == "btn-back":
            self._set_status("")
            self._set_step(1)
            self.call_after_refresh(lambda: self.query_one("#btn-continue", Button).focus())
            return

        if event.button.id == "btn-continue":
            if self._wizard_step == 1:
                self._go_step_2()
                return

            if self._selected_provider == "ollama":
                sel = self.query_one("#ollama-model-select", Select)
                check_sel = self.query_one("#ollama-check-model-select", Select)
                choice = sel.value
                check_choice = check_sel.value
                bad = {
                    _PLACEHOLDER_LOADING,
                    _PLACEHOLDER_EMPTY,
                    _PLACEHOLDER_ERROR,
                }
                if choice in bad:
                    self._set_status(
                        "Pick a downloaded Ollama model from the list, or install Ollama and pull one first."
                    )
                    return
                if not isinstance(choice, str) or not choice.strip():
                    self._set_status("Pick a chat model from the first dropdown.")
                    return
                chat_save = str(choice).strip()
                if check_choice in bad:
                    self._set_status(
                        "Pick a check model from the second dropdown, or wait for the list to load."
                    )
                    return
                if check_choice == _CHECK_SAME_AS_CHAT:
                    check_model_save = chat_save
                else:
                    check_model_save = (
                        str(check_choice).strip() if isinstance(check_choice, str) else ""
                    )
                if not check_model_save:
                    self._set_status("Pick a check model from the second dropdown.")
                    return
                self._verify_and_save_ollama(chat_save, check_model_save)
            else:
                api_key = self.query_one("#api-key-input", Input).value.strip()
                if self._selected_provider in _API_KEY_REQUIRED and not api_key:
                    self._set_status("Please enter your API key.")
                    return
                base_in = self.query_one("#base-url-input", Input).value.strip()
                if self._selected_provider == "custom" and not base_in:
                    self._set_status("Enter the API base URL for your custom endpoint.")
                    return
                chat_m = self.query_one("#remote-chat-model-input", Input).value.strip()
                check_m = self.query_one("#remote-check-model-input", Input).value.strip()
                if not chat_m:
                    self._set_status("Enter the chat model id.")
                    return
                if not check_m:
                    self._set_status("Enter the check model id (shellclaw check).")
                    return

                if self._selected_provider == "custom":
                    resolved_base = base_in
                elif self._selected_provider == "lmstudio":
                    resolved_base = base_in or PROVIDER_BASE_URLS["lmstudio"]
                elif self._selected_provider == "anthropic":
                    resolved_base = "https://api.anthropic.com"
                else:
                    resolved_base = PROVIDER_BASE_URLS[self._selected_provider]

                self._save_config(
                    api_key,
                    model=chat_m,
                    check_model=check_m,
                    base_url=resolved_base,
                )
                self._open_main()

    @work(exclusive=True, thread=True)
    def _load_ollama_models(self) -> None:
        def reset_loading() -> None:
            try:
                loading = [("— Loading models… —", _PLACEHOLDER_LOADING)]
                self.query_one("#ollama-model-select", Select).set_options(loading)
                self.query_one("#ollama-check-model-select", Select).set_options(loading)
            except Exception:
                pass

        self.app.call_from_thread(reset_loading)
        models, err = discover_ollama_models()

        def apply() -> None:
            sel = self.query_one("#ollama-model-select", Select)
            check_sel = self.query_one("#ollama-check-model-select", Select)
            if err:
                err_row = [("— Could not list models —", _PLACEHOLDER_ERROR)]
                sel.set_options(err_row)
                check_sel.set_options(err_row)
                self._set_status(
                    f"{err} See https://ollama.com — then run e.g. ollama pull llama3.2"
                )
            elif not models:
                empty_row = [("— No local models found —", _PLACEHOLDER_EMPTY)]
                sel.set_options(empty_row)
                check_sel.set_options(_ollama_check_model_select_options(empty_row))
                self._set_status("Pull a model in your terminal first, e.g. ollama pull llama3.2")
            else:
                sel.set_options(models)
                check_sel.set_options(_ollama_check_model_select_options(models))
                self._set_status("")

        self.app.call_from_thread(apply)

    @work(exclusive=True, thread=True)
    def _verify_and_save_ollama(self, model: str, check_model: str) -> None:
        def set_msg(msg: str) -> None:
            self._set_status(msg)

        if not (model or "").strip():
            self.app.call_from_thread(
                lambda: set_msg("Pick a chat model from the list.")
            )
            return
        self.app.call_from_thread(lambda: set_msg(f"Checking chat model {model!r}…"))
        if not verify_ollama_model_available(model):
            self.app.call_from_thread(
                lambda: set_msg(
                    f"Chat model {model!r} is not available. Choose another from the list or run ollama pull."
                )
            )
            return
        if not (check_model or "").strip():
            self.app.call_from_thread(
                lambda: set_msg("Pick a check model (or “Same as chat model”).")
            )
            return
        if check_model != model:
            self.app.call_from_thread(
                lambda m=check_model: set_msg(f"Checking check model {m!r}…")
            )
            if not verify_ollama_model_available(check_model):
                self.app.call_from_thread(
                    lambda m=check_model: set_msg(
                        f"Check model {m!r} is not available. Choose another or pick “Same as chat model”."
                    )
                )
                return
        self.app.call_from_thread(
            lambda: self._save_config(
                "ollama",
                model=model,
                check_model=check_model,
                base_url=PROVIDER_BASE_URLS["ollama"],
            )
        )
        self.app.call_from_thread(self._open_main)

    def _save_config(
        self,
        api_key: str,
        *,
        model: str | None = None,
        check_model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        if base_url is None:
            if self._selected_provider == "anthropic":
                resolved_base = "https://api.anthropic.com"
            else:
                resolved_base = PROVIDER_BASE_URLS.get(
                    self._selected_provider, PROVIDER_BASE_URLS["ollama"]
                )
        else:
            resolved_base = base_url

        resolved_model = model if model is not None else ""

        provider = ProviderConfig(
            name=self._selected_provider,
            model=resolved_model,
            api_key=api_key,
            base_url=resolved_base,
        )
        safety = (
            SafetyConfig(check_model=check_model)
            if check_model is not None
            else SafetyConfig()
        )
        config = AppConfig(
            provider=provider,
            agent=AgentConfig(),
            ui=UIConfig(),
            health=HealthConfig(),
            safety=safety,
        )
        save_config(config)

    def _save_default_config(self) -> None:
        save_config(AppConfig())

    def _set_status(self, message: str) -> None:
        try:
            status = self.query_one("#status-message", Static)
            status.update(message)
        except Exception:
            pass

    def _open_main(self) -> None:
        from .main import MainScreen
        config = AppConfig()
        self.app.push_screen(MainScreen(config=config))
