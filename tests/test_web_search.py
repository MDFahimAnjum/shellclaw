"""Tests for web_search tool and query safety checks."""

from __future__ import annotations

import pytest

from shellclaw.safety.web_search_safety import validate_web_search_query


@pytest.mark.asyncio
async def test_web_search_blocked_for_system_path():
    from shellclaw.agent.tools import dispatch

    out = await dispatch("web_search", {"query": "error reading /etc/passwd help"})
    assert "[blocked]" in out
    assert "system information" in out.lower() or "local" in out.lower()


@pytest.mark.asyncio
async def test_web_search_blocked_for_empty_query():
    from shellclaw.agent.tools import dispatch

    out = await dispatch("web_search", {"query": "   "})
    assert "[blocked]" in out


@pytest.mark.asyncio
async def test_web_search_blocked_for_email():
    from shellclaw.agent.tools import dispatch

    out = await dispatch("web_search", {"query": "contact me at user@example.com"})
    assert "[blocked]" in out
    assert "personal" in out.lower() or "pii" in out.lower() or "information" in out.lower()


@pytest.mark.asyncio
async def test_web_search_runs_with_mock_ddgs(monkeypatch):
    from shellclaw.agent import tools as tools_mod

    class _FakeDDGS:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def text(self, query, **kwargs):
            assert "nginx" in query
            yield {
                "title": "Example",
                "href": "https://example.com/doc",
                "body": "How to install nginx.",
            }

    monkeypatch.setattr("ddgs.DDGS", lambda: _FakeDDGS())

    out = await tools_mod.dispatch("web_search", {"query": "ubuntu install nginx"})
    assert "Example" in out
    assert "example.com" in out


def test_validate_allows_simple_query():
    ok, msg = validate_web_search_query("how to free disk space linux")
    assert ok
    assert msg == ""


def test_validate_blocks_ssh_key_material():
    ok, msg = validate_web_search_query("ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAAB")
    assert not ok
