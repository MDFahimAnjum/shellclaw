"""'Is this safe?' command check subcommand.

Analyses a command string using the LLM and prints a plain-text verdict.
Does NOT execute the command — analysis is purely LLM reasoning.
"""

from __future__ import annotations

import asyncio
import sys

from ..config import AppConfig, provider_stream_log_path
from ..utils import TerminalStreamWait
from ..providers import get_provider
from ..providers.base import DeltaDone, DeltaReasoning, DeltaText
from ..session.hardware import load_profile

_CHECK_SYSTEM_PROMPT = """\
You are a Linux safety advisor for everyday users — not developers.
Your job is to explain whether a shell command is safe to run.

Rules:
- Do NOT use markdown formatting for the output. Keep it plain text for terminal readability.
- Do NOT execute the command. Analyse only.
- Avoid verbose sentences. Keep it very short and concise.
- Identify any risk to files, services, or security.
- Give a clear verdict: Safe / Caution / Dangerous / Unknown.


Strictly follow the output format:

Description:
<One-sentence description of what it does>

Impact level: < 🟢 Safe / 🟠 Caution / 🔴 Dangerous / ❓ Unknown >

Explanation:
<Explanation of the impact in one or two sentences>
"""

async def _run_check(command: str, config: AppConfig) -> None:
    check_model = (config.safety.check_model or "").strip()
    model = check_model or config.provider.model
    provider = get_provider(
        name=config.provider.name,
        model=model,
        api_key=config.provider.api_key,
        base_url=config.provider.base_url,
        stream_log_path=provider_stream_log_path(),
        temperature=config.provider.temperature,
        num_ctx=config.provider.num_ctx,
    )

    hardware = load_profile() or {}
    hw_summary = "\n".join(f"- {k}: {v}" for k, v in hardware.items())
    user_message = (
        f"Is this command safe to run?\n\n"
        f"Command: {command}\n\n"
        f"My computer:\n{hw_summary or '(hardware info not available)'}"
    )

    messages = [{"role": "user", "content": user_message}]

    print()
    wait = TerminalStreamWait(enabled=sys.stdout.isatty())
    await wait.start()
    try:
        async for delta in provider.stream(
            messages=messages,
            tools=[],
            system=_CHECK_SYSTEM_PROMPT,
        ):
            if isinstance(delta, DeltaText):
                await wait.stop()
                print(delta.text, end="", flush=True)
            elif isinstance(delta, DeltaReasoning):
                await wait.stop()
            elif isinstance(delta, DeltaDone):
                break
    finally:
        await wait.stop()
    print()


def run_check(command: str, config: AppConfig) -> None:
    """Entry point for `shellclaw check "<command>"`."""
    try:
        asyncio.run(_run_check(command, config))
    except KeyboardInterrupt:
        sys.exit(0)
