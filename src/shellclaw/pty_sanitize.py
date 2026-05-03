"""Strip PTY noise from agent tool output before sending to the LLM.

Two-phase approach
------------------
Phase 1 – Aggressive clean (terminal-agnostic)
    • Strip every ANSI / VT escape sequence (CSI, OSC, SS2/SS3, DCS, …).
    • Strip box-drawing and decorative Unicode (U+2500–U+259F block elements,
      U+25A0–U+27BF geometric/dingbats, U+E000–U+F8FF private-use / powerline).
      This removes Starship, powerline, and other fancy prompt chrome without
      knowing anything about the user's prompt theme.
    • rstrip() + strip() each line; collapse runs of 2+ spaces → 1 space.
      Eliminates pyte terminal-column padding and any stray whitespace.

Phase 2 – Lightweight echo strip (safety net only)
    With the raw-byte PTY buffering in OscInterceptor, the command echo and
    stale scrollback are no longer present in the input.  The heavy multi-
    segment / wrapped-command logic from the previous screen-scraping approach
    is no longer needed.  We retain a single-pass check that removes a leading
    line that is an exact (or prefix/suffix) echo of the command, as a safety
    net for unusual shell configurations that still emit an echo after START.
"""

from __future__ import annotations

import re
import unicodedata

# ---------------------------------------------------------------------------
# Phase 1 – escape-sequence stripping
# ---------------------------------------------------------------------------

# OSC: BEL-terminated (\x1b] … \x07) — covers \x1b]777;shellclaw_…\x07
_OSC_BEL_RE = re.compile(r"\x1b\][^\x07]*\x07")
# OSC: ST-terminated (\x1b] … \x1b\) — only safe printable+whitespace body
_OSC_ST_RE = re.compile(r"\x1b\][\x08-\x0d\x20-\x7e]*\x1b\\")
# CSI: \x1b[ … final byte (covers SGR colours, cursor moves, etc.)
_CSI_RE = re.compile(r"\x1b\[[\x30-\x3f]*[\x20-\x2f]*[\x40-\x7e]")
# SS2 / SS3 / DCS / … + their single argument byte
_SS_RE = re.compile(r"\x1b[NOPnop].")
# Two-character Esc sequences (Esc + intermediate + final)
_SIMPLE_ESC_RE = re.compile(r"\x1b[\x20-\x2f][\x30-\x7e]")
# Any remaining lone \x1b
_OTHER_ESC_RE = re.compile(r"\x1b.")

# Runs of 2 or more spaces → single space
_MULTI_SPC_RE = re.compile(r"  +")


def _is_decor_char(ch: str) -> bool:
    """True for box-drawing, block/geometric shapes, and private-use glyphs."""
    cp = ord(ch)
    return (
        0x2500 <= cp <= 0x259F  # box drawing + block elements
        or 0x25A0 <= cp <= 0x27BF  # geometric shapes + dingbats
        or 0xE000 <= cp <= 0xF8FF  # private use area (powerline glyphs)
    )


def strip_ansi(text: str) -> str:
    """Remove all ANSI / VT escape sequences, returning plain text."""
    if not text:
        return ""
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    s = _OSC_BEL_RE.sub("", s)
    s = _OSC_ST_RE.sub("", s)
    s = _CSI_RE.sub("", s)
    s = _SS_RE.sub("", s)
    s = _SIMPLE_ESC_RE.sub("", s)
    s = _OTHER_ESC_RE.sub("", s)
    return s


def _clean_lines(text: str) -> list[str]:
    """Phase 1: return a list of clean, normalised lines."""
    s = strip_ansi(text)
    # Remove decorative Unicode (box-drawing, powerline, etc.)
    s = "".join(ch for ch in s if not _is_decor_char(ch))
    result: list[str] = []
    for raw_line in s.split("\n"):
        line = _MULTI_SPC_RE.sub(" ", raw_line.rstrip()).strip()
        result.append(line)
    return result


# ---------------------------------------------------------------------------
# Phase 2 – lightweight leading-echo strip (safety net)
# ---------------------------------------------------------------------------

def _norm_for_match(s: str) -> str:
    """Normalise a command or line for echo matching."""
    s = _MULTI_SPC_RE.sub(" ", (s or "").strip())
    return s.replace("\\n", "\n").replace("\\t", "\t")


def _line_echoes_command(nc: str, line: str) -> bool:
    """True if *line* looks like a shell echo of the normalised command *nc*."""
    nl = _norm_for_match(line)
    if not nl or not nc:
        return False
    if nl == nc:
        return True
    # Line ends with the command (prompt-prefix + cmd on same line)
    if nl.endswith(nc):
        return True
    # Line is a long-enough prefix of the command (first wrap chunk)
    if nc.startswith(nl) and len(nl) >= 12:
        return True
    # Command is contained inside the line with minimal surrounding text
    if nc in nl and len(nl) - len(nc) <= 20:
        return True
    return False


def _strip_leading_echo(lines: list[str], cmd: str) -> list[str]:
    """Drop leading lines that are an echo of *cmd* (and any wrapped tails).

    This is a safety net only — with raw-byte buffering the echo should not
    normally be present.  We make at most one pass from the top.

    A wrapped-tail continuation is only consumed *after* at least one echo
    line has already been matched, to prevent false-positive stripping of
    lines whose suffix happens to match the command.
    """
    nc = _norm_for_match(cmd)
    if not nc or not lines:
        return lines
    i = 0
    found_echo = False
    while i < len(lines):
        line = lines[i]
        if _line_echoes_command(nc, line):
            found_echo = True
            i += 1
            continue
        # Wrapped tail only once an echo line has been confirmed
        if found_echo:
            nl = _norm_for_match(line)
            if nl and nc.endswith(nl) and len(nl) >= 6:
                i += 1
                continue
        break
    return lines[i:]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def sanitize_pty_command_output(cmd: str, text: str) -> str:
    """Return LLM-ready plain text from a PTY-captured command result.

    Phase 1 strips all ANSI escapes, decorative Unicode, and normalises
    whitespace.  Phase 2 removes a leading command echo if one is present
    (safety net — should not be needed with raw-byte PTY buffering).

    Parameters
    ----------
    cmd:
        The shell command string that was executed.
    text:
        Raw PTY output, as returned by ``run_pty_command`` / the OSC buffer.
    """
    if not (text or "").strip():
        return text or ""

    lines = _clean_lines(text)
    lines = _strip_leading_echo(lines, cmd)

    # Drop leading / trailing blank lines
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()

    out = "\n".join(lines)
    return unicodedata.normalize("NFC", out).rstrip()
