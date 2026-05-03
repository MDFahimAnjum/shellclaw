"""Extract Qwen-style XML tool calls from model reasoning text.

Some OpenAI-compatible backends stream ``<tool_call>...</tool_call>`` inside a
``reasoning`` delta instead of native ``tool_calls``. We parse the last
complete block from the end of the buffer and turn it into structured calls.
"""

from __future__ import annotations

import re
import uuid

from .base import DeltaToolCall

# Last complete <tool_call>...</tool_call> (non-greedy inner match from the end).
_TOOL_CALL_BLOCK = re.compile(
    r"<tool_call\s*>(.*?)</tool_call\s*>",
    re.IGNORECASE | re.DOTALL,
)
_FUNCTION_OPEN = re.compile(r"<function\s*=\s*(\w+)\s*>", re.IGNORECASE)
_PARAMETER = re.compile(
    r"<parameter\s*=\s*(\w+)\s*>(.*?)</parameter\s*>",
    re.IGNORECASE | re.DOTALL,
)


def reasoning_has_tool_call_markup(reasoning: str) -> bool:
    """True if *reasoning* contains Qwen-style tool XML (parseable or not)."""
    if not reasoning:
        return False
    return "<tool_call" in reasoning.lower()


def parse_reasoning_xml_tool_calls(reasoning: str) -> list[DeltaToolCall]:
    """Parse the last well-formed XML tool call block from *reasoning* (scan from end)."""
    if not reasoning or "</tool_call" not in reasoning.lower():
        return []

    matches = list(_TOOL_CALL_BLOCK.finditer(reasoning))
    if not matches:
        return []

    # Prefer the last match that contains a <function=name> tag.
    for m in reversed(matches):
        block = m.group(1)
        fn_m = _FUNCTION_OPEN.search(block)
        if not fn_m:
            continue
        name = fn_m.group(1)
        arguments: dict[str, str] = {}
        for pm in _PARAMETER.finditer(block):
            key = pm.group(1)
            val = pm.group(2).strip()
            arguments[key] = val
        call_id = f"call_{uuid.uuid4().hex[:8]}"
        return [
            DeltaToolCall(
                call_id=call_id,
                name=name,
                arguments=arguments,
            )
        ]

    return []
