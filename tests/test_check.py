"""Tests for the shellclaw check subcommand."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from shellclaw.config import AppConfig, ProviderConfig, SafetyConfig
from shellclaw.providers.base import DeltaDone, DeltaText
from shellclaw.safety.check import _run_check


def _make_config() -> AppConfig:
    return AppConfig(
        provider=ProviderConfig(name="ollama", model="test", api_key="test"),
    )


class TestRunCheck:
    async def test_check_prints_output(self, capsys):
        async def mock_stream(*args, **kwargs):
            yield DeltaText(text="This command is ")
            yield DeltaText(text="safe to run.")
            yield DeltaDone()

        mock_provider = MagicMock()
        mock_provider.stream = mock_stream

        with patch("shellclaw.safety.check.get_provider", return_value=mock_provider):
            with patch("shellclaw.safety.check.load_profile", return_value={}):
                config = _make_config()
                await _run_check("apt clean", config)

        captured = capsys.readouterr()
        assert "safe" in captured.out.lower() or "This command" in captured.out

    async def test_check_builds_message_with_command(self, capsys):
        """The command string is included in the message sent to the LLM."""
        captured_messages = []

        async def mock_stream(messages, tools, system):
            captured_messages.extend(messages)
            yield DeltaText(text="Safe.")
            yield DeltaDone()

        mock_provider = MagicMock()
        mock_provider.stream = mock_stream

        with patch("shellclaw.safety.check.get_provider", return_value=mock_provider):
            with patch("shellclaw.safety.check.load_profile", return_value={}):
                config = _make_config()
                await _run_check("rm -rf /tmp/cache", config)

        assert any("rm -rf /tmp/cache" in str(m) for m in captured_messages)

    async def test_check_uses_hardware_profile(self, capsys):
        """Hardware profile is included in the message when available."""
        hardware = {"CPU": "Intel i5", "RAM": "8GB"}
        captured_messages = []

        async def mock_stream(messages, tools, system):
            captured_messages.extend(messages)
            yield DeltaText(text="Safe.")
            yield DeltaDone()

        mock_provider = MagicMock()
        mock_provider.stream = mock_stream

        with patch("shellclaw.safety.check.get_provider", return_value=mock_provider):
            with patch("shellclaw.safety.check.load_profile", return_value=hardware):
                config = _make_config()
                await _run_check("df -h", config)

        assert any("Intel i5" in str(m) for m in captured_messages)

    async def test_check_uses_safety_check_model_when_set(self, capsys):
        """When safety.check_model is set, get_provider receives that model."""
        captured = {}

        async def mock_stream(*args, **kwargs):
            yield DeltaText(text="Safe.")
            yield DeltaDone()

        def capture_get_provider(**kwargs):
            captured.update(kwargs)
            mock_provider = MagicMock()
            mock_provider.stream = mock_stream
            return mock_provider

        config = AppConfig(
            provider=ProviderConfig(name="ollama", model="qwen3.5:9b", api_key="test"),
            safety=SafetyConfig(check_model="qwen2.5:7b"),
        )

        with patch("shellclaw.safety.check.get_provider", side_effect=capture_get_provider):
            with patch("shellclaw.safety.check.load_profile", return_value={}):
                await _run_check("ls", config)

        assert captured.get("model") == "qwen2.5:7b"
