"""Tests for Qwen-style XML tool calls embedded in reasoning."""

from shellclaw.providers.reasoning_tool_xml import (
    parse_reasoning_xml_tool_calls,
    reasoning_has_tool_call_markup,
)


def test_parse_last_tool_call_from_end_qwen_log_shape():
    """Shape observed in Ollama qwen3.5 stream (reasoning delta)."""
    reasoning = (
        "Let me try a different way to check if Brave is installed.\n\n"
        "<tool_call>\n"
        "<function=run_safe>\n"
        "<parameter=cmd>\n"
        "dpkg -l | grep brave\n"
        "</parameter>\n"
        "</function>\n"
        "</tool_call>"
    )
    calls = parse_reasoning_xml_tool_calls(reasoning)
    assert len(calls) == 1
    assert calls[0].name == "run_safe"
    assert calls[0].arguments == {"cmd": "dpkg -l | grep brave"}
    assert calls[0].call_id.startswith("call_")


def test_parse_prefers_last_block_when_multiple():
    reasoning = (
        "<tool_call><function=a><parameter=x>1</parameter></function></tool_call>"
        " noise "
        "<tool_call><function=run_safe><parameter=cmd>which z</parameter></function></tool_call>"
    )
    calls = parse_reasoning_xml_tool_calls(reasoning)
    assert len(calls) == 1
    assert calls[0].name == "run_safe"
    assert calls[0].arguments["cmd"] == "which z"


def test_parse_empty_without_tool_call():
    assert parse_reasoning_xml_tool_calls("") == []
    assert parse_reasoning_xml_tool_calls("just thinking") == []


def test_reasoning_has_tool_call_markup():
    assert not reasoning_has_tool_call_markup("")
    assert not reasoning_has_tool_call_markup("Just thinking out loud.")
    assert reasoning_has_tool_call_markup("<tool_call>")
    assert reasoning_has_tool_call_markup("<TOOL_CALL>\n")


def test_parse_case_insensitive_tags():
    reasoning = (
        "<TOOL_CALL><FUNCTION=run_safe><PARAMETER=cmd>uname -a</PARAMETER></FUNCTION></TOOL_CALL>"
    )
    calls = parse_reasoning_xml_tool_calls(reasoning)
    assert len(calls) == 1
    assert calls[0].name == "run_safe"
    assert calls[0].arguments["cmd"] == "uname -a"
