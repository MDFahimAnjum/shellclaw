"""Tests for the wiki/explain pipeline."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch


class TestTldr:
    def test_lookup_known_command(self):
        from shellclaw.wiki.tldr import lookup
        result = lookup("df")
        assert result is not None
        assert "description" in result
        assert "examples" in result
        assert len(result["examples"]) > 0

    def test_lookup_unknown_command_returns_none(self):
        from shellclaw.wiki.tldr import lookup
        result = lookup("xyzzy_nonexistent_command_12345")
        assert result is None

    def test_lookup_case_insensitive(self):
        from shellclaw.wiki.tldr import lookup
        assert lookup("DF") == lookup("df")

    def test_format_for_llm_returns_string(self):
        from shellclaw.wiki.tldr import format_for_llm
        result = format_for_llm("journalctl")
        assert isinstance(result, str)
        assert "journalctl" in result.lower()

    def test_format_for_llm_unknown(self):
        from shellclaw.wiki.tldr import format_for_llm
        result = format_for_llm("not_a_real_command")
        assert "No tldr page found" in result


class TestGlossary:
    def test_glossary_file_exists(self):
        from shellclaw.wiki.glossary import DATA_PATH
        assert DATA_PATH.exists()

    def test_lookup_known_term(self):
        from shellclaw.wiki.glossary import lookup
        result = lookup("kernel")
        assert result is not None
        assert len(result) > 10

    def test_lookup_case_insensitive(self):
        from shellclaw.wiki.glossary import lookup
        assert lookup("KERNEL") == lookup("kernel")

    def test_lookup_unknown_returns_none(self):
        from shellclaw.wiki.glossary import lookup
        assert lookup("xyzzy_not_a_linux_term") is None

    def test_find_terms_in_text(self):
        from shellclaw.wiki.glossary import find_terms
        terms = find_terms("The kernel is the core of your daemon-based system.")
        assert "kernel" in terms
        assert "daemon" in terms

    def test_find_terms_empty_text(self):
        from shellclaw.wiki.glossary import find_terms
        assert find_terms("") == []

    def test_all_terms_returns_dict(self):
        from shellclaw.wiki.glossary import all_terms
        data = all_terms()
        assert isinstance(data, dict)
        assert len(data) >= 50


class TestParser:
    def test_extract_flags_long(self):
        from shellclaw.wiki.parser import extract_flags
        flags = extract_flags("journalctl --since '1 hour ago' --no-pager")
        assert "--since" in flags
        assert "--no-pager" in flags

    def test_extract_flags_short(self):
        from shellclaw.wiki.parser import extract_flags
        flags = extract_flags("df -h -a")
        assert "-h" in flags
        assert "-a" in flags

    def test_extract_flags_mixed(self):
        from shellclaw.wiki.parser import extract_flags
        flags = extract_flags("ps -aux --sort=-%cpu")
        assert "-aux" in flags or "-a" in flags or "-u" in flags or "-x" in flags
        assert "--sort" in flags

    def test_extract_flags_no_flags(self):
        from shellclaw.wiki.parser import extract_flags
        assert extract_flags("df") == []

    def test_describe_flags_returns_list(self):
        from shellclaw.wiki.parser import describe_flags
        result = describe_flags("df -h")
        assert isinstance(result, list)
        for item in result:
            assert "flag" in item
            assert "description" in item
            assert "command" in item
            assert item["command"].lower() == "df"


class TestCommands:
    def test_split_pipeline_respects_quotes(self):
        from shellclaw.wiki.commands import split_pipeline_segments

        assert split_pipeline_segments("echo '|' | wc -l") == ["echo '|'", "wc -l"]

    def test_command_names_strip_sudo(self):
        from shellclaw.wiki.commands import command_names_in_shell_line

        assert command_names_in_shell_line("sudo reboot") == ["reboot"]
        assert command_names_in_shell_line("sudo -n reboot") == ["reboot"]
        assert command_names_in_shell_line("sudo -u nobody id") == ["id"]

    def test_command_names_pipeline(self):
        from shellclaw.wiki.commands import command_names_in_shell_line

        assert command_names_in_shell_line("df -h | grep tmp") == ["df", "grep"]

    def test_format_for_shell_line_multi(self):
        from shellclaw.wiki.tldr import format_for_shell_line

        text = format_for_shell_line("sudo df -h | du -sh .")
        assert "# df" in text
        assert "# du" in text

    def test_find_terms_for_shell_command_normalizes_sudo(self):
        from shellclaw.wiki.glossary import find_terms_for_shell_command

        terms = find_terms_for_shell_command("sudo dmesg | grep kernel")
        assert "kernel" in terms
