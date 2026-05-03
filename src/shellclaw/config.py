"""Application configuration loaded from ~/.config/shellclaw/config.toml."""

from __future__ import annotations

from os.path import basename
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

CONFIG_PATH = Path.home() / ".config" / "shellclaw" / "config.toml"
DATA_DIR = Path.home() / ".local" / "share" / "shellclaw"
BACKUP_DIR = DATA_DIR / "backups"

# Conservative character-to-token estimate: 4 chars ≈ 1 token.
# We budget 12 000 tokens for history to leave room for the system prompt
# and the model's reply.
CHARS_PER_TOKEN = 4
MAX_HISTORY_TOKENS = 100_000
MAX_TERMINAL_USER_MESSAGE_TOKENS = 30_000 # how much of a manual side-terminal command + output is included in the user message 

MAX_HISTORY_CHARS = MAX_HISTORY_TOKENS * CHARS_PER_TOKEN
MAX_TERMINAL_USER_MESSAGE_CHARS = MAX_TERMINAL_USER_MESSAGE_TOKENS * CHARS_PER_TOKEN

# Ceilings for find tools
FIND_MAX_DEPTH = 4
FIND_MAX_RESULTS = 10

# Defaults for web_search (DuckDuckGo via ddgs); not exposed as LLM tool parameters.
WEB_SEARCH_MAX_RESULTS = 5
WEB_SEARCH_REGION = "us-en"
WEB_SEARCH_SAFESEARCH = "moderate"  # off | moderate | strict
# None = any time; d, w, m, y = day, week, month, year
WEB_SEARCH_TIMELIMIT: str | None = None


# Provider HTTP I/O log (request JSON + response JSON body). Not read from TOML — flip the flag here.
PROVIDER_IO_LOG_ENABLED = True
PROVIDER_IO_LOG_PATH = DATA_DIR / "provider_io.txt"

# Terminal history debug log — written when DebugConfig.save_terminal is True.
TERMINAL_LOG_PATH = DATA_DIR / "terminal_history.txt"

# Context compression threshold in tokens
CONTEXT_COMPRESSION_THRESHOLD = 1000
CONTEXT_COMPRESSION_CHAR_BUDGET = CHARS_PER_TOKEN * CONTEXT_COMPRESSION_THRESHOLD


class ProviderConfig(BaseModel):
    name: str = "ollama"
    model: str = "qwen3.5:9b"
    base_url: str = "http://localhost:11434/v1"
    api_key: str = "ollama"
    # None = shellclaw picks a backend-specific default (0.2 for Ollama).
    temperature: float | None = None
    # Ollama only: passed as request ``options.num_ctx`` (ignored for other providers).
    num_ctx: int = 32_000


class AgentConfig(BaseModel):
    # Per user message: max executed tool calls (each tool in a batch counts).
    max_iterations: int = 10
    max_output_bytes: int = 8192
    timeout_seconds: int = 10
    # False = compact tool schemas and short tool docs (small models). True = full.
    advanced_toolset: bool = False
    # Summarize older tool rounds into system prompt additional context (extra LLM call when triggered).
    tool_context_compression: bool = True
    find_max_depth: int = FIND_MAX_DEPTH
    find_max_results: int = FIND_MAX_RESULTS
    web_search_max_results: int = WEB_SEARCH_MAX_RESULTS
    web_search_region: str = WEB_SEARCH_REGION
    web_search_safesearch: str = WEB_SEARCH_SAFESEARCH
    web_search_timelimit: str | None = WEB_SEARCH_TIMELIMIT


class UIConfig(BaseModel):
    show_raw_output: bool = True
    theme: str = "dark"


class HealthConfig(BaseModel):
    snapshot_on_launch: bool = True


def parse_extra_allowed_command_bases(raw: str | None) -> frozenset[str]:
    """Split comma-separated command basenames for :func:`run_safe` extra allowlist.

    Whitespace is stripped per entry; path-like values use :func:`os.path.basename`
    so only the final component is kept. Empty entries are skipped.
    """
    if not raw or not isinstance(raw, str):
        return frozenset()
    out: set[str] = set()
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        base = basename(name.strip())
        if not base or base in (".", ".."):
            continue
        out.add(base)
    return frozenset(out)


class SafetyConfig(BaseModel):
    auto_backup: bool = True
    backup_retention_days: int = 30
    # Model for `shellclaw check` only. Empty string falls back to [provider].model in CLI.
    check_model: str = "qwen2.5-coder:7b"
    # check_model: str | None = None
    # Comma-separated extra command basenames merged into run_safe allowlist (see read_rules.md).
    extra_allowed_command_bases: str = ""


class DebugConfig(BaseModel):
    # When True every entry appended to TerminalHistoryStore is written to
    # TERMINAL_LOG_PATH so you can inspect command output for leftover noise.
    save_terminal: bool = True


class SimplexConfig(BaseModel):
    """SimpleX Chat bridge (dedicated DB under DATA_DIR; WebSocket to ``simplex-chat -p``)."""

    # Local WebSocket port for ``simplex-chat -p``.
    port: int = 5225
    # ``@contactId`` or ``#groupId`` for the phone chat; set after first-time setup.
    chat_ref: str = ""
    # Directory prefix passed to ``simplex-chat -d`` (empty = default under DATA_DIR).
    database_dir: str = ""
    # Executable name or path for the SimpleX Chat CLI.
    executable: str = "simplex-chat"


class AppConfig(BaseSettings):
    model_config = SettingsConfigDict(
        toml_file=str(CONFIG_PATH),
        env_prefix="shellclaw_",
        env_nested_delimiter="__",
        extra="ignore",
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            TomlConfigSettingsSource(settings_cls),
            dotenv_settings,
            file_secret_settings,
        )

    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    debug: DebugConfig = Field(default_factory=DebugConfig)
    simplex: SimplexConfig = Field(default_factory=SimplexConfig)


def load_config() -> AppConfig:
    """Load config from file, returning defaults if the file does not exist."""
    return AppConfig()


def save_config(config: AppConfig) -> None:
    """Write config to ~/.config/shellclaw/config.toml in TOML format."""
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []

    def _write_section(section_name: str, model: BaseModel) -> None:
        lines.append(f"\n[{section_name}]")
        for field_name, value in model.model_dump().items():
            if value is None:
                continue
            if isinstance(value, str):
                lines.append(f'{field_name} = "{value}"')
            elif isinstance(value, bool):
                lines.append(f"{field_name} = {str(value).lower()}")
            else:
                lines.append(f"{field_name} = {value}")

    _write_section("provider", config.provider)
    _write_section("agent", config.agent)
    _write_section("ui", config.ui)
    _write_section("health", config.health)
    _write_section("safety", config.safety)
    _write_section("debug", config.debug)
    _write_section("simplex", config.simplex)

    CONFIG_PATH.write_text("\n".join(lines).lstrip() + "\n")


def config_exists() -> bool:
    return CONFIG_PATH.exists()


def provider_stream_log_path() -> Path | None:
    """Return the path for provider request/response logging, or None if disabled."""
    if not PROVIDER_IO_LOG_ENABLED:
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return PROVIDER_IO_LOG_PATH


def terminal_log_path(config: "AppConfig") -> Path | None:
    """Return the path for terminal history debug logging, or None if disabled."""
    if not config.debug.save_terminal:
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return TERMINAL_LOG_PATH


def simplex_database_prefix(config: "AppConfig") -> Path:
    """Filesystem prefix for ``simplex-chat -d`` (separate from the main SimpleX profile)."""
    raw = (config.simplex.database_dir or "").strip()
    if raw:
        return Path(raw).expanduser()
    return DATA_DIR / "simplex" / "simplex_v1"
