"""Tests for chat fence parsing helpers (structured command blocks)."""

from __future__ import annotations

from shellclaw.tui.widgets.chat import _SHELL_ACTION_FENCE_RE, _body_is_desc_command_pairs


def test_body_is_desc_command_pairs_valid_single() -> None:
    body = "# List files\nls -la\n"
    assert _body_is_desc_command_pairs(body)


def test_body_is_desc_command_pairs_valid_multi() -> None:
    body = "# First\nls\n# Second\ncat README\n"
    assert _body_is_desc_command_pairs(body)


def test_body_is_desc_command_pairs_ignores_blank_lines() -> None:
    body = "# Show disk\n\n\ndf -h\n"
    assert _body_is_desc_command_pairs(body)


def test_body_is_desc_command_pairs_rejects_plain_command() -> None:
    assert not _body_is_desc_command_pairs("ls -la")


def test_body_is_desc_command_pairs_rejects_desc_without_command() -> None:
    assert not _body_is_desc_command_pairs("# only a description")


def test_body_is_desc_command_pairs_rejects_trailing_garbage() -> None:
    assert not _body_is_desc_command_pairs("# ok\nls\nextra")


def test_body_is_desc_command_pairs_rejects_empty_desc() -> None:
    assert not _body_is_desc_command_pairs("#\nls\n")


def test_fence_regex_matches_two_shell_fences() -> None:
    text = "x\n```bash\n# a\nb\n```\ny\n```zsh\n# c\nd\n```\n"
    ms = list(_SHELL_ACTION_FENCE_RE.finditer(text))
    assert len(ms) == 2
    assert ms[0].group("body").strip() == "# a\nb"
    assert ms[1].group("body").strip() == "# c\nd"


def test_fence_regex_case_insensitive_tag() -> None:
    m = _SHELL_ACTION_FENCE_RE.search("```BASH\n# x\ny\n```")
    assert m is not None
    assert m.group("body").strip() == "# x\ny"
