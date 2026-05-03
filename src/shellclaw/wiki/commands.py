"""Parse shell command lines for wiki lookup (sudo, pipelines, wrappers)."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath

# Leading VAR=value assignments on a command segment
_ENV_ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# sudo long options that take a separate argument (when not using --opt=value)
_SUDO_LONG_WITH_ARG = frozenset(
    {
        "--user",
        "--group",
        "--chdir",
        "--login-class",
        "--preserve-env",
        "--set-home",
        "--other-user",
        "--host",
        "--session-tty",
    }
)

# short sudo options that consume the next token as value
_SUDO_SHORT_WITH_ARG = frozenset("ugUDpRsTtc")


def split_pipeline_segments(line: str) -> list[str]:
    """Split a command on `|` outside single/double quotes and unquoted contexts."""
    if not line.strip():
        return []
    parts: list[str] = []
    buf: list[str] = []
    in_squote = False
    in_dquote = False
    i = 0
    while i < len(line):
        c = line[i]
        if in_squote:
            buf.append(c)
            if c == "'":
                in_squote = False
        elif in_dquote:
            if c == "\\" and i + 1 < len(line):
                buf.append(line[i + 1])
                i += 2
                continue
            buf.append(c)
            if c == '"':
                in_dquote = False
        else:
            if c == "'":
                in_squote = True
                buf.append(c)
            elif c == '"':
                in_dquote = True
                buf.append(c)
            elif c == "|":
                seg = "".join(buf).strip()
                if seg:
                    parts.append(seg)
                buf = []
            else:
                buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return parts


def _consume_sudo_flags(tokens: list[str]) -> None:
    """Mutate tokens, consuming sudo short/long flags until the subcommand."""
    while tokens:
        cur = tokens[0]
        if cur == "--":
            tokens.pop(0)
            return
        if not cur.startswith("-") or cur == "-":
            return
        opt = tokens.pop(0)
        if "=" in opt:
            continue
        low = opt.lower()
        if low.startswith("--"):
            if low in _SUDO_LONG_WITH_ARG:
                if tokens and not tokens[0].startswith("-"):
                    tokens.pop(0)
            continue
        body = low.lstrip("-")
        for ch in body:
            if ch in _SUDO_SHORT_WITH_ARG and tokens and not tokens[0].startswith("-"):
                tokens.pop(0)


def strip_leading_privilege(tokens: list[str]) -> list[str]:
    """Remove leading sudo/doas/run0 (and common sudo arguments) from argv."""
    t = list(tokens)
    while t:
        head = t[0].lower()
        if head == "sudo":
            t.pop(0)
            _consume_sudo_flags(t)
            continue
        if head == "doas":
            t.pop(0)
            if t and t[0] == "-u" and len(t) > 1:
                t.pop(0)
                t.pop(0)
            continue
        if head == "run0":
            t.pop(0)
            continue
        break
    return t


def strip_env_assignments(tokens: list[str]) -> list[str]:
    """Drop leading VAR=value tokens (e.g. `LANG=C grep`)."""
    t = list(tokens)
    while t and _ENV_ASSIGN_RE.match(t[0]):
        t.pop(0)
    return t


def primary_executable_name(token: str) -> str:
    """Basename for paths like /usr/bin/grep; otherwise the token unchanged."""
    if "/" in token:
        return PurePath(token).name
    return token


def first_executable_from_segment(segment: str) -> str | None:
    """First real command in one pipeline segment (after sudo/env)."""
    try:
        tokens = shlex.split(segment.strip(), posix=True)
    except ValueError:
        tokens = segment.split()

    tokens = strip_leading_privilege(tokens)
    tokens = strip_env_assignments(tokens)
    if not tokens:
        return None
    return primary_executable_name(tokens[0])


def command_names_in_shell_line(line: str) -> list[str]:
    """Ordered unique executable names for tldr/glossary (handles `|` and sudo)."""
    segments = split_pipeline_segments(line)
    if not segments:
        segments = [line.strip()] if line.strip() else []

    out: list[str] = []
    seen_set: set[str] = set()

    for seg in segments:
        name = first_executable_from_segment(seg)
        if not name:
            continue
        key = name.lower()
        if key not in seen_set:
            seen_set.add(key)
            out.append(name)
    return out


def normalize_shell_line_for_glossary(line: str) -> str:
    """Flatten pipelines and drop privilege wrappers so find_terms hits real commands."""
    s = re.sub(r"\b(sudo|doas|run0)\b", " ", line, flags=re.IGNORECASE)
    s = s.replace("|", " ")
    return re.sub(r"\s+", " ", s).strip()


def text_for_glossary_search(line: str) -> str:
    """Text passed to glossary term scanning for a shell command line."""
    names = command_names_in_shell_line(line)
    flat = normalize_shell_line_for_glossary(line)
    if names:
        return f"{flat} {' '.join(names)}".strip()
    return flat
