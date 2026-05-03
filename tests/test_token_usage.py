"""Tests for stripping / parsing ``total_tokens`` from model text."""

from __future__ import annotations

from shellclaw.token_usage import format_tokens_compact, split_trailing_token_usage


def test_no_tokens_returns_original() -> None:
    body, total = split_trailing_token_usage("Hello world.")
    assert body == "Hello world."
    assert total is None


def test_trailing_json_stripped_and_total() -> None:
    raw = 'Done.\n\n{"prompt_tokens": 1, "total_tokens": 3421}'
    body, total = split_trailing_token_usage(raw)
    assert body == "Done."
    assert total == 3421


def test_total_tokens_quoted_key() -> None:
    raw = "OK\n{'total_tokens': 99}"
    body, total = split_trailing_token_usage(raw)
    assert body == "OK"
    assert total == 99


def test_mid_text_tokens_no_strip_but_reports_total() -> None:
    raw = 'See total_tokens": 12 in this sentence.'
    body, total = split_trailing_token_usage(raw)
    assert body == raw
    assert total == 12


def test_format_tokens_compact() -> None:
    assert format_tokens_compact(0) == "0"
    assert format_tokens_compact(42) == "42"
    assert format_tokens_compact(1000) == "1k"
    assert format_tokens_compact(1500) == "1.5k"
    assert format_tokens_compact(2_500_000) == "2.5M"
