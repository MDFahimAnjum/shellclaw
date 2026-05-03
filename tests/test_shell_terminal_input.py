"""Unit tests for terminal shell highlighter and command suggester."""

from __future__ import annotations

import pytest
from rich.text import Text

from shellclaw.tui.shell_highlighter import ShellCommandHighlighter
from shellclaw.tui.shell_suggester import ShellCommandSuggester


class _HighlightDB:
    __slots__ = ("_names",)

    def __init__(self, names: set[str]) -> None:
        self._names = names

    def __contains__(self, item: str) -> bool:
        return item in self._names


class _SuggestDB:
    def completion_candidates(self, prefix: str) -> list[str]:
        if prefix == "d":
            return ["df", "diff", "dmesg"]
        return []


def _span_styles(text: Text) -> list[tuple[int, int, str]]:
    spans = getattr(text, "_spans", None) or text.spans
    return sorted((s.start, s.end, str(s.style)) for s in spans)


def test_highlighter_sudo_known_unknown() -> None:
    db = _HighlightDB({"ls", "apt"})
    h = ShellCommandHighlighter(db)
    t = Text("sudo ls notacommand")
    h.highlight(t)
    styles = _span_styles(t)
    assert (0, 4, "bold yellow") in styles  # sudo
    assert (5, 7, "green") in styles  # ls
    assert (8, 19, "bold red") in styles  # notacommand


def test_highlighter_flag_and_path() -> None:
    db = _HighlightDB({"ls"})
    h = ShellCommandHighlighter(db)
    t = Text("ls -la /tmp")
    h.highlight(t)
    styles = _span_styles(t)
    assert (0, 2, "green") in styles
    assert (3, 6, "dim") in styles
    assert (7, 11, "green") in styles  # /tmp


def test_highlighter_assignment_dim() -> None:
    db = _HighlightDB({"ls"})
    h = ShellCommandHighlighter(db)
    t = Text("FOO=1 ls")
    h.highlight(t)
    styles = _span_styles(t)
    assert (0, 5, "dim") in styles
    assert (6, 8, "green") in styles


def test_highlighter_pipe_operator() -> None:
    db = _HighlightDB({"ls", "grep"})
    h = ShellCommandHighlighter(db)
    t = Text("ls | grep")
    h.highlight(t)
    styles = _span_styles(t)
    assert (3, 4, "bold cyan") in styles
    assert (0, 2, "green") in styles
    assert (5, 9, "green") in styles


def test_highlighter_double_quoted_string() -> None:
    db = _HighlightDB({"echo"})
    h = ShellCommandHighlighter(db)
    t = Text('echo "hello world"')
    h.highlight(t)
    styles = _span_styles(t)
    assert (0, 4, "green") in styles
    assert (5, 18, "dim") in styles


def test_highlighter_single_quoted_string() -> None:
    db = _HighlightDB({"grep"})
    h = ShellCommandHighlighter(db)
    t = Text("grep 'a|b' x")
    h.highlight(t)
    styles = _span_styles(t)
    assert (0, 4, "green") in styles
    assert (5, 10, "dim") in styles
    assert (11, 12, "bold red") in styles  # unknown single-letter "command"


def test_highlighter_double_quoted_escape() -> None:
    db = _HighlightDB({"echo"})
    h = ShellCommandHighlighter(db)
    t = Text(r'echo "say \"hi\""')
    h.highlight(t)
    styles = _span_styles(t)
    assert (5, 17, "dim") in styles


def test_highlighter_redirect_dim() -> None:
    db = _HighlightDB({"ls"})
    h = ShellCommandHighlighter(db)
    t = Text("ls 2>/dev/null")
    h.highlight(t)
    styles = _span_styles(t)
    assert (3, 14, "dim") in styles


def test_highlighter_observe_tool_name_green() -> None:
    db = _HighlightDB(set())
    h = ShellCommandHighlighter(db)
    t = Text("exact_find --foo")
    h.highlight(t)
    styles = _span_styles(t)
    assert (0, 10, "green") in styles
    assert (11, 16, "dim") in styles


def test_highlighter_numeric_argument_dim() -> None:
    db = _HighlightDB({"head"})
    h = ShellCommandHighlighter(db)
    t = Text("head -n 20")
    h.highlight(t)
    styles = _span_styles(t)
    assert (8, 10, "dim") in styles


def test_highlighter_brace_punctuation_dim() -> None:
    db = _HighlightDB(set())
    h = ShellCommandHighlighter(db)
    t = Text("echo {a..z}")
    h.highlight(t)
    styles = _span_styles(t)
    assert (5, 6, "dim") in styles  # {
    assert (6, 10, "dim") in styles  # a..z
    assert (10, 11, "dim") in styles  # }


@pytest.mark.asyncio
async def test_suggester_extends_last_token() -> None:
    s = ShellCommandSuggester(_SuggestDB())
    assert await s.get_suggestion("sudo d") == "sudo df"


@pytest.mark.asyncio
async def test_suggester_no_match_when_exact() -> None:
    s = ShellCommandSuggester(_SuggestDB())
    assert await s.get_suggestion("sudo df") is None


@pytest.mark.asyncio
async def test_suggester_empty_partial() -> None:
    s = ShellCommandSuggester(_SuggestDB())
    assert await s.get_suggestion("sudo ") is None
