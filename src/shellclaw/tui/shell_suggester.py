"""Textual suggester: complete the last shell token against :class:`CommandDatabase`."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from textual.suggester import Suggester

if TYPE_CHECKING:
    from .command_db import CommandDatabase

# Prefix before the final token: non-greedy so the last group is the partial command word.
_LAST_TOKEN = re.compile(r"^(?P<prefix>.*?)(?P<token>[^\s;&|]*)$")


class ShellCommandSuggester(Suggester):
    """Return a full input string whose last token is a longer matching command name.

    Textual compares the suggestion to the whole ``value``; we only extend the final token.
    Caching is disabled because PATH results merge after mount.
    """

    def __init__(self, db: CommandDatabase) -> None:
        super().__init__(use_cache=False, case_sensitive=True)
        self._db = db

    async def get_suggestion(self, value: str) -> str | None:
        m = _LAST_TOKEN.match(value)
        if not m:
            return None
        partial = m.group("token")
        if not partial:
            return None
        prefix = m.group("prefix")
        candidates = self._db.completion_candidates(partial)
        p = partial.casefold()
        for cmd in candidates:
            if cmd.casefold() == p:
                continue
            return prefix + cmd
        return None
