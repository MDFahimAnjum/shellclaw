"""Rich highlighter for a single-line shell command in the terminal input."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from rich.highlighter import Highlighter
from rich.text import Text

from ..agent.tools import OBSERVE_TOOL_NAMES

if TYPE_CHECKING:
    from .command_db import CommandDatabase

_ASSIGNMENT = re.compile(r"^[\w.-]+=")
_DIGITS_ONLY = re.compile(r"^\d+$")
# Command-shaped identifier vs argument-like tokens (paths, globs, versions, etc.)
_IDENTIFIER_WORD = re.compile(r"^[a-zA-Z_][\w-]*$")
_ARGISH = re.compile(r"[\d./~*?\[\]${}:,=@#%^]")

_TOOL_NAMES_CF: frozenset[str] = frozenset(n.casefold() for n in OBSERVE_TOOL_NAMES)


class ShellCommandHighlighter(Highlighter):
    """Color shell-like lines: operators, redirects, tools, known cmds, args (dim), unknown cmds (red)."""

    def __init__(self, db: CommandDatabase) -> None:
        self._db = db

    def highlight(self, text: Text) -> None:
        plain = text.plain
        spans = getattr(text, "_spans", None)
        if spans is not None:
            spans.clear()
        elif hasattr(text, "spans"):
            text.spans.clear()
        if not plain:
            return

        for start, end, segment, kind in _iter_segments(plain):
            style = _style_for_segment(segment, kind, self._db)
            if style:
                text.stylize(style, start, end)


def _style_for_segment(slice_: str, kind: str, db: CommandDatabase) -> str | None:
    if kind == "operator":
        return "bold cyan"
    if kind == "string":
        return "dim"
    if kind in ("redirect", "punct", "flag"):
        return "dim"
    if kind != "word":
        return None
    return _style_for_word(slice_, db)


def _style_for_word(word: str, db: CommandDatabase) -> str | None:
    if word == "sudo":
        return "bold yellow"
    if _ASSIGNMENT.match(word):
        return "dim"
    if word.startswith("-"):
        return "dim"
    if _DIGITS_ONLY.match(word):
        return "dim"
    if _is_path_like(word):
        return "green"
    if word.casefold() in _TOOL_NAMES_CF:
        return "green"
    if word in db:
        return "green"
    if _ARGISH.search(word) or not _IDENTIFIER_WORD.match(word):
        return "dim"
    return "bold red"


def _is_path_like(token: str) -> bool:
    return (
        token.startswith("/")
        or token.startswith("./")
        or token.startswith("../")
        or token.startswith("~/")
    )


def _is_segment_delim(plain: str, j: int) -> bool:
    if j >= len(plain):
        return True
    c = plain[j]
    if c.isspace():
        return True
    if c in "|;()[]{}":
        return True
    if c == "&":
        return True
    if plain.startswith("&&", j) or plain.startswith("||", j):
        return True
    if c == ">":
        return True
    if c in "\"'":
        return True
    return False


def _consume_single_quoted(plain: str, i: int) -> tuple[int, str] | None:
    """Single-quoted shell string: '...' (no escapes; unclosed runs to EOF)."""
    if i >= len(plain) or plain[i] != "'":
        return None
    n = len(plain)
    j = i + 1
    while j < n:
        if plain[j] == "'":
            return j + 1, plain[i : j + 1]
        j += 1
    return n, plain[i:n]


def _consume_double_quoted(plain: str, i: int) -> tuple[int, str] | None:
    """Double-quoted string with \\ escapes for \\\" and \\\\."""
    if i >= len(plain) or plain[i] != '"':
        return None
    n = len(plain)
    j = i + 1
    while j < n:
        if plain[j] == "\\" and j + 1 < n:
            j += 2
            continue
        if plain[j] == '"':
            return j + 1, plain[i : j + 1]
        j += 1
    return n, plain[i:n]


def _iter_segments(plain: str):
    """Yield (start, end, slice, kind). Kinds: operator, string, redirect, punct, flag, word."""
    i, n = 0, len(plain)
    while i < n:
        if plain[i].isspace():
            i += 1
            continue
        if plain.startswith("&&", i):
            yield i, i + 2, "&&", "operator"
            i += 2
            continue
        if plain.startswith("||", i):
            yield i, i + 2, "||", "operator"
            i += 2
            continue
        if plain[i] == "|":
            yield i, i + 1, "|", "operator"
            i += 1
            continue
        if plain[i] == ";":
            yield i, i + 1, ";", "operator"
            i += 1
            continue
        if plain[i] == "&":
            yield i, i + 1, "&", "operator"
            i += 1
            continue
        if plain[i] in "()[]{}":
            yield i, i + 1, plain[i], "punct"
            i += 1
            continue

        sq = _consume_single_quoted(plain, i)
        if sq is not None:
            end, seg = sq
            yield i, end, seg, "string"
            i = end
            continue

        dq = _consume_double_quoted(plain, i)
        if dq is not None:
            end, seg = dq
            yield i, end, seg, "string"
            i = end
            continue

        rm = re.match(r"\d*(?:>>|>)\S*", plain[i:])
        if rm and ">" in rm.group(0):
            seg = rm.group(0)
            e = i + len(seg)
            yield i, e, seg, "redirect"
            i = e
            continue

        if plain[i] == "-":
            j = i + 1
            while j < n and not _is_segment_delim(plain, j):
                if plain.startswith("&&", j) or plain.startswith("||", j):
                    break
                j += 1
            yield i, j, plain[i:j], "flag"
            i = j
            continue

        j = i
        while j < n and not _is_segment_delim(plain, j):
            if plain.startswith("&&", j) or plain.startswith("||", j):
                break
            j += 1
        yield i, j, plain[i:j], "word"
        i = j
