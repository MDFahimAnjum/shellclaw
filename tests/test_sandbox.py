"""Tests for safety/sandbox.py — command allowlist and safety validation."""

import shlex

import pytest

from shellclaw.config import parse_extra_allowed_command_bases
from shellclaw.safety.sandbox import (
    ALLOWED_COMMANDS,
    MAX_OUTPUT_BYTES,
    TRUNCATION_NOTICE,
    SandboxError,
    _parse_and_validate,
    _split_pipeline,
    run_safe,
    validate,
)
from shellclaw.agent.tools import JOURNAL_RELTIME_AWK, _journal_dedupe_stage


class TestValidate:
    def test_allowed_command_passes(self):
        validate("df -h")

    def test_allowed_command_with_args_passes(self):
        validate("ps aux")

    def test_grep_allowed(self):
        validate("grep -n root /etc/passwd")

    def test_command_and_which_allowed(self):
        validate("command -v sh")
        validate("which python3")

    def test_unknown_command_raises(self):
        with pytest.raises(SandboxError, match="not in the allowed list"):
            validate("wget https://example.com")

    def test_pipe_allowed_both_stages_allowlisted(self):
        validate("ls /usr/bin | grep brave")
        validate("dpkg -l | grep brave")

    def test_pipe_rejected_if_stage_not_allowlisted(self):
        with pytest.raises(SandboxError, match="not in the allowed list"):
            validate("ls | wget http://x")

    def test_or_operator_allowed_when_stages_allowlisted(self):
        validate("ls || grep x")
        validate("false || true")

    def test_redirect_out_blocked(self):
        with pytest.raises(SandboxError, match="blocked"):
            validate("df -h > /tmp/out.txt")

    def test_dev_null_redirects_allowed(self):
        validate("df -h >/dev/null")
        validate("df -h 2>/dev/null")
        validate("ls 2> /dev/null")
        validate("ls &>/dev/null")
        validate("find /tmp -name x -type d 2>/dev/null | head -5")

    def test_find_exec_blocked_even_if_split_tokens(self):
        with pytest.raises(SandboxError, match="find"):
            validate(r"find /tmp -exec rm {} \;")

    def test_find_executable_not_treated_as_exec(self):
        validate("find /usr/bin -type f -executable")

    def test_crontab_edit_blocked(self):
        with pytest.raises(SandboxError, match="crontab"):
            validate("crontab -e")

    def test_crontab_list_allowed(self):
        validate("crontab -l")

    def test_at_list_only(self):
        validate("at -l")
        with pytest.raises(SandboxError, match="at only"):
            validate("at now")

    def test_strace_output_file_blocked(self):
        with pytest.raises(SandboxError, match="strace"):
            validate("strace -o /tmp/out ls")

    def test_pip_install_blocked(self):
        with pytest.raises(SandboxError, match="install"):
            validate("pip install requests")

    def test_pip_list_allowed(self):
        validate("pip list")

    def test_npm_run_blocked(self):
        with pytest.raises(SandboxError, match="npm"):
            validate("npm run build")

    def test_redirect_in_blocked(self):
        with pytest.raises(SandboxError, match="blocked"):
            validate("cat < /etc/hosts")

    def test_subshell_dollar_blocked(self):
        with pytest.raises(SandboxError, match="blocked"):
            validate("echo $(whoami)")

    def test_backtick_subshell_blocked(self):
        with pytest.raises(SandboxError, match="blocked"):
            validate("echo `hostname`")

    def test_semicolon_allowed_when_stages_allowlisted(self):
        validate("df; ls")

    def test_and_chain_allowed_when_stages_allowlisted(self):
        validate("df -h && du -sh /")

    def test_pipe_binds_tighter_than_and(self):
        """``a | b && c`` is ``(a|b) && c``."""
        validate("false | true && uname")

    def test_sudo_blocked(self):
        with pytest.raises(SandboxError, match="blocked"):
            validate("sudo apt update")

    def test_empty_command_raises(self):
        with pytest.raises(SandboxError):
            validate("")

    def test_empty_segment_raises(self):
        with pytest.raises(SandboxError, match="Empty pipeline segment"):
            validate("ls | ")

    def test_pipeline_too_many_stages(self):
        cmd = "|".join(["ls"] * 7)
        with pytest.raises(SandboxError, match="more than"):
            validate(cmd)

    def test_allowed_commands_set_contains_essentials(self):
        for cmd in ["df", "free", "ps", "journalctl", "systemctl", "apt", "lscpu"]:
            assert cmd in ALLOWED_COMMANDS

    def test_xrandr_query_allowed(self):
        validate("xrandr")
        validate("xrandr --listmonitors --query")

    def test_xrandr_output_change_blocked(self):
        with pytest.raises(SandboxError, match="xrandr"):
            validate("xrandr --output HDMI-1 --mode 1920x1080")

    def test_cpupower_frequency_info_allowed(self):
        validate("cpupower frequency-info")

    def test_cpupower_set_blocked(self):
        with pytest.raises(SandboxError, match="cpupower"):
            validate("cpupower frequency-set -f 2000")

    def test_udevadm_info_allowed(self):
        validate("udevadm info -q all -n sda")

    def test_udevadm_control_blocked(self):
        with pytest.raises(SandboxError, match="udevadm"):
            validate("udevadm control --reload")


class TestJournalDedupeStage:
    def test_no_literal_tab_in_shell_command(self):
        """Regression: literal TAB in -F was stripped from OSC cmd= text → awk -F ''."""
        s = _journal_dedupe_stage()
        assert chr(9) not in s, "must not embed U+0009 in the shell command line"
        assert "'\\t'" in s  # shell: -F '\t' (awk interprets as tab)
        assert "!seen[$2]++" in s


class TestValidationMaskFragments:
    def test_journal_reltime_blocked_without_mask(self):
        rel_q = shlex.quote(JOURNAL_RELTIME_AWK)
        cmd = (
            f"journalctl -p 3 -r --no-pager -n 1 -o short-unix | awk {rel_q} | head -n 1"
        )
        with pytest.raises(SandboxError, match="blocked"):
            _parse_and_validate(cmd)

    def test_journal_reltime_passes_with_mask(self):
        rel_q = shlex.quote(JOURNAL_RELTIME_AWK)
        cmd = (
            f"journalctl -p 3 -r --no-pager -n 1 -o short-unix | awk {rel_q} | head -n 1"
        )
        _parse_and_validate(cmd, validation_mask_fragments=(rel_q,))

    def test_validate_accepts_mask_keyword(self):
        rel_q = shlex.quote(JOURNAL_RELTIME_AWK)
        cmd = f"echo x | awk {rel_q}"
        with pytest.raises(SandboxError):
            validate(cmd)
        validate(cmd, validation_mask_fragments=(rel_q,))


class TestSplitPipeline:
    def test_pipe_inside_double_quotes_not_split(self):
        assert _split_pipeline('grep "a|b" /etc/passwd') == ['grep "a|b" /etc/passwd']

    def test_simple_two_stage(self):
        assert _split_pipeline("ls | grep x") == ["ls", "grep x"]


class TestRunSafe:
    async def test_unknown_command_not_in_allowlist(self):
        # echo is allowlisted (read_rules.md); wget is not
        result = await run_safe("wget https://example.com")
        assert "[blocked]" in result

    async def test_uname_runs(self):
        result = await run_safe("uname -a")
        assert "Linux" in result or len(result) > 0

    async def test_pipeline_ls_grep(self):
        result = await run_safe("ls /usr/bin | grep -E '^ls$'")
        assert "[blocked]" not in result
        assert "ls" in result.splitlines()

    async def test_stderr_devnull_not_merged_into_pipeline(self):
        """``2>/dev/null`` must drop stderr (sandbox strips redirect but must emulate it)."""
        result = await run_safe(
            "grep '^nope_shellclaw_xyz$' /this/path/does/not/exist 2>/dev/null | head -n 1"
        )
        assert "[blocked]" not in result
        assert "No such file" not in result
        assert "grep:" not in result

    async def test_empty_stdout_reports_exit_code(self):
        """Commands with no stdout still return a non-empty string for the LLM."""
        result = await run_safe("grep '^zzzzshellclaw_nomatch_zzzz$' /etc/passwd")
        assert "exit code" in result.lower()
        assert "no output" in result.lower()

    async def test_output_truncated(self, monkeypatch):
        # Monkeypatch ALLOWED_COMMANDS to include 'yes' for this test
        import shellclaw.safety.sandbox as sb
        original = sb.ALLOWED_COMMANDS
        sb.ALLOWED_COMMANDS = frozenset(original | {"yes"})
        try:
            result = await run_safe("yes", timeout=1)
            assert len(result.encode()) <= MAX_OUTPUT_BYTES + len(TRUNCATION_NOTICE) + 10
        except Exception:
            pass
        finally:
            sb.ALLOWED_COMMANDS = original

    async def test_timeout_returns_message(self):
        import shellclaw.safety.sandbox as sb
        original = sb.ALLOWED_COMMANDS
        sb.ALLOWED_COMMANDS = frozenset(original | {"sleep"})
        try:
            result = await run_safe("sleep 10", timeout=1)
            assert "timed out" in result
        finally:
            sb.ALLOWED_COMMANDS = original


def test_parse_extra_allowed_command_bases() -> None:
    assert parse_extra_allowed_command_bases("") == frozenset()
    assert parse_extra_allowed_command_bases("  ") == frozenset()
    assert parse_extra_allowed_command_bases("btop, ncdu") == frozenset({"btop", "ncdu"})
    assert parse_extra_allowed_command_bases("/usr/bin/foo, bar") == frozenset({"foo", "bar"})


def test_validate_extra_bases_from_dispatch_context() -> None:
    import shellclaw.safety.sandbox as sb

    assert "yes" not in sb.ALLOWED_COMMANDS
    token = sb.dispatch_extra_allowed_command_bases.set(frozenset({"yes"}))
    try:
        validate("yes")
    finally:
        sb.dispatch_extra_allowed_command_bases.reset(token)
