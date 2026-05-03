"""OSC marker stripping and pyte feeding (no live shell required)."""

from __future__ import annotations

import pytest

pytest.importorskip("pyte")

import asyncio

from shellclaw.pty_emulator import (
    END_RE,
    CommandEvent,
    OscInterceptor,
    PtyEmulator,
    START_RE,
    TrackedHistoryScreen,
    _commands_match,
    line_suggests_password_prompt,
    probe_line_for_password_prompt,
)
from pyte.streams import ByteStream


def test_commands_match_prefix_symmetry() -> None:
    """Shell hook cmd= can be shorter than our inject buffer (or vice versa)."""
    long = "journalctl -p 3 -r --no-pager -n 200 " + "x" * 120
    short = long[:60]
    assert _commands_match(long, short)
    assert _commands_match(short, long)


def test_dispatch_agent_inject_resolves_when_strings_differ() -> None:
    """OSC preexec line must still complete run_command_and_wait (no false timeout)."""
    emu = PtyEmulator(24, 80)
    try:
        loop = asyncio.new_event_loop()
        fut = loop.create_future()
        emu._inject_waiter = fut
        emu._inject_expected = "echo INJECTED_LINE"
        emu._inject_source = "agent"
        seen: list[str] = []

        def on_complete(ev: object) -> None:
            seen.append(getattr(ev, "source", ""))

        emu.set_callbacks(None, on_complete)
        ev = CommandEvent(
            command="echo ACTUAL_PREEXEC_LINE",
            output="ok\n",
            exit_code=0,
            context_before="",
        )
        emu._dispatch_command_event(ev)
        assert fut.done()
        done = fut.result()
        assert done.source == "agent"
        assert done.command == "echo ACTUAL_PREEXEC_LINE"
        assert done.output == "ok\n"
        assert seen == ["agent"]
    finally:
        loop.close()
        emu.kill()


def test_marker_regexes() -> None:
    b = b"pre\x1b]777;shellclaw_START;cmd=ls -la\x07post"
    m = START_RE.search(b)
    assert m is not None
    assert m.group(1) == b"ls -la"
    e = b"x\x1b]777;shellclaw_END;exit=42\x07y"
    m2 = END_RE.search(e)
    assert m2 is not None
    assert m2.group(1) == b"42"


def test_osc_strip_and_feed() -> None:
    screen = TrackedHistoryScreen(20, 5, history=50)
    stream = ByteStream(screen, strict=False)
    events: list[tuple[str, int]] = []

    def on_start(cmd: str) -> None:
        events.append(("start", cmd))

    def on_end(ev: object) -> None:
        ec = getattr(ev, "exit_code", -1)
        events.append(("end", ec))

    osc = OscInterceptor(screen, stream, on_command_start=on_start, on_command_complete=on_end)
    osc.feed_raw(b"hello \x1b]777;shellclaw_START;cmd=echo hi\x07")
    osc.feed_raw(b" world\x1b]777;shellclaw_END;exit=0\x07")
    disp = "".join(screen.display)
    assert "hello" in disp
    assert "world" in disp
    assert "\x1b]777" not in disp
    assert any(e[0] == "start" for e in events)
    assert any(e == ("end", 0) for e in events)


def test_osc_command_active_between_markers() -> None:
    screen = TrackedHistoryScreen(20, 5, history=50)
    stream = ByteStream(screen, strict=False)
    osc = OscInterceptor(screen, stream)
    assert osc.command_active() is False
    osc.feed_raw(b"\x1b]777;shellclaw_START;cmd=sleep 9\x07")
    assert osc.command_active() is True
    osc.feed_raw(b"\x1b]777;shellclaw_END;exit=130\x07")
    assert osc.command_active() is False


def test_osc_stray_end_ignored() -> None:
    screen = TrackedHistoryScreen(20, 5, history=50)
    stream = ByteStream(screen, strict=False)
    ends: list[int] = []

    def on_end(ev: object) -> None:
        ends.append(getattr(ev, "exit_code", -1))

    osc = OscInterceptor(screen, stream, on_command_complete=on_end)
    osc.feed_raw(b"\x1b]777;shellclaw_END;exit=0\x07")
    assert ends == []
    assert osc.command_active() is False


def test_line_suggests_password_prompt_sudo() -> None:
    assert line_suggests_password_prompt("[sudo] password for alice:")
    assert line_suggests_password_prompt("  [sudo] password for bob:  ")
    assert line_suggests_password_prompt("[sudo] password for fahim:")


def test_line_suggests_password_prompt_colon_endings() -> None:
    assert line_suggests_password_prompt("Password:")
    assert line_suggests_password_prompt("  Passphrase:  ")
    assert line_suggests_password_prompt("PIN for key token:")


def test_line_suggests_password_prompt_negative() -> None:
    assert not line_suggests_password_prompt("")
    assert not line_suggests_password_prompt("echo password=secret")
    assert not line_suggests_password_prompt("$ ls -la")


def test_probe_line_still_sudo_when_prompt_is_active_row() -> None:
    pytest.importorskip("pyte")
    from pyte.streams import ByteStream

    s = TrackedHistoryScreen(80, 4, history=0)
    ByteStream(s, strict=False).feed(b"[sudo] password for bob:")
    assert line_suggests_password_prompt(probe_line_for_password_prompt(s))


def test_force_command_end_synthetic_hook() -> None:
    screen = TrackedHistoryScreen(20, 5, history=50)
    stream = ByteStream(screen, strict=False)
    completed: list[tuple[str, int]] = []

    def on_end(ev: object) -> None:
        completed.append((getattr(ev, "command", ""), getattr(ev, "exit_code", -1)))

    osc = OscInterceptor(screen, stream, on_command_complete=on_end)
    osc.feed_raw(b"\x1b]777;shellclaw_START;cmd=sleep 999\x07")
    assert osc.force_command_end(130) is True
    assert osc.command_active() is False
    assert completed == [("sleep 999", 130)]
    osc.feed_raw(b"\x1b]777;shellclaw_END;exit=0\x07")
    assert completed == [("sleep 999", 130)]
