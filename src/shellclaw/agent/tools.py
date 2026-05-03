"""Tool definitions and dispatch for the observe loop.

Each tool is described by a JSON schema (for the LLM) and backed by an
async handler function.  ``observe_tool_schemas(advanced_toolset)`` is passed
to the provider on each request.  dispatch() maps tool name to handler for execution.
Pass terminal_log= for terminal_history_* tools (optional for others).
Pass terminal_snapshot= for ``terminal_screen_snapshot`` when running inside the TUI.
Pass pty_stop= for ``stop_execution`` (same as the side terminal Stop button).
"""

from __future__ import annotations

import asyncio
import shlex
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from ..config import load_config, parse_extra_allowed_command_bases
from ..safety.sandbox import (
    SandboxError,
    dispatch_extra_allowed_command_bases,
    dispatch_pty_runner,
    run_safe,
)
from ..safety.web_search_safety import validate_web_search_query
from .terminal_history import TerminalHistoryStore

# GNU journalctl + awk: relative age + message (task-tools.md). Masked in sandbox blocked-char scan.
JOURNAL_RELTIME_AWK = (
    r'{ age=int(systime()-$1); r=(age<60)?age"s":(age<3600)?int(age/60)"m":(age<86400)?int(age/3600)"h":int(age/86400)"d"; '
    r'printf "[%s]\t%s\n", r, substr($0,index($0,$3)) }'
)

_JOURNAL_MAX_LINES = 50

# network_info: awk argv strings (contain </>/{}; masked in sandbox blocked-char scan).
# Single-line programs avoid PTY/shell splitting mid-token (e.g. \047).
_NETWORK_INFO_PORTS_AWK = (
    "NR>1 { split($5,a,\":\"); port=a[length(a)]+0; "
    "if(port>0 && port<10000) print $1, a[length(a)] }"
)


def _network_info_svc_awk() -> str:
    # cmd=…|awk '{print $1}' built from awk string literals. \047 must sit *inside*
    # double-quoted awk strings; if it appears between literals awk sees a bare \ (error).
    _o = chr(92) + "047"
    awk_str3 = '"' + " 2>/dev/null | awk " + _o + '"'
    awk_str5 = '"' + _o + '"'
    return (
        '{ cmd = "getent services " $2 '
        + awk_str3
        + ' "{print $1}" '
        + awk_str5
        + "; cmd | getline svc; close(cmd); "
        "entry = $2 (svc ? \"(\" svc \")\" : \"\"); "
        "ports[$1] = ports[$1] (ports[$1] ? \" \" : \"\") entry } "
        'END { for (p in ports) print p ": " ports[p] }'
    )


def _network_info_command() -> tuple[str, tuple[str, ...]]:
    qp = shlex.quote(_NETWORK_INFO_PORTS_AWK)
    qs = shlex.quote(_network_info_svc_awk())
    cmd = (
        f'echo "=PORTS=" && ss -tuln | awk {qp} | sort -u | awk {qs} | sort && '
        f'echo "=INTERFACES=" && ip -brief addr show | grep -v {shlex.quote("^lo")}'
    )
    masks = tuple(sorted((qp, qs), key=len, reverse=True))
    return cmd, masks


# ---------------------------------------------------------------------------
# Safe-file read configuration
# ---------------------------------------------------------------------------


FILE_SIZE_CAP = 16384  # 16KB per file read

# Live pyte viewport text returned to the model (avoid huge tool payloads).
_TERMINAL_SNAPSHOT_MAX_CHARS = 24_000


def _agent_find_limits() -> tuple[int, int]:
    a = load_config().agent
    return a.find_max_depth, a.find_max_results


def _agent_web_search_settings() -> tuple[int, str, str, str | None]:
    """DDGS parameters from config (1–10 results, validated safesearch/timelimit)."""
    a = load_config().agent
    try:
        n = int(a.web_search_max_results)
    except (TypeError, ValueError):
        n = 5
    n = max(1, min(n, 10))
    region = (a.web_search_region or "us-en").strip() or "us-en"
    ss = str(a.web_search_safesearch or "moderate").strip().lower()
    if ss not in ("off", "moderate", "strict"):
        ss = "moderate"
    tl_raw = a.web_search_timelimit
    if tl_raw is None or (isinstance(tl_raw, str) and not tl_raw.strip()):
        timelimit: str | None = None
    else:
        t = str(tl_raw).strip().lower()
        timelimit = t if t in ("d", "w", "m", "y") else None
    return n, region, ss, timelimit


def _clamp_journal_length(value: object, default: int) -> int:
    try:
        x = int(value) if value is not None else default
    except (TypeError, ValueError):
        x = default
    return max(1, min(x, _JOURNAL_MAX_LINES))


def _clamp_find_int(value: object, default: int, lo: int, hi: int) -> int:
    try:
        if value is None:
            return max(lo, min(default, hi))
        x = int(value)
    except (TypeError, ValueError):
        x = default
    return max(lo, min(x, hi))


def _find_printf_format() -> str:
    """GNU find -printf format: path relative to search dir, ./ prefix, newline per match."""
    return "./%P\\n"


async def _exact_find_handler(
    search_dir: str,
    target: str,
    max_results: int,
    max_depth: int,
) -> str:
    root = (search_dir or "").strip()
    name = (target or "").strip()
    if not root:
        return "[error] search_dir is required."
    if not name:
        return "[error] target name is required."
    max_d, max_n = _agent_find_limits()
    depth = _clamp_find_int(max_depth, 5, 1, max_d)
    n = _clamp_find_int(max_results, 10, 1, max_n)
    fmt = _find_printf_format()
    cmd = (
        f"find {shlex.quote(root)} -maxdepth {depth} -iname {shlex.quote(name)} "
        f"-printf {shlex.quote(fmt)} 2>/dev/null | head -n {n}"
    )
    return await _run_safe_handler(cmd)


async def _fuzzy_find_handler(
    mode: str,
    search_dir: str,
    target: str,
    max_results: int,
    max_depth: int,
) -> str:
    root = (search_dir or "").strip()
    needle = (target or "").strip()
    if not root:
        return "[error] search_dir is required."
    if not needle:
        return "[error] target name is required."
    max_d, max_n = _agent_find_limits()
    depth = _clamp_find_int(max_depth, 5, 1, max_d)
    n = _clamp_find_int(max_results, 10, 1, max_n)
    fmt = _find_printf_format()
    m = (mode or "normal").strip().lower()
    if m == "normal":
        pattern = f"*{needle}*"
        cmd = (
            f"find {shlex.quote(root)} -maxdepth {depth} -iname {shlex.quote(pattern)} "
            f"-printf {shlex.quote(fmt)} 2>/dev/null | head -n {n}"
        )
    elif m == "broad":
        cmd = (
            f"find {shlex.quote(root)} -maxdepth {depth} -printf {shlex.quote(fmt)} "
            f"2>/dev/null | grep -i {shlex.quote(needle)} | head -n {n}"
        )
    else:
        return "[error] mode must be 'normal' or 'broad'."
    return await _run_safe_handler(cmd)


async def _find_recent_handler(
    search_dir: str,
    max_results: int,
    max_depth: int,
) -> str:
    """Files/dirs under search_dir newer than /tmp's mtime; compact timestamp + relative path."""
    root = (search_dir or "").strip()
    if not root:
        return "[error] search_dir is required."
    max_d, max_n = _agent_find_limits()
    depth = _clamp_find_int(max_depth, 4, 1, max_d)
    n = _clamp_find_int(max_results, 10, 1, max_n)
    fmt = r"%TY%Tm%Td%TH%TM\t./%P\n"
    ref = "/tmp"
    cmd = (
        f"find {shlex.quote(root)} -maxdepth {depth} -type f -newer {shlex.quote(ref)} "
        f"-printf {shlex.quote(fmt)} 2>/dev/null | sort -r | head -n {n}"
    )
    return await _run_safe_handler(cmd)


async def _find_largest_handler(
    search_dir: str,
    max_results: int,
    max_depth: int,
) -> str:
    """Largest regular files under search_dir; size in MB (numfmt) and ./relative/path."""
    root = (search_dir or "").strip()
    if not root:
        return "[error] search_dir is required."
    max_d, max_n = _agent_find_limits()
    depth = _clamp_find_int(max_depth, 5, 1, max_d)
    n = _clamp_find_int(max_results, 10, 1, max_n)
    fmt = r"%s\t./%P\n"
    cmd = (
        f"find {shlex.quote(root)} -maxdepth {depth} -type f "
        f"-printf {shlex.quote(fmt)} 2>/dev/null | sort -rn | "
        f"numfmt --field=1 --to-unit=1048576 --format={shlex.quote('%.1fM')} | head -n {n}"
    )
    return await _run_safe_handler(cmd)


# ---------------------------------------------------------------------------
# Handler functions
# ---------------------------------------------------------------------------

def _tool_shell_cmd(arguments: dict) -> str:
    """Best-effort command line from tool JSON (Ollama sometimes uses ``command`` not ``cmd``)."""
    for key in ("cmd", "command", "shell", "line"):
        raw = arguments.get(key)
        if raw is None:
            continue
        s = str(raw).strip()
        if s:
            return s
    return ""


async def _run_safe_handler(
    cmd: str,
    *,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
    pty_runner: Callable[[str, int], Awaitable[tuple[str, int]]] | None = None,
) -> str:
    try:
        return await run_safe(cmd, stream_line=stream_line, pty_runner=pty_runner)
    except SandboxError as exc:
        return f"[blocked] {exc}"


async def _read_file_handler(path: str) -> str:
    p = Path(path).resolve()
    path_str = str(p)

    if not p.exists():
        return f"[not found] {path!r} does not exist."
    if not p.is_file():
        return f"[error] {path!r} is not a regular file."

    q = shlex.quote(path_str)
    cmd = f"head -c {FILE_SIZE_CAP} -- {q}"
    return await _run_safe_handler(cmd)


async def _list_dir_handler(path: str) -> str:
    """List directory via ``find`` + ``sort`` (same PTY path as other shell tools)."""
    raw = (path or "").strip()
    if not raw:
        return "[error] path is required."
    try:
        root = str(Path(raw).expanduser().resolve())
    except (OSError, RuntimeError) as exc:
        return f"[error] Invalid path: {exc}"
    p = Path(root)
    if not p.is_dir():
        return f"[error] {root!r} is not a directory."
    fmt = r"%y  %12s  %f\n"
    cmd = (
        f"find {shlex.quote(root)} -maxdepth 1 -mindepth 1 "
        f"-printf {shlex.quote(fmt)} | sort | head -n 200"
    )
    return await _run_safe_handler(cmd)


def _sed_bre_path_literal(s: str) -> str:
    """Escape *s* as a literal substring in a sed BRE (for path prefixes in du output)."""
    out: list[str] = []
    for c in s:
        if c == "\\":
            out.append("\\\\")
        elif c in r"^$.*+?[](){}|" or c == "\t":
            out.append("\\" + c)
        else:
            out.append(c)
    return "".join(out)


def format_disk_usage_command(path: str) -> str:
    """Shell command string for :func:`_disk_usage_handler` (for UI display)."""
    p = str(Path(path).expanduser().resolve())
    q = shlex.quote(p)
    if not p.rstrip("/"):
        sed_arg = r"s|\t/$|\t.|; s|\t/|\t./|"
    else:
        pat = _sed_bre_path_literal(p)
        sed_arg = f"s|\\t{pat}/|\\t./|; s|\\t{pat}$|\\t.|"
    return (
        f"df -h --output=source,size,used,avail,pcent,target {q} "
        f"&& du -xh -d 1 {q} 2>/dev/null | sort -hr | head -n 11 | "
        f"sed {shlex.quote(sed_arg)}"
    )


async def _disk_usage_handler(path: str) -> str:
    return await run_safe(format_disk_usage_command(path))


async def _process_list_handler() -> str:
    return await run_safe("ps -eo pid,user,%cpu,%mem,comm --sort=-%cpu | head -20") 


def _journal_reltime_quoted() -> str:
    return shlex.quote(JOURNAL_RELTIME_AWK)


def _journal_dedupe_stage() -> str:
    # Use awk's escape in -F '\t' instead of a literal tab in the shell word.
    # A literal tab inside shlex.quote(chr(9)) can be dropped when zsh preexec
    # puts the command in OSC 777 cmd=…, yielding awk -F '' and awk syntax errors.
    return r"awk -F '\t' " + shlex.quote("!seen[$2]++")


async def _journal_logs_handler(
    mode: str,
    length: object,
    time_window: str | None,
    query: str | None,
    unit: str | None,
) -> str:
    m = (mode or "").strip().replace("-", "_").lower()
    rel_q = _journal_reltime_quoted()
    dedupe = _journal_dedupe_stage()
    masks = (rel_q,)

    if m == "recent_errors":
        n = _clamp_journal_length(length, 15)
        cmd = (
            f"journalctl -p 3 -r --no-pager -n 200 -o short-unix | "
            f"awk {rel_q} | {dedupe} | head -n {n}"
        )
        return await run_safe(cmd, validation_mask_fragments=masks)

    if m == "frequent_errors":
        n = _clamp_journal_length(length, 10)
        tw = (time_window or "").strip() or "24h ago"
        cmd = (
            f"journalctl -p 3 --no-pager --since {shlex.quote(tw)} -o cat | "
            r"sed 's/\[[0-9]*\]//g' | sort | uniq -c | sort -nr | "
            f"head -n {n}"
        )
        return await run_safe(cmd)

    if m == "boot_errors":
        n = _clamp_journal_length(length, 15)
        cmd = (
            f"journalctl -b -p 3 -r --no-pager -o short-unix | "
            f"awk {rel_q} | {dedupe} | head -n {n}"
        )
        return await run_safe(cmd, validation_mask_fragments=masks)

    if m == "time_window":
        tw = (time_window or "").strip()
        if not tw:
            return "[error] time_window is required for mode 'time_window'."
        n = _clamp_journal_length(length, 15)
        cmd = (
            f"journalctl --since {shlex.quote(tw)} -r --no-pager -o short-unix | "
            f"awk {rel_q} | {dedupe} | head -n {n}"
        )
        return await run_safe(cmd, validation_mask_fragments=masks)

    if m == "search":
        q = (query or "").strip()
        if not q:
            return "[error] query is required for mode 'search'."
        if len(q.split()) != 1:
            return (
                "[error] query must be a single term (one word, no spaces): "
                "journalctl --grep with multiple terms is extremely slow on the "
                "journal index and often times out. Use one token (e.g. 'slack'), "
                "or use mode 'time_window' / 'frequent_errors' instead."
            )
        n = _clamp_journal_length(length, 15)
        cmd = (
            f"journalctl -r --no-pager --grep={shlex.quote(q)} -o short-unix | "
            f"awk {rel_q} | {dedupe} | head -n {n}"
        )
        return await run_safe(cmd, validation_mask_fragments=masks)

    if m == "search_service":
        u = (unit or "").strip()
        if not u:
            return "[error] unit is required for mode 'search_service'."
        n = _clamp_journal_length(length, 15)
        cmd = (
            f"journalctl -u {shlex.quote(u)} -r -n {n} --no-pager -o short-unix | "
            f"awk {rel_q}"
        )
        return await run_safe(cmd, validation_mask_fragments=masks)

    return (
        f"[error] Unknown mode {mode!r}. Use: recent_errors, frequent_errors, "
        f"boot_errors, time_window, search, or search_service."
    )


async def _service_status_handler(unit: str) -> str:
    return await _run_safe_handler(f"systemctl status {unit} --no-pager")


async def _network_info_handler() -> str:
    cmd, masks = _network_info_command()
    return await run_safe(cmd, validation_mask_fragments=masks)


async def _get_distro_info_handler() -> str:
    return await _read_file_handler("/etc/os-release")


def _run_ddgs_text(
    query: str,
    max_results: int,
    region: str,
    safesearch: str,
    timelimit: str | None,
) -> str:
    from ddgs import DDGS

    lines: list[str] = []
    kwargs: dict = {
        "region": region,
        "safesearch": safesearch,
        "max_results": max_results,
    }
    if timelimit:
        kwargs["timelimit"] = timelimit

    with DDGS() as ddgs:
        for i, row in enumerate(ddgs.text(query, **kwargs)):
            if i >= max_results:
                break
            title = row.get("title", "").strip()
            href = row.get("href", "").strip()
            body = row.get("body", "").strip()
            lines.append(f"{i + 1}. {title}\n   {href}\n   {body}")

    if not lines:
        return "No results found."
    return "\n\n".join(lines)


def _terminal_screen_snapshot_text(
    snap: Mapping[str, str],
    *,
    max_chars: int = _TERMINAL_SNAPSHOT_MAX_CHARS,
) -> str:
    mode = (snap.get("mode") or "normal").strip() or "normal"
    raw = snap.get("content") or ""
    truncated = False
    if len(raw) > max_chars:
        raw = raw[:max_chars]
        truncated = True
    note = (
        "The text below is the current visible terminal viewport (what fits on the live "
        "terminal panel), not full scrollback.\n"
        f"mode={mode!r} means {'alternate/full-screen style buffer' if mode == 'interactive' else 'normal scrolling buffer'}."
    )
    if truncated:
        note += f"\n\n[Output truncated to {max_chars} characters.]"
    body = raw if raw.strip() else "(empty viewport — blank or whitespace only)"
    return f"{note}\n\n---\n{body}"


async def _stop_execution_handler(pty_stop: Callable[[], str] | None) -> str:
    if pty_stop is None:
        return "[error] Stopping the live terminal is not available in this environment."
    return pty_stop()


async def _terminal_screen_snapshot_handler(
    terminal_snapshot: Callable[[], dict[str, str]] | None,
) -> str:
    if terminal_snapshot is None:
        return "[error] Live terminal snapshot is not available in this environment."
    try:
        snap = terminal_snapshot()
    except Exception as exc:
        return f"[error] Terminal snapshot failed: {exc}"
    if not isinstance(snap, Mapping):
        return "[error] Terminal snapshot returned an invalid value."
    return _terminal_screen_snapshot_text(snap)


async def _web_search_handler(query: str) -> str:
    ok, err = validate_web_search_query(query)
    if not ok:
        return f"[blocked] {err}"

    n, region, safesearch, timelimit = _agent_web_search_settings()
    try:
        text = await asyncio.to_thread(
            _run_ddgs_text,
            query.strip(),
            n,
            region,
            safesearch,
            timelimit,
        )
        return text
    except Exception as exc:
        return f"[error] Web search failed: {exc}"


# ---------------------------------------------------------------------------
# JSON schemas — passed to the LLM on every request (observation tools only).
# The LLM proposes fixes via <proposed_actions> in its text, not a tool.
# ---------------------------------------------------------------------------

_find_depth_ceiling, _find_results_ceiling = _agent_find_limits()

OBSERVE_TOOL_SCHEMAS_V0: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_safe",
            "description": (
                "Run a read-only shell command, optionally as a pipeline (a | b | c). \
                No redirects or sudo. \n \
                This tool is the last resort. Use combination of the other tools instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "The command to run, e.g. 'free -h'.",
                    }
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a system file (config, log, proc entry). Size-capped at 16KB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory with file sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute directory path."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disk_usage",
            "description": (
                "Show free/used space for the filesystem that contains this path, "
                "and the largest immediate subdirectories (du). "
                "Directory paths in the du section are relative to the given path (e.g. . and ./name)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path whose mount to report, e.g. '/', '/home', '/var'.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "List running programs sorted by CPU usage (top 20).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal_logs",
            "description": (
                "View and search systemd journal (journalctl) logs. "
                "Mode 'search' accepts only a single-token --grep query (no spaces); "
                "multi-word grep is disallowed because it is too slow and may time out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": (
                            "recent_errors: recent errors (deduped), frequent_errors: frequent errors (counts by message, PID-stripped), boot_errors: errors since boot, time_window: all priorities in a since window, search: single-term grep on journal index, search_service: one service, relative time"
                        ),
                        "enum": [
                            "recent_errors",
                            "frequent_errors",
                            "boot_errors",
                            "time_window",
                            "search",
                            "search_service",
                        ],
                    },
                    "length": {
                        "type": "integer",
                        "description": (
                            f"Max lines for head -n (or journalctl -n for search_service). "
                            f"Default 15 (10 for frequent_errors). Clamped 1–{_JOURNAL_MAX_LINES}."
                        ),
                    },
                    "time_window": {
                        "type": "string",
                        "description": (
                            "journalctl --since string. Required for time_window; optional for "
                            "frequent_errors (default '24h ago'). Examples: '10 min ago', '24h ago'."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "journalctl --grep for mode 'search' only: exactly one word/token, "
                            "no whitespace (multi-word patterns are disallowed — too slow, may time out)."
                        ),
                    },
                    "unit": {
                        "type": "string",
                        "description": "Service for journalctl -u; required for search_service.",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_status",
            "description": "Get the current status of a background service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "description": "Service name, e.g. 'nginx.service'."}
                },
                "required": ["unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "network_info",
            "description": "Show open network connections and network interface addresses.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_distro_info",
            "description": "Read /etc/os-release to identify the Linux distribution and version.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for documentation, error messages, "
                "version facts, or general Linux help. Use short factual queries — never "
                "paste passwords, API keys, private paths, or personal details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up, e.g. 'ubuntu 24.04 install nginx'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exact_find",
            "description": (
                "Locate/find a file or directory whose name matches exactly (case-insensitive) under "
                "a search directory. Returns paths relative to the search root (./...)"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_dir": {
                        "type": "string",
                        "description": "Directory to search (e.g. '/', '/home', or a project path).",
                    },
                    "target": {
                        "type": "string",
                        "description": "File or folder name to match (exact, case-insensitive).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"Max paths to return (1–{_find_results_ceiling}). Default 10."
                        ),
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": (
                            f"find -maxdepth (1–{_find_depth_ceiling}). Default 5; limits how deep "
                            "to walk the tree."
                        ),
                    },
                },
                "required": ["search_dir", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fuzzy_find",
            "description": (
                "Find files or folders by partial name match under a directory. "
                "Output paths are relative to the search root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "'normal' for partial name matches (glob *target*) or 'broad' for partial matches anywhere in the path",
                        "enum": ["normal", "broad"],
                    },
                    "search_dir": {
                        "type": "string",
                        "description": "Directory to search.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Substring or pattern hint to match in paths (case-insensitive).",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"Max paths to return (1–{_find_results_ceiling}). Default 10."
                        ),
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": (
                            f"find -maxdepth (1–{_find_depth_ceiling}). Default 5."
                        ),
                    },
                },
                "required": ["mode", "search_dir", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_recent",
            "description": (
                "List recently modified files and directories under a path."
                "very recent changes. Each line: compact timestamp YYYYMMDDHHMM then tab then "
                "./relative/path, sorted newest first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_dir": {
                        "type": "string",
                        "description": "Directory tree to scan.",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": (
                            f"find -maxdepth (1–{_find_depth_ceiling}). Default 4."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"Max lines to return (1–{_find_results_ceiling}). Default 10."
                        ),
                    },
                },
                "required": ["search_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_largest",
            "description": (
                "List the largest regular files under a directory. Output is size in megabytes "
                "(one decimal) and ./relative/path, sorted largest first (find, sort, numfmt)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_dir": {
                        "type": "string",
                        "description": "Directory tree to scan (files only).",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": (
                            f"find -maxdepth (1–{_find_depth_ceiling}). Default 5."
                        ),
                    },
                    "max_results": {
                        "type": "integer",
                        "description": (
                            f"Max files to list (1–{_find_results_ceiling}). Default 10."
                        ),
                    },
                },
                "required": ["search_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_history_summary",
            "description": (
                "List activity shown in the terminal panel: manual commands and agent tool runs. "
                "Each line gives id, kind (user or tool), timestamp, and description — not the output. "
                "Use ids with terminal_history_fetch to load full output when needed."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_history_fetch",
            "description": (
                "Return full details for one terminal history entry by id "
                "(from terminal_history_summary), including the full output text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "Numeric entry id from terminal_history_summary.",
                    }
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_latest",
            "description": (
                "Return the most recent terminal history entry (manual command or agent tool) "
                "with full description and output."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_screen_snapshot",
            "description": (
                "Capture the live side terminal as plain text: the current visible screen "
                "Use when the user (or a prior command) may be in top, htop, "
                "vim, less, or any full-screen TUI — mode will be 'interactive' when the "
                "alternate screen is active. This is not terminal history; it is what is on "
                "screen right now."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "stop_execution",
            "description": (
                "Use when a prior run_safe command is hung, "
                "streaming indefinitely, or exit an interactive program."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

OBSERVE_TOOL_SCHEMAS_V1: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "run_safe",
            "description": (
                "Run a read-only shell command, optionally as a pipeline (a | b | c). \
                No redirects or sudo. \n \
                This tool is the last resort. Use combination of the other tools instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {
                        "type": "string",
                        "description": "The command to run, e.g. 'free -h'.",
                    }
                },
                "required": ["cmd"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a system file (config, log, proc entry). Size-capped at 16KB.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute file path."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List the contents of a directory with file sizes.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute directory path."}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "disk_usage",
            "description": (
                "Show free/used space for the filesystem that contains this path, "
                "Directory paths in the du section are relative to the given path (e.g. . and ./name)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Path whose mount to report, e.g. '/', '/home', '/var'.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "process_list",
            "description": "List running programs sorted by CPU usage (top 20).",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "journal_logs",
            "description": (
                "View and search systemd journal (journalctl) logs. "
                "Mode 'search' accepts only a single-token --grep query (no spaces); "
                "multi-word grep is disallowed because it is too slow and may time out."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": (
                            "recent_errors: recent errors (deduped), frequent_errors: frequent errors (counts by message, PID-stripped), boot_errors: errors since boot, time_window: all priorities in a since window, search: single-term grep on journal index, search_service: one service, relative time"
                        ),
                        "enum": [
                            "recent_errors",
                            "frequent_errors",
                            "boot_errors",
                            "time_window",
                            "search",
                            "search_service",
                        ],
                    },
                    "time_window": {
                        "type": "string",
                        "description": (
                            "journalctl --since string. Required for time_window; optional for "
                            "frequent_errors (default '24h ago'). Examples: '10 min ago', '24h ago'."
                        ),
                    },
                    "query": {
                        "type": "string",
                        "description": (
                            "journalctl --grep for mode 'search' only: exactly one word/token, "
                            "no whitespace (multi-word patterns are disallowed — too slow, may time out)."
                        ),
                    },
                    "unit": {
                        "type": "string",
                        "description": "Service for journalctl -u; required for search_service.",
                    },
                },
                "required": ["mode"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "service_status",
            "description": "Get the current status of a background service.",
            "parameters": {
                "type": "object",
                "properties": {
                    "unit": {"type": "string", "description": "Service name, e.g. 'nginx.service'."}
                },
                "required": ["unit"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "network_info",
            "description": "Show open network connections and network interface addresses.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_distro_info",
            "description": "Read /etc/os-release to identify the Linux distribution and version.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Search the public web for documentation, error messages, "
                "version facts, or general Linux help. Use short factual queries — never "
                "paste passwords, API keys, private paths, or personal details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "What to look up, e.g. 'ubuntu 24.04 install nginx'.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fuzzy_find",
            "description": (
                "Find files or folders by partial name match under a directory. "
                "Output paths are relative to the search root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mode": {
                        "type": "string",
                        "description": "'normal' for partial name matches (glob *target*) or 'broad' for partial matches anywhere in the path",
                        "enum": ["normal", "broad"],
                    },
                    "search_dir": {
                        "type": "string",
                        "description": "Directory to search.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Substring or pattern hint to match in paths (case-insensitive).",
                    },
                },
                "required": ["mode", "search_dir", "target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_recent",
            "description": (
                "List recently modified files and directories under a path."
                "very recent changes. Each line: compact timestamp YYYYMMDDHHMM then tab then "
                "./relative/path, sorted newest first."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_dir": {
                        "type": "string",
                        "description": "Directory tree to scan.",
                    },
                },
                "required": ["search_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_largest",
            "description": (
                "List the largest regular files under a directory. Output is size in megabytes "
                "(one decimal) and ./relative/path, sorted largest first (find, sort, numfmt)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "search_dir": {
                        "type": "string",
                        "description": "Directory tree to scan (files only).",
                    },
                },
                "required": ["search_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_history_summary",
            "description": (
                "List activity shown in the terminal panel: manual commands and agent tool runs. "
                "Each line gives id, kind (user or tool), timestamp, and description — not the output. "
                "Use ids with terminal_history_fetch to load full output when needed."
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_history_fetch",
            "description": (
                "Return full details for one terminal history entry by id "
                "(from terminal_history_summary), including the full output text."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "id": {
                        "type": "integer",
                        "description": "Numeric entry id from terminal_history_summary.",
                    }
                },
                "required": ["id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "terminal_screen_snapshot",
            "description": (
                "Capture the live side terminal as plain text: the current visible screen "
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


def observe_tool_schemas(advanced_toolset: bool) -> list[dict]:
    """OpenAI-style tool list: full (V0) when ``advanced_toolset``, else compact (V1)."""
    return OBSERVE_TOOL_SCHEMAS_V0 if advanced_toolset else OBSERVE_TOOL_SCHEMAS_V1


def tool_descriptions_for_compression() -> dict[str, str]:
    """Map tool name to schema ``description`` for compression / transcript formatting.

    Merges compact (V1) then full (V0) schemas so overlapping names keep the richer V0 text.
    """
    merged: dict[str, str] = {}
    for advanced_toolset in (False, True):
        for entry in observe_tool_schemas(advanced_toolset):
            fn = entry.get("function") or {}
            name = fn.get("name")
            if not name:
                continue
            desc = (fn.get("description") or "").strip()
            if desc:
                merged[name] = desc
    return merged


OBSERVE_TOOL_NAMES: frozenset[str] = frozenset(
    entry["function"]["name"] for entry in OBSERVE_TOOL_SCHEMAS_V0
) | frozenset(entry["function"]["name"] for entry in OBSERVE_TOOL_SCHEMAS_V1)


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

_TERMINAL_HISTORY_ERROR = "[error] Terminal history is not available."


async def dispatch(
    name: str,
    arguments: dict,
    *,
    terminal_log: TerminalHistoryStore | None = None,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
    pty_runner: Callable[[str, int], Awaitable[tuple[str, int]]] | None = None,
    terminal_snapshot: Callable[[], dict[str, str]] | None = None,
    pty_stop: Callable[[], str] | None = None,
) -> str:
    """Execute a tool by name and return the string result."""
    extras = parse_extra_allowed_command_bases(
        load_config().safety.extra_allowed_command_bases
    )
    token_pty = dispatch_pty_runner.set(pty_runner)
    token_extras = dispatch_extra_allowed_command_bases.set(
        extras if extras else None
    )
    try:
        return await _dispatch_inner(
            name,
            arguments,
            terminal_log=terminal_log,
            stream_line=stream_line,
            pty_runner=pty_runner,
            terminal_snapshot=terminal_snapshot,
            pty_stop=pty_stop,
        )
    finally:
        dispatch_extra_allowed_command_bases.reset(token_extras)
        dispatch_pty_runner.reset(token_pty)


async def _dispatch_inner(
    name: str,
    arguments: dict,
    *,
    terminal_log: TerminalHistoryStore | None,
    stream_line: Callable[[str], Awaitable[None]] | None,
    pty_runner: Callable[[str, int], Awaitable[tuple[str, int]]] | None,
    terminal_snapshot: Callable[[], dict[str, str]] | None,
    pty_stop: Callable[[], str] | None,
) -> str:
    match name:
        case "run_safe":
            return await _run_safe_handler(
                _tool_shell_cmd(arguments),
                stream_line=stream_line,
                pty_runner=pty_runner,
            )
        case "read_file":
            return await _read_file_handler(arguments.get("path", ""))
        case "list_dir":
            return await _list_dir_handler(arguments.get("path", ""))
        case "disk_usage":
            return await _disk_usage_handler(arguments.get("path", "/"))
        case "process_list":
            return await _process_list_handler()
        case "journal_logs":
            return await _journal_logs_handler(
                str(arguments.get("mode", "")),
                arguments.get("length"),
                arguments.get("time_window"),
                arguments.get("query"),
                arguments.get("unit"),
            )
        case "service_status":
            return await _service_status_handler(arguments.get("unit", ""))
        case "network_info":
            return await _network_info_handler()
        case "get_distro_info":
            return await _get_distro_info_handler()
        case "web_search":
            return await _web_search_handler(arguments.get("query", ""))
        case "exact_find":
            return await _exact_find_handler(
                arguments.get("search_dir", ""),
                arguments.get("target", ""),
                arguments.get("max_results", 10),
                arguments.get("max_depth", 5),
            )
        case "fuzzy_find":
            return await _fuzzy_find_handler(
                arguments.get("mode", "normal"),
                arguments.get("search_dir", ""),
                arguments.get("target", ""),
                arguments.get("max_results", 10),
                arguments.get("max_depth", 5),
            )
        case "find_recent":
            return await _find_recent_handler(
                arguments.get("search_dir", ""),
                arguments.get("max_results", 10),
                arguments.get("max_depth", 4),
            )
        case "find_largest":
            return await _find_largest_handler(
                arguments.get("search_dir", ""),
                arguments.get("max_results", 10),
                arguments.get("max_depth", 5),
            )
        case "terminal_history_summary":
            if terminal_log is None:
                return _TERMINAL_HISTORY_ERROR
            return terminal_log.summary_text()
        case "terminal_history_fetch":
            if terminal_log is None:
                return _TERMINAL_HISTORY_ERROR
            raw_id = arguments.get("id")
            try:
                entry_id = int(raw_id)
            except (TypeError, ValueError):
                return "[error] Invalid or missing id (integer required)."
            return terminal_log.fetch_full(entry_id)
        case "terminal_latest":
            if terminal_log is None:
                return _TERMINAL_HISTORY_ERROR
            return terminal_log.latest_full()
        case "terminal_screen_snapshot":
            return await _terminal_screen_snapshot_handler(terminal_snapshot)
        case "stop_execution":
            return await _stop_execution_handler(pty_stop)
        case _:
            return f"[unknown tool] {name!r}"
