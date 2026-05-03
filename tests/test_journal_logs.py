"""Tests for journal_logs tool validation."""

import pytest

from shellclaw.agent.tools import _journal_logs_handler


@pytest.mark.asyncio
async def test_search_multiword_query_returns_error_without_run_safe(monkeypatch):
    async def _must_not_run(*_a, **_k):
        raise AssertionError("run_safe must not be called for invalid query")

    monkeypatch.setattr("shellclaw.agent.tools.run_safe", _must_not_run)
    out = await _journal_logs_handler("search", 15, None, "slack error", None)
    assert "[error]" in out
    assert "single term" in out.lower() or "one word" in out.lower()


@pytest.mark.asyncio
async def test_search_single_token_calls_run_safe(monkeypatch):
    calls: list[str] = []

    async def capture(cmd: str, **_k):
        calls.append(cmd)
        return "ok"

    monkeypatch.setattr("shellclaw.agent.tools.run_safe", capture)
    out = await _journal_logs_handler("search", 15, None, "slack", None)
    assert out == "ok"
    assert len(calls) == 1
    assert "journalctl" in calls[0] and "--grep=" in calls[0]


@pytest.mark.asyncio
async def test_search_hyphenated_single_token_allowed(monkeypatch):
    calls: list[str] = []

    async def capture(cmd: str, **_k):
        calls.append(cmd)
        return "ok"

    monkeypatch.setattr("shellclaw.agent.tools.run_safe", capture)
    out = await _journal_logs_handler("search", 15, None, "slack-error", None)
    assert out == "ok"
    assert "slack-error" in calls[0]
