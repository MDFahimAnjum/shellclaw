"""Tests for PTY output sanitisation.

With the raw-byte OscInterceptor buffering, the sanitiser input is clean
(no stale scrollback, no multi-segment blobs, no pyte column padding).
Tests here validate:
  - Phase 1: ANSI/escape stripping and whitespace normalisation
  - Phase 2: lightweight leading-echo safety-net strip
  - Pass-through for plain subprocess output (no PTY noise at all)
"""

from shellclaw.pty_sanitize import (
    sanitize_pty_command_output,
    strip_ansi,
    _clean_lines,
)


# ---------------------------------------------------------------------------
# Phase 1: strip_ansi
# ---------------------------------------------------------------------------

def test_strip_ansi_csi_sgr():
    assert strip_ansi("\x1b[31mred\x1b[0m") == "red"


def test_strip_ansi_osc_bel():
    assert strip_ansi("\x1b]0;title\x07plain") == "plain"


def test_strip_ansi_osc_shellclaw_marker():
    raw = "\x1b]777;shellclaw_START;cmd=ls\x07hello"
    assert strip_ansi(raw) == "hello"


def test_strip_ansi_ss3_consumes_argument_byte():
    # \x1bOP = F1 key (SS3 + P); must not leave stray 'P' behind
    assert strip_ansi("\x1bOPtext") == "text"


def test_strip_ansi_csi_and_osc_combined():
    raw = "\x1b[31mred\x1b[0m\n\x1b]0;title\x07plain"
    assert strip_ansi(raw) == "red\nplain"


# ---------------------------------------------------------------------------
# Phase 1: _clean_lines (ANSI + decor + whitespace normalisation)
# ---------------------------------------------------------------------------

def test_clean_lines_removes_box_drawing():
    lines = _clean_lines("╭─ hello ─╮\n╰─ world ─╯\n")
    assert all("╭" not in l and "╰" not in l and "─" not in l for l in lines)


def test_clean_lines_collapses_column_padding():
    raw = "Filesystem      Size  Used Avail Use% Mounted on                             \n"
    lines = _clean_lines(raw)
    assert lines[0] == "Filesystem Size Used Avail Use% Mounted on"


def test_clean_lines_normalises_crlf():
    lines = _clean_lines("a\r\nb\rc\n")
    assert lines[:3] == ["a", "b", "c"]


def test_clean_lines_strips_powerline_private_use():
    # U+E0B0 is a common powerline arrow glyph
    lines = _clean_lines("\ue0b0 hello \ue0b0\n")
    assert "hello" in lines[0]
    assert "\ue0b0" not in lines[0]


def test_clean_lines_collapses_spaces():
    lines = _clean_lines("a   b    c\n")
    assert lines[0] == "a b c"


# ---------------------------------------------------------------------------
# Raw-buffer shaped input: clean output with ANSI only (typical new path)
# ---------------------------------------------------------------------------

def test_list_dir_clean_ansi_only():
    """Typical raw-buffer output: just ANSI codes in the ls listing."""
    blob = (
        "\x1b[0m\x1b[01;34mfahim\x1b[0m\n"
        "\x1b[01;34mlost+found\x1b[0m\n"
        "timeshift\n"
    )
    out = sanitize_pty_command_output(
        "find /home -maxdepth 1 -mindepth 1 -printf '%y  %12s  %f\\n' | sort | head -n 200",
        blob,
    )
    assert "fahim" in out
    assert "lost+found" in out
    assert "timeshift" in out
    # No escape codes
    assert "\x1b" not in out


def test_disk_usage_clean_output():
    """Raw-buffer df+du output: ANSI color on numbers stripped cleanly."""
    blob = (
        "\x1b[1mFilesystem      Size  Used Avail Use% Mounted on\x1b[0m\n"
        "/dev/nvme0n1p2  137G   70G   61G  54% /\n"
        "66G     .\n"
        "15G     ./var\n"
    )
    cmd = "df -h --output=source,size,used,avail,pcent,target / && du -xh -d 1 / 2>/dev/null | sort -hr | head -n 11"
    out = sanitize_pty_command_output(cmd, blob)
    assert "Filesystem" in out
    assert "nvme0n1p2" in out
    assert "66G" in out
    assert "./var" in out
    assert "\x1b" not in out


# ---------------------------------------------------------------------------
# Phase 2 safety-net: leading echo strip
# ---------------------------------------------------------------------------

def test_echo_strip_exact_match():
    """If the command echo appears on line 1 (unusual shell), strip it."""
    cmd = "ls /tmp"
    blob = "ls /tmp\nfile1\nfile2\n"
    out = sanitize_pty_command_output(cmd, blob)
    assert "file1" in out
    assert "file2" in out
    assert "ls /tmp" not in out


def test_echo_strip_prompt_prefix():
    """Bash $ prompt + command on same line."""
    out = sanitize_pty_command_output("ls /tmp", "$ ls /tmp\nfile1\nfile2\n$ ")
    assert "file1" in out
    assert "file2" in out


def test_echo_strip_starship_with_ansi():
    """With raw-byte buffering the prompt never enters the buffer.
    The blob here represents only the command output with ANSI colors."""
    cmd = "ls /home"
    # Raw buffer: no prompt lines, just coloured output
    blob = (
        "\x1b[01;34mfahim\x1b[0m\n"
        "\x1b[01;34mguest\x1b[0m\n"
    )
    out = sanitize_pty_command_output(cmd, blob)
    assert "fahim" in out
    assert "guest" in out
    assert "\x1b" not in out


def test_echo_strip_starship_legacy_blob():
    """Phase 1 removes box-drawing chars and ANSI escapes from a Starship blob.

    With raw-byte buffering this blob never appears in practice (the prompt
    renders after the OSC END marker).  The simplified Phase 2 echo-strip
    makes a single pass from the top: it stops at the first non-echo line
    ('/home' from the prompt fragment), so the 'ls /home' echo line below it
    is NOT stripped — that is acceptable given the legacy scenario is gone.
    The important guarantee is that the actual output (fahim, guest) is present
    and all decoration is cleaned.
    """
    cmd = "ls /home"
    blob = (
        "\x1b[32m╭─ /home ─╮\x1b[0m\n"
        "\x1b[32m╰─ ls /home ─╯\x1b[0m\n"
        "fahim\n"
        "guest\n"
    )
    out = sanitize_pty_command_output(cmd, blob)
    assert "fahim" in out
    assert "guest" in out
    assert "╭" not in out
    assert "\x1b" not in out


# ---------------------------------------------------------------------------
# Plain / subprocess-style output (no PTY noise) passes through intact
# ---------------------------------------------------------------------------

def test_plain_output_no_echo_passes_through():
    cmd = "df -h"
    blob = "Filesystem  Size  Used\n/dev/sda1   100G   50G\n"
    out = sanitize_pty_command_output(cmd, blob)
    assert "Filesystem" in out
    assert "/dev/sda1" in out


def test_plain_echo_hello():
    out = sanitize_pty_command_output("echo hello", "hello\n")
    assert out == "hello"


def test_short_command_content_not_stripped():
    # 'ls' is too short (< 12 chars) to trigger the prefix-match heuristic
    out = sanitize_pty_command_output("ls", "files\nmore_files\n")
    assert "files" in out
    assert "more_files" in out


def test_no_prompt_subprocess():
    out = sanitize_pty_command_output("free -h", "Mem:  30Gi  10Gi  20Gi\n")
    assert "30Gi" in out


def test_empty_text_returns_empty():
    assert sanitize_pty_command_output("ls", "") == ""


def test_all_blank_returns_empty():
    assert sanitize_pty_command_output("ls", "   \n\n  ").strip() == ""


def test_short_cmd_error_message_preserved():
    out = sanitize_pty_command_output("ls", "ls: cannot access '/x': No such file\n")
    assert "cannot access" in out
