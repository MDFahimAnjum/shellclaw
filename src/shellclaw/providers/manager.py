"""Provider factory — reads config and returns the correct provider instance."""

from __future__ import annotations

from pathlib import Path

from .anthropic import AnthropicProvider
from .base import Provider
from .openai_compat import OpenAICompatProvider

OPENAI_COMPAT_PROVIDERS = {"ollama", "openai", "groq", "openrouter", "lmstudio", "custom"}

PROVIDER_BASE_URLS: dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "groq": "https://api.groq.com/openai/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://localhost:11434/v1",
    "lmstudio": "http://127.0.0.1:1234/v1",
}


def get_provider(
    name: str,
    model: str,
    api_key: str,
    base_url: str | None = None,
    stream_log_path: Path | None = None,
    temperature: float | None = None,
    num_ctx: int | None = None,
) -> Provider:
    """Return a configured provider instance for the given provider name."""
    if name == "anthropic":
        return AnthropicProvider(
            api_key=api_key, model=model, stream_log_path=stream_log_path
        )

    resolved_url = base_url or PROVIDER_BASE_URLS.get(name, "http://localhost:11434/v1")
    # Low default for local Ollama improves tool-call and instruction adherence.
    effective_temp = temperature
    if effective_temp is None and name == "ollama":
        effective_temp = 0.2

    ollama_num_ctx = num_ctx if name == "ollama" else None

    return OpenAICompatProvider(
        base_url=resolved_url,
        api_key=api_key,
        model=model,
        stream_log_path=stream_log_path,
        temperature=effective_temp,
        num_ctx=ollama_num_ctx,
    )
