"""Safe command executor.

Commands are parsed as bash-like lists: ``;`` separates statements, ``&&`` / ``||``
combine pipelines (left-associative), ``|`` connects stages inside a pipeline.
Each stage’s base executable must be in the effective allowlist: ``ALLOWED_COMMANDS``
plus optional user extras from config (see read_rules.md).

Redirects except **only** ``>/dev/null``, ``>>/dev/null``, ``2>/dev/null``, ``&>/dev/null``
(and optional file descriptors) are stripped before validation. Other redirects,
command substitution (``$()``), backticks, ``sudo``, and dangerous argv tokens
are blocked. Output is capped at MAX_OUTPUT_BYTES.

Pipelines use one ``asyncio`` timeout for the whole chain (not per stage); slow or
hung middle stages block the entire pipeline until timeout.

**validation_mask_fragments** (keyword-only on :func:`run_safe`): trusted call sites may
pass literal substrings that are replaced *only* for :data:`_SEGMENT_BLOCKED_PATTERN`
(scan for redirects, ``$()``, ``{}``, etc.). Allowlisting, ``shlex.split``, and
:func:`_check_dangerous_tokens` still run on the **original** segment. Fragments must be
non-overlapping; list **longest first** if one could be a substring of another. Passing
user-controlled mask strings is unsafe and must never be done from the ``run_safe`` tool.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
import shlex
from collections.abc import Awaitable, Callable
from typing import Literal

from ..pty_sanitize import sanitize_pty_command_output

# Set by :func:`shellclaw.agent.tools.dispatch` for the duration of a tool call so every
# ``run_safe`` (including indirect) uses the same PTY runner as the named ``run_safe`` tool.
PtyRunner = Callable[[str, int], Awaitable[tuple[str, int]]]
dispatch_pty_runner: contextvars.ContextVar[PtyRunner | None] = contextvars.ContextVar(
    "shellclaw_dispatch_pty_runner",
    default=None,
)

# Set by :func:`shellclaw.agent.tools.dispatch` for the duration of a tool call so
# ``run_safe`` (and nested handlers) merge these basenames with :data:`ALLOWED_COMMANDS`.
dispatch_extra_allowed_command_bases: contextvars.ContextVar[frozenset[str] | None] = (
    contextvars.ContextVar(
        "shellclaw_dispatch_extra_allowed_command_bases",
        default=None,
    )
)


def effective_allowed_command_bases() -> frozenset[str]:
    """Built-in allowlist plus any extras from the active dispatch context (if any)."""
    extra = dispatch_extra_allowed_command_bases.get()
    if extra:
        return ALLOWED_COMMANDS | extra
    return ALLOWED_COMMANDS

# Canonical permission categories: read_rules.md in the repo root.
ALLOWED_COMMANDS: frozenset[str] = frozenset({
    # File content & structure
    "cat", "head", "tail", "tac", "less", "more",
    "xxd", "od", "hexdump", "strings",
    "stat", "file", "wc",
    "md5sum", "sha256sum", "sha1sum", "sha512sum", "cksum",
    "base32", "base64", "basenc",
    "ls", "tree", "dir",
    "find", "locate", "which", "whereis", "realpath", "readlink", "namei",
    "df", "du", "lsblk", "findmnt", "blkid", "e2label",
    "lsattr", "getfattr",
    "tar", "zip", "unzip", "7z",
    # Text search & processing
    "grep", "egrep", "fgrep", "rg", "ag", "ack",
    "diff", "colordiff", "cmp", "comm", "vimdiff",
    "awk", "sed", "cut", "sort", "uniq", "tr", "paste", "join", "column", "fold", "fmt",
    "numfmt",
    "pr", "nl", "expand", "unexpand",
    "iconv", "rev",
    # System identity & hardware
    "uname", "hostname", "domainname", "hostnamectl", "lsb_release",
    "lscpu", "lsmem", "lshw", "dmidecode", "lspci", "lsusb", "lsscsi",
    "biosdecode", "vpddecode", "cpuid",
    "fdisk", "parted", "hdparm",
    "sensors", "acpi", "upower",
    "dmesg", "journalctl",
    "usb-devices",
    "i2cdetect", "i2cdump",
    "lstopo", "hwloc-ls",
    "inxi",
    "hwinfo",
    "cpupower",
    "nvidia-smi", "rocm-smi",
    "modetest", "edid-decode",
    # Display & input (query-oriented; see _check_dangerous_tokens for xrandr)
    "xrandr", "xdpyinfo", "xprop", "xwininfo",
    "xlsclients", "xlsfonts", "xlsatoms",
    "glxinfo", "eglinfo", "vulkaninfo", "vdpauinfo",
    "wlr-randr", "wayland-info",
    # Processes & runtime
    "ps", "pstree", "top", "htop", "pidof", "pgrep",
    "vmstat", "iostat", "mpstat", "sar", "dstat", "glances",
    "lsof", "strace", "ltrace",
    "lsfd", "pwdx", "prtstat",
    "uptime", "w", "free",
    # Users & permissions
    "whoami", "id", "groups", "logname",
    "who", "users", "finger",
    "last", "lastlog", "lastb",
    "getfacl", "getcap", "capsh",
    "lslogins",
    # Environment & shell
    "env", "printenv", "echo", "printf",
    "locale", "localectl",
    "infocmp",
    "crontab", "at", "getent",
    # Services & kernel
    "systemctl", "service", "sysctl", "lsmod", "modinfo",
    "loginctl", "systemd-analyze", "systemd-detect-virt",
    "udevadm",
    # IPC, locks, namespaces
    "lsipc", "lsns", "lslocks",
    # Networking (mostly query; ``ping`` sends ICMP for reachability checks)
    "ip", "ifconfig", "ss", "netstat", "route", "arp",
    "dig", "nslookup", "host", "resolvectl",
    "iptables", "nft", "ufw", "ping",
    # Packages
    "dpkg", "apt", "apt-get", "apt-cache", "rpm", "dnf", "yum", "pacman", "zypper",
    "snap", "flatpak",
    "pip", "pip3", "npm", "gem", "cargo", "go",
    # Logs & audit
    "ausearch", "aureport",
    # Binaries & libraries (inspection)
    "ldd", "readelf", "objdump", "nm", "c++filt",
    # SELinux / AppArmor (query)
    "sestatus", "getenforce",
    "aa-status",
    # Extras still useful for diagnostics
    "command", "needrestart", "timedatectl",
    "true", "false",
    # Programming
    "conda", "mamba", "python", "python3", "py", "py3"
})

# xrandr flags that change outputs, modes, or monitor layout (query-only use otherwise).
_XRANDR_MUTATING_FLAGS: frozenset[str] = frozenset({
    "--output", "--mode", "--pos", "--rotate", "--reflect", "--primary",
    "--off", "--auto", "--gamma", "--brightness", "--newmode", "--rmmode",
    "--addmode", "--delmode", "--setmonitor", "--delmonitor", "--panning",
    "--transform", "--scale", "--scale-from", "--filter", "--fb", "--screen",
    "--dpi", "--set", "--orientation", "--size", "--rate", "--refresh",
    "--prop", "--noprimary", "--same-as", "--right-of", "--left-of",
    "--above", "--below",
})

# Whole argv tokens that imply mutation, destruction, or package writes.
DANGEROUS_TOKENS: frozenset[str] = frozenset({
    "rm", "rmdir", "unlink", "shred", "dd", "mkfs", "mkswap", "mount", "umount",
    "swapon", "swapoff", "chmod", "chown", "chgrp", "mv", "cp", "tee",
    "install", "purge", "remove", "autoremove", "uninstall", "upgrade",
    "dist-upgrade", "reinstall", "hold", "unhold", "full-upgrade",
    "build-dep", "source", "changelog",
})

# These mutate package state; "update" is not global (e.g. journalctl -u update).
_PACKAGE_MANAGER_BASES: frozenset[str] = frozenset({
    "apt", "apt-get", "dnf", "yum", "zypper", "pacman",
})
_PACKAGE_MUTATING_TOKENS: frozenset[str] = frozenset({
    "update", "upgrade", "full-upgrade", "dist-upgrade",
})

_NPM_READ_SUBCOMMANDS: frozenset[str] = frozenset({
    "list",
    "ls",
    "outdated",
    "show",
    "whoami",
    "ping",
    "docs",
    "repo",
})
_PIP_READ_SUBCOMMANDS: frozenset[str] = frozenset({
    "list", "show", "freeze", "check", "help", "index", "version",
})

# Any token starting with these prefixes is blocked (find deletes, etc.).
# ``-exec`` / ``-ok`` are enforced only for ``find`` so ``-executable`` is not a false positive.
DANGEROUS_TOKEN_PREFIXES: tuple[str, ...] = (
    "-delete",
    "--delete",
    "+delete",
)

_DEV_NULL = "/dev/null"


def _scan_dev_null_redirect(s: str, start: int) -> tuple[int | None, bool]:
    """Match ``[&fd]>? … /dev/null`` at *start*.

    Returns ``(end_index, suppress_stderr)`` where *suppress_stderr* is True when
    the redirect discards stderr (``2>…`` or ``&>…``), so the executor can use
    ``DEVNULL`` instead of merging stderr into the pipeline.
    """
    n = len(s)
    k = start
    suppress_stderr = False
    if k < n and s[k] == "&":
        k += 1
        if k >= n or s[k] != ">":
            return None, False
        k += 1
        if k < n and s[k] == ">":
            k += 1
        suppress_stderr = True
    elif k < n and s[k].isdigit():
        fd_start = k
        while k < n and s[k].isdigit():
            k += 1
        fd = s[fd_start:k]
        if k >= n or s[k] != ">":
            return None, False
        k += 1
        if k < n and s[k] == ">":
            k += 1
        suppress_stderr = fd == "2"
    elif k < n and s[k] == ">":
        k += 1
        if k < n and s[k] == ">":
            k += 1
        suppress_stderr = False
    else:
        return None, False
    while k < n and s[k].isspace():
        k += 1
    if k + len(_DEV_NULL) > n or s[k : k + len(_DEV_NULL)] != _DEV_NULL:
        return None, False
    k += len(_DEV_NULL)
    if k < n and not s[k].isspace():
        return None, False
    return k, suppress_stderr


def _strip_allowed_null_redirects(segment: str) -> tuple[str, bool]:
    """Remove redirects to ``/dev/null`` only (quote-aware); other ``>``/``<`` stay.

    Returns the cleaned segment and whether any stripped redirect discarded stderr
    (so execution can match shell ``2>/dev/null`` / ``&>/dev/null`` behaviour).
    """
    out: list[str] = []
    i = 0
    n = len(segment)
    in_single = False
    in_double = False
    stderr_discard = False

    while i < n:
        c = segment[i]
        if in_single:
            out.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                out.append(c)
                out.append(segment[i + 1])
                i += 2
                continue
            out.append(c)
            if c == '"':
                in_double = False
            i += 1
            continue
        if c == "'":
            in_single = True
            out.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            out.append(c)
            i += 1
            continue

        if c.isspace():
            j = i
            while j < n and segment[j].isspace():
                j += 1
            scanned = _scan_dev_null_redirect(segment, j)
            end, sup = scanned
            if end is not None:
                stderr_discard |= sup
                i = end
                continue
            out.append(c)
            i += 1
            continue

        if i == 0 or segment[i - 1].isspace():
            end, sup = _scan_dev_null_redirect(segment, i)
            if end is not None:
                stderr_discard |= sup
                i = end
                continue

        out.append(c)
        i += 1

    return "".join(out), stderr_discard


def _normalize_pipeline_stage(segment: str) -> tuple[str, bool]:
    """Strip allowed ``… >/dev/null`` fragments, then trim.

    Second value is True if stderr was redirected to ``/dev/null`` (``2>`` or ``&>``).
    """
    cleaned, stderr_discard = _strip_allowed_null_redirects(segment)
    return cleaned.strip(), stderr_discard


_SEGMENT_BLOCKED_PATTERN = re.compile(
    r"""
    [><]     |   # redirect
    \$\(     |   # command substitution
    `        |   # backtick
    \{|\}    |   # brace expansion
    \bsudo\b |   # escalation
    (?<![\d<>])\s&\s*$ |   # trailing background &
    ^\s*&\s*$                # lone &
    """,
    re.VERBOSE,
)


def _segment_for_blocked_scan(segment: str, fragments: tuple[str, ...]) -> str:
    """Return *segment* with each *fragment* replaced by a safe placeholder for blocked-char scan.

    Fragments are applied in order; each occurrence of a fragment is replaced once per
    inner loop pass (repeat until that fragment no longer appears). Empty fragments are
    skipped. Use non-overlapping fragments; longest match first if one nests in another.
    """
    if not fragments:
        return segment
    s = segment
    for i, frag in enumerate(fragments):
        if not frag:
            continue
        placeholder = f" __SANDBOX_MASK_{i}__ "
        while frag in s:
            s = s.replace(frag, placeholder, 1)
    return s

MAX_PIPELINE_STAGES = 6
MAX_SEMICOLON_STATEMENTS = 12
MAX_AND_OR_CHUNKS = 24
MAX_TOTAL_PIPELINE_STAGES = 48
MAX_OUTPUT_BYTES = 8192
TRUNCATION_NOTICE = "\n... [output truncated] ..."

Op = Literal[";", "&&", "||", "|"]
Token = tuple[Literal["txt"], str] | tuple[Literal["op"], Op]

# One pipeline stage: argv string (redirects to /dev/null already stripped) and
# whether stderr was discarded (``2>/dev/null`` / ``&>/dev/null``).
PipelineStage = tuple[str, bool]

# Parsed: program = list of statements; each statement is
# [(None, pipeline_stages), ('&&'|'||', pipeline_stages), ...]
RawStatement = list[tuple[str | None, list[str]]]
Statement = list[tuple[str | None, list[PipelineStage]]]
ParsedProgram = list[Statement]


class SandboxError(Exception):
    """Raised when a command is rejected by the sandbox."""


def _tokenize(cmd: str) -> list[Token]:
    """Quote-aware split into text chunks and operators ``;`` ``&&`` ``||`` ``|``."""
    out: list[Token] = []
    buf: list[str] = []
    i = 0
    n = len(cmd)
    in_single = False
    in_double = False

    def flush() -> None:
        if buf:
            out.append(("txt", "".join(buf)))
            buf.clear()

    while i < n:
        c = cmd[i]
        if in_single:
            buf.append(c)
            if c == "'":
                in_single = False
            i += 1
            continue
        if in_double:
            if c == "\\" and i + 1 < n:
                buf.append(c)
                buf.append(cmd[i + 1])
                i += 2
                continue
            buf.append(c)
            if c == '"':
                in_double = False
            i += 1
            continue

        if c == "'":
            in_single = True
            buf.append(c)
            i += 1
            continue
        if c == '"':
            in_double = True
            buf.append(c)
            i += 1
            continue

        if c == ";":
            flush()
            out.append(("op", ";"))
            i += 1
            continue
        if i + 1 < n and c == "&" and cmd[i + 1] == "&":
            flush()
            out.append(("op", "&&"))
            i += 2
            continue
        if i + 1 < n and c == "|" and cmd[i + 1] == "|":
            flush()
            out.append(("op", "||"))
            i += 2
            continue
        if c == "|":
            flush()
            out.append(("op", "|"))
            i += 1
            continue

        buf.append(c)
        i += 1

    flush()
    return out


def _parse_pipeline_tokens(tokens: list[Token], start: int) -> tuple[list[str], int]:
    """Parse ``text (| text)*`` starting at *start*; return stage strings and new index."""
    if start >= len(tokens) or tokens[start][0] != "txt":
        raise SandboxError("Expected a command (missing or invalid segment).")
    stages: list[str] = [tokens[start][1].strip()]
    if not stages[0]:
        raise SandboxError("Empty pipeline segment.")
    i = start + 1
    while i < len(tokens) and tokens[i] == ("op", "|"):
        i += 1
        if i >= len(tokens) or tokens[i][0] != "txt":
            raise SandboxError("Empty pipeline segment.")
        seg = tokens[i][1].strip()
        if not seg:
            raise SandboxError("Empty pipeline segment.")
        stages.append(seg)
        i += 1
    return stages, i


def _parse_statement_tokens(tokens: list[Token]) -> RawStatement:
    """Parse one semicolon-separated statement: pipelines with ``&&`` / ``||``."""
    if not tokens:
        raise SandboxError("Empty command segment between ';'.")
    statement: RawStatement = []
    i = 0
    stages, i = _parse_pipeline_tokens(tokens, i)
    statement.append((None, stages))
    chunks = 1
    while i < len(tokens):
        if tokens[i][0] != "op" or tokens[i][1] not in ("&&", "||"):
            raise SandboxError(
                f"Unexpected token in command: expected '&&' or '||'."
            )
        if chunks >= MAX_AND_OR_CHUNKS:
            raise SandboxError("Too many '&&' / '||' segments.")
        op = tokens[i][1]
        i += 1
        stages, i = _parse_pipeline_tokens(tokens, i)
        statement.append((op, stages))
        chunks += 1
    if i != len(tokens):
        raise SandboxError("Trailing garbage after command.")
    return statement


def _tokens_to_statements(tokens: list[Token]) -> list[list[Token]]:
    """Split token stream on top-level ``;``."""
    statements: list[list[Token]] = []
    cur: list[Token] = []
    for t in tokens:
        if t == ("op", ";"):
            statements.append(cur)
            cur = []
        else:
            cur.append(t)
    statements.append(cur)
    return statements


def _split_pipeline(cmd: str) -> list[str]:
    """Split on a single ``|`` outside quotes; ``||`` / ``&&`` / ``;`` stay in the segment.

    Delegates to :func:`_tokenize` so quoting matches the real parser (used by tests).
    """
    tokens = _tokenize(cmd.strip())
    if not tokens:
        return [""]
    parts: list[str] = []
    cur: list[str] = []
    for kind, val in tokens:
        if kind == "op":
            if val == "|":
                parts.append("".join(cur).strip())
                cur = []
            else:
                cur.append(val)
        else:
            cur.append(val)
    parts.append("".join(cur).strip())
    return parts


def _next_non_flag_index(tokens: list[str], start: int) -> int | None:
    i = start
    while i < len(tokens) and tokens[i].startswith("-"):
        i += 1
    return i if i < len(tokens) else None


def _base_command(cmd: str) -> str:
    try:
        tokens = shlex.split(cmd)
    except ValueError as exc:
        raise SandboxError(f"Could not parse command: {exc}") from exc
    return tokens[0] if tokens else ""


def _check_dangerous_tokens(segment: str, base: str, tokens: list[str]) -> None:
    """Reject argv that implies writes or destructive actions. See read_rules.md."""
    for tok in tokens:
        if tok == "&":
            raise SandboxError("Background operator '&' is not permitted.")
        if tok in DANGEROUS_TOKENS:
            raise SandboxError(f"Disallowed token {tok!r} in command.")
        low = tok.lower()
        for pref in DANGEROUS_TOKEN_PREFIXES:
            if low == pref or low.startswith(pref + "=") or tok.startswith(pref):
                raise SandboxError(f"Disallowed flag or option {tok!r} in command.")

    if base == "find":
        for t in tokens[1:]:
            head = t.split("=", 1)[0].lower()
            if head.startswith("-executable"):
                continue
            if head in (
                "-exec",
                "-execdir",
                "-ok",
                "-okdir",
                "-delete",
                "+delete",
                "--exec",
                "--execdir",
            ):
                raise SandboxError(
                    f"Disallowed find action {t!r} (read-only observation only)."
                )
            if head.startswith(("-exec", "--exec")):
                raise SandboxError(
                    f"Disallowed find action {t!r} (read-only observation only)."
                )
            if head.startswith(("-ok", "--ok")):
                raise SandboxError(
                    f"Disallowed find action {t!r} (read-only observation only)."
                )

    if base == "sed":
        for t in tokens[1:]:
            if t in ("-i", "--in-place") or t.startswith("--in-place="):
                raise SandboxError("sed in-place editing (-i) is not permitted.")
            if len(t) > 2 and t.startswith("-i"):
                raise SandboxError("sed in-place editing (-i) is not permitted.")

    if base == "tar":
        for t in tokens[1:]:
            if t in ("-x", "--extract", "--get"):
                raise SandboxError("tar extract mode is not permitted (read-only list only).")
            if t.startswith("--extract=") or t.startswith("--get"):
                raise SandboxError("tar extract mode is not permitted.")

    if base == "zip":
        if "-l" not in tokens:
            raise SandboxError("zip only allows listing mode (-l) for read-only use.")
        for t in tokens[1:]:
            if t in ("-d", "-u", "-U", "-m", "-r", "-g", "-F"):
                raise SandboxError(f"Disallowed zip mode {t!r} (use zip -l only).")

    if base == "unzip":
        if "-l" not in tokens and "-Z" not in tokens:
            raise SandboxError("unzip only allows list mode (-l or -Z) for read-only use.")
        for t in tokens[1:]:
            if t in ("-x", "-e", "-p"):
                raise SandboxError(f"Disallowed unzip extract option {t!r}.")

    if base == "7z" and len(tokens) > 1:
        sub = tokens[1].lower().lstrip("-")
        if sub in ("x", "e", "a", "d", "u", "rn", "del", "delete", "b"):
            raise SandboxError("7z only allows list/test/info (e.g. '7z l', '7z t') for read-only use.")

    if base in _PACKAGE_MANAGER_BASES:
        for t in tokens[1:]:
            if t in _PACKAGE_MUTATING_TOKENS:
                raise SandboxError(
                    f"Disallowed package-manager action {t!r} (read-only observation only)."
                )

    if base == "dpkg":
        for t in tokens[1:]:
            if t in (
                "-i", "--install", "-r", "--remove", "--purge", "-P",
                "--configure", "--triggers-only",
            ):
                raise SandboxError(f"Disallowed dpkg action {t!r} (read-only only).")

    if base == "rpm":
        for t in tokens[1:]:
            if t in (
                "-i", "--install", "-e", "--erase", "--reinstall",
                "-U", "--upgrade", "-F", "--freshen",
            ):
                raise SandboxError(f"Disallowed rpm action {t!r} (read-only only).")

    if base in ("strace", "ltrace"):
        for t in tokens[1:]:
            low = t.lower()
            if low == "--inject" or low.startswith("--inject="):
                raise SandboxError(f"Disallowed {base} option {t!r} (read-only observation only).")
            if low in ("-o", "--output") or low.startswith("--output="):
                raise SandboxError(
                    f"Disallowed {base} output file {t!r} (read-only observation only)."
                )
            if low.startswith("-e") and "inject" in low:
                raise SandboxError(
                    f"Disallowed {base} option {t!r} (read-only observation only)."
                )

    if base == "crontab":
        if "-l" not in tokens:
            raise SandboxError("crontab only allows -l (list) for read-only use.")
        for t in tokens[1:]:
            if t in ("-e", "-r", "-i", "--edit", "--remove"):
                raise SandboxError(f"Disallowed crontab option {t!r} (read-only only).")

    if base == "at":
        if "-l" not in tokens:
            raise SandboxError("at only allows -l (list queued jobs) for read-only use.")

    if base == "npm":
        i = _next_non_flag_index(tokens, 1)
        if i is None:
            raise SandboxError("npm requires a read-only subcommand (e.g. list, show).")
        sub = tokens[i]
        if sub == "config":
            j = _next_non_flag_index(tokens, i + 1)
            if j is None or tokens[j] not in ("get", "list", "ls"):
                raise SandboxError(
                    "npm config only allows get or list for read-only use."
                )
        elif sub == "index":
            j = _next_non_flag_index(tokens, i + 1)
            if j is None or tokens[j] != "versions":
                raise SandboxError("npm index only allows 'versions' for read-only use.")
        elif sub not in _NPM_READ_SUBCOMMANDS:
            raise SandboxError(f"Disallowed npm subcommand {sub!r} for read-only use.")

    if base in ("pip", "pip3"):
        # Subcommand allowlist limits mutations; ``list``/``show`` may still contact an index unless
        # the user passes ``--no-index`` (not enforced here).
        i = _next_non_flag_index(tokens, 1)
        if i is None:
            raise SandboxError("pip requires a read-only subcommand (e.g. list, show).")
        sub = tokens[i]
        if sub in (
            "install", "uninstall", "download", "wheel", "cache", "lock", "hash",
        ):
            raise SandboxError(f"Disallowed pip action {sub!r} (read-only only).")
        if sub == "config":
            j = _next_non_flag_index(tokens, i + 1)
            if j is None or tokens[j] not in ("get", "list", "-l"):
                raise SandboxError("pip config only allows get or list for read-only use.")
        elif sub == "index":
            j = _next_non_flag_index(tokens, i + 1)
            if j is None or tokens[j] != "versions":
                raise SandboxError("pip index only allows 'versions' for read-only use.")
        elif sub not in _PIP_READ_SUBCOMMANDS:
            raise SandboxError(f"Disallowed pip subcommand {sub!r} for read-only use.")

    if base == "cargo":
        i = _next_non_flag_index(tokens, 1)
        if i is None or tokens[i] not in ("metadata", "tree"):
            raise SandboxError("cargo only allows 'metadata' or 'tree' for read-only use.")

    if base == "go":
        i = _next_non_flag_index(tokens, 1)
        if i is None or tokens[i] != "list":
            raise SandboxError("go only allows 'list' for read-only use.")

    if base == "gem":
        i = _next_non_flag_index(tokens, 1)
        if i is None or tokens[i] != "list":
            raise SandboxError("gem only allows 'list' for read-only use.")

    if base == "xrandr":
        for t in tokens[1:]:
            low = t.lower()
            head = low.split("=", 1)[0]
            if head in _XRANDR_MUTATING_FLAGS:
                raise SandboxError(
                    f"Disallowed xrandr option {t!r} (read-only query only; omit output/mode changes)."
                )
            if low in ("-s", "-o"):
                raise SandboxError(
                    "Disallowed xrandr -s/-o (resolution or orientation changes are not permitted)."
                )

    if base == "cpupower":
        i = _next_non_flag_index(tokens, 1)
        if i is None or tokens[i] != "frequency-info":
            raise SandboxError("cpupower only allows 'frequency-info' for read-only use.")

    if base == "udevadm":
        i = _next_non_flag_index(tokens, 1)
        if i is None or tokens[i] != "info":
            raise SandboxError("udevadm only allows 'info' for read-only use.")


def _validate_pipeline_stage(
    segment: str,
    validation_mask_fragments: tuple[str, ...] = (),
) -> None:
    """Validate a pipeline stage after :func:`_normalize_pipeline_stage` (no /dev/null redirects left)."""
    if not segment:
        raise SandboxError("Empty pipeline segment.")
    scan_segment = _segment_for_blocked_scan(segment, validation_mask_fragments)
    if _SEGMENT_BLOCKED_PATTERN.search(scan_segment):
        raise SandboxError(
            f"Command contains blocked characters or operators: {segment!r}"
        )
    base = _base_command(segment)
    if not base:
        raise SandboxError(f"Could not parse pipeline segment: {segment!r}")
    if base not in effective_allowed_command_bases():
        raise SandboxError(f"Command {base!r} is not in the allowed list.")
    try:
        tokens = shlex.split(segment)
    except ValueError as exc:
        raise SandboxError(f"Could not parse pipeline segment: {exc}") from exc
    if not tokens:
        raise SandboxError("Empty pipeline segment after parse.")
    _check_dangerous_tokens(segment, base, tokens)


def _validate_statement(
    statement: RawStatement,
    validation_mask_fragments: tuple[str, ...] = (),
) -> tuple[Statement, int]:
    """Normalize stages, validate, return executable statement and total stage count."""
    total_stages = 0
    norm_statement: Statement = []
    for op, stages in statement:
        if len(stages) > MAX_PIPELINE_STAGES:
            raise SandboxError(
                f"Pipeline has more than {MAX_PIPELINE_STAGES} stages."
            )
        norm_stages: list[PipelineStage] = []
        for s in stages:
            seg, stderr_devnull = _normalize_pipeline_stage(s)
            norm_stages.append((seg, stderr_devnull))
        total_stages += len(norm_stages)
        for seg, _stderr in norm_stages:
            if not seg:
                raise SandboxError("Empty pipeline segment.")
            _validate_pipeline_stage(seg, validation_mask_fragments)
        norm_statement.append((op, norm_stages))
    return norm_statement, total_stages


def _parse_and_validate(
    cmd: str,
    validation_mask_fragments: tuple[str, ...] = (),
) -> ParsedProgram:
    stripped = cmd.strip()
    if not stripped:
        raise SandboxError("Empty command.")

    tokens = _tokenize(stripped)
    if not tokens:
        raise SandboxError("Empty command.")

    raw_groups = _tokens_to_statements(tokens)
    groups = [g for g in raw_groups if g]  # drop empty from leading/trailing ;
    if len(groups) > MAX_SEMICOLON_STATEMENTS:
        raise SandboxError(f"More than {MAX_SEMICOLON_STATEMENTS} ';'-separated commands.")

    program: ParsedProgram = []
    total_pipe_stages = 0
    for g in groups:
        if not g:
            raise SandboxError("Empty command between ';'.")
        st = _parse_statement_tokens(g)
        st, ts = _validate_statement(st, validation_mask_fragments)
        total_pipe_stages += ts
        program.append(st)

    if total_pipe_stages > MAX_TOTAL_PIPELINE_STAGES:
        raise SandboxError("Too many total pipeline stages in command.")

    return program


def validate(
    cmd: str,
    *,
    validation_mask_fragments: tuple[str, ...] | None = None,
) -> None:
    """Raise SandboxError if cmd is not safe to run."""
    _parse_and_validate(cmd, validation_mask_fragments or ())


def format_shell_output(text: str, rc: int) -> str:
    """Format captured shell stdout (e.g. from a PTY) like :func:`run_safe` tool results."""
    return _format_process_output(text.encode("utf-8", errors="replace"), rc)


def _format_process_output(stdout: bytes, rc: int) -> str:
    output = stdout.decode("utf-8", errors="replace")
    if len(output.encode()) > MAX_OUTPUT_BYTES:
        truncated = output.encode()[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
        output = truncated + TRUNCATION_NOTICE
    stripped = output.strip()
    if not stripped:
        return f"(no output; exit code: {rc})"
    if rc != 0:
        return f"{output.rstrip()}\n(exit code: {rc})"
    return output


async def _run_one_pipeline(
    stages: list[PipelineStage],
    timeout: int,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bytes, int]:
    if len(stages) == 1:
        seg, stderr_devnull = stages[0]
        try:
            tl = shlex.split(seg)
        except ValueError as exc:
            raise SandboxError(f"Could not parse command: {exc}") from exc
        return await _run_single(tl, timeout, stderr_devnull, stream_line=stream_line)
    return await _run_pipeline(stages, timeout, stream_line=stream_line)


async def _run_single(
    tokens: list[str],
    timeout: int,
    stderr_devnull: bool = False,
    *,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bytes, int]:
    stderr = (
        asyncio.subprocess.DEVNULL if stderr_devnull else asyncio.subprocess.STDOUT
    )
    proc = await asyncio.create_subprocess_exec(
        *tokens,
        stdout=asyncio.subprocess.PIPE,
        stderr=stderr,
    )
    try:
        async with asyncio.timeout(timeout):
            if stream_line is None:
                stdout, _ = await proc.communicate()
            else:
                if proc.stdout is None:
                    raise SandboxError("Process missing stdout pipe.")
                parts: list[bytes] = []
                while True:
                    line = await proc.stdout.readline()
                    if not line:
                        break
                    parts.append(line)
                    text = line.decode("utf-8", errors="replace").rstrip("\r\n")
                    await stream_line(text)
                await proc.wait()
                stdout = b"".join(parts)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    rc = proc.returncode if proc.returncode is not None else -1
    return stdout, rc


async def _forward_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            writer.write(chunk)
            await writer.drain()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


async def _drain_reader(
    reader: asyncio.StreamReader,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
) -> bytes:
    parts: list[bytes] = []
    pending = b""
    while True:
        chunk = await reader.read(65536)
        if not chunk:
            if stream_line is not None and pending:
                await stream_line(pending.decode("utf-8", errors="replace"))
            elif pending:
                parts.append(pending)
            break
        parts.append(chunk)
        if stream_line is None:
            continue
        pending += chunk
        while b"\n" in pending:
            line, _, pending = pending.partition(b"\n")
            await stream_line(line.decode("utf-8", errors="replace"))
    return b"".join(parts)


async def _run_pipeline(
    stages: list[PipelineStage],
    timeout: int,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bytes, int]:
    token_lists: list[list[str]] = []
    stderr_devnull_flags: list[bool] = []
    for seg, stderr_devnull in stages:
        try:
            tl = shlex.split(seg)
        except ValueError as exc:
            raise SandboxError(f"Could not parse command: {exc}") from exc
        if not tl:
            raise SandboxError("Empty pipeline segment after parse.")
        token_lists.append(tl)
        stderr_devnull_flags.append(stderr_devnull)

    procs: list[asyncio.subprocess.Process] = []
    forward_tasks: list[asyncio.Task[None]] = []
    read_task: asyncio.Task[bytes] | None = None
    try:
        n = len(token_lists)
        for i, tokens in enumerate(token_lists):
            stdin = None if i == 0 else asyncio.subprocess.PIPE
            stderr = (
                asyncio.subprocess.DEVNULL
                if stderr_devnull_flags[i]
                else asyncio.subprocess.STDOUT
            )
            p = await asyncio.create_subprocess_exec(
                *tokens,
                stdin=stdin,
                stdout=asyncio.subprocess.PIPE,
                stderr=stderr,
            )
            procs.append(p)

        for i in range(n - 1):
            out_r = procs[i].stdout
            in_w = procs[i + 1].stdin
            if out_r is None or in_w is None:
                raise SandboxError("Pipeline process missing stdio pipes.")
            forward_tasks.append(
                asyncio.create_task(_forward_stream(out_r, in_w))
            )

        last = procs[-1]
        if last.stdout is None:
            raise SandboxError("Last pipeline stage missing stdout pipe.")

        read_task = asyncio.create_task(_drain_reader(last.stdout, stream_line))

        async def _run_chain() -> tuple[bytes, int]:
            await asyncio.gather(*forward_tasks, read_task)
            await last.wait()
            body = read_task.result()
            rc = last.returncode if last.returncode is not None else -1
            return body, rc

        return await asyncio.wait_for(_run_chain(), timeout=timeout)
    except asyncio.TimeoutError:
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
        for t in forward_tasks:
            t.cancel()
        for p in procs:
            try:
                p.kill()
            except ProcessLookupError:
                pass
            try:
                await p.communicate()
            except ProcessLookupError:
                pass
        raise
    finally:
        for t in forward_tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass


async def _run_statement(
    statement: Statement,
    timeout: int,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
) -> tuple[bytes, int]:
    """Evaluate ``&&`` / ``||`` chain; return concatenated stdout and final exit code."""
    rc = 0
    parts: list[bytes] = []
    for idx, (lead, stages) in enumerate(statement):
        if idx > 0:
            assert lead is not None
            if lead == "&&" and rc != 0:
                continue
            if lead == "||" and rc == 0:
                continue
        out, rc = await _run_one_pipeline(stages, timeout, stream_line=stream_line)
        parts.append(out)
    return b"".join(parts), rc


async def run_safe(
    cmd: str,
    timeout: int = 10,
    *,
    validation_mask_fragments: tuple[str, ...] | None = None,
    stream_line: Callable[[str], Awaitable[None]] | None = None,
    pty_runner: Callable[[str, int], Awaitable[tuple[str, int]]] | None = None,
) -> str:
    """Validate and execute a read-only command, returning its output.

    Blocked or disallowed commands return a ``[blocked]`` string rather
    than raising, so callers don't need try/except around every call.
    Returns combined stdout+stderr, truncated to MAX_OUTPUT_BYTES.

    If the process prints nothing, the return value is still non-empty:
    ``(no output; exit code: N)``. Non-zero exits append ``(exit code: N)``
    when there was stdout/stderr text so callers (and the LLM) can tell
    success from failure.

    ``validation_mask_fragments``: optional trusted substrings (e.g. a quoted awk
    program) omitted only from the brace/redirect/subshell scan. Do not pass
    LLM-controlled values.

    ``stream_line``: when set, each decoded stdout line (or tail without ``\\n`` at
    EOF) is awaited as it is read, for live UIs. Full output is still collected and
    returned as usual (including truncation via ``MAX_OUTPUT_BYTES``).

    ``pty_runner``: when set, the validated command is executed via this coroutine
    ``(cmd, timeout) -> (stdout_text, exit_code)`` instead of subprocess pipelines
    (used by the Textual PTY integration). ``stream_line`` is ignored in that case.
    When ``pty_runner`` is omitted, :data:`dispatch_pty_runner` is used if the agent
    set it for the current :func:`shellclaw.agent.tools.dispatch` call (so nested
    ``run_safe`` calls share the same PTY).
    """
    try:
        program = _parse_and_validate(cmd, validation_mask_fragments or ())
    except SandboxError as exc:
        return f"[blocked] {exc}"

    runner: PtyRunner | None = (
        pty_runner if pty_runner is not None else dispatch_pty_runner.get()
    )
    if runner is not None:
        try:
            out, rc = await runner(cmd, timeout)
        except asyncio.TimeoutError:
            return f"[command timed out after {timeout}s]"
        out = sanitize_pty_command_output(cmd, out)
        return _format_process_output(out.encode("utf-8", errors="replace"), rc)

    combined_text_parts: list[str] = []
    last_rc = 0
    try:
        for stmt_idx, statement in enumerate(program):
            try:
                stdout, last_rc = await _run_statement(
                    statement, timeout, stream_line=stream_line
                )
            except SandboxError as exc:
                return f"[blocked] {exc}"
            except asyncio.TimeoutError:
                return f"[command timed out after {timeout}s]"
            piece = _format_process_output(stdout, last_rc)
            combined_text_parts.append(piece)
    except asyncio.TimeoutError:
        return f"[command timed out after {timeout}s]"

    if len(combined_text_parts) == 1:
        return combined_text_parts[0]

    merged = "\n".join(combined_text_parts)
    if len(merged.encode()) > MAX_OUTPUT_BYTES:
        merged = (
            merged.encode()[:MAX_OUTPUT_BYTES].decode("utf-8", errors="replace")
            + TRUNCATION_NOTICE
        )
    return merged
