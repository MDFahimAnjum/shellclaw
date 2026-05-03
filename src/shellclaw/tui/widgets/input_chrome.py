"""One-line text actions (Clear / Send / Stop) for chat and terminal input strips."""

from __future__ import annotations

from typing import Literal

from textual.message import Message
from textual.widgets import Static

# Shown on chrome buttons; MainScreen / text areas handle the same F-keys.
CHAT_CLEAR_LABEL = " Clear F3 "
TERM_CLEAR_LABEL = " Clear F4 "
CHAT_SEND_STOP_KEY = "F2"
TERM_SEND_KEY = "F2"
TERM_STOP_KEY = "F5"


class ChromePressed(Message):
    """User clicked a chrome action on the chat or terminal input strip."""

    bubble = True

    def __init__(
        self,
        target: Literal["chat", "terminal"],
        action: Literal["clear", "send", "stop"],
    ) -> None:
        super().__init__()
        self.target = target
        self.action = action


class ChromeAction(Static):
    """Compact clickable label with a solid background (like header Settings)."""

    DEFAULT_CSS = """
    ChromeAction {
        width: auto;
        min-width: 7;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        content-align: center middle;
        text-style: bold;
        color: $text;
    }
    ChromeAction:hover {
        text-style: underline;
        opacity: 0.9;
    }
    ChromeAction.-clear {
        background: $warning;
    }
    ChromeAction.-send {
        background: $accent;
    }
    ChromeAction.-stop {
        background: $error;
        color: $text;
    }
    """

    def __init__(
        self,
        label: str,
        *,
        target: Literal["chat", "terminal"],
        action: Literal["clear", "send", "stop"],
        **kwargs: object,
    ) -> None:
        super().__init__(label, classes=f"-{action}", **kwargs)
        self._target = target
        self._action = action

    def on_click(self) -> None:
        self.post_message(ChromePressed(self._target, self._action))


class SendStopChrome(Static):
    """Right slot: **Send** while idle; **Stop** while busy (same layout cell, no ghost gap)."""

    DEFAULT_CSS = """
    SendStopChrome {
        width: auto;
        min-width: 7;
        height: 1;
        padding: 0 1;
        margin-left: 1;
        content-align: center middle;
        text-style: bold;
        color: $text;
    }
    SendStopChrome:hover {
        text-style: underline;
        opacity: 0.9;
    }
    SendStopChrome.-send {
        background: $accent;
    }
    SendStopChrome.-stop {
        background: $error;
        color: $text;
    }
    """

    def __init__(
        self,
        *,
        target: Literal["chat", "terminal"],
        key_label: str = CHAT_SEND_STOP_KEY,
        stop_key_label: str | None = None,
        **kwargs: object,
    ) -> None:
        self._key_label = key_label
        self._stop_key_label = key_label if stop_key_label is None else stop_key_label
        super().__init__(
            f" Send {key_label} ",
            classes="-send",
            **kwargs,
        )
        self._target = target
        self._busy = False

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        k = self._stop_key_label if busy else self._key_label
        if busy:
            self.update(f" Stop {k} ")
            self.remove_class("-send")
            self.add_class("-stop")
        else:
            self.update(f" Send {k} ")
            self.remove_class("-stop")
            self.add_class("-send")

    @property
    def is_stop_mode(self) -> bool:
        return self._busy

    def on_click(self) -> None:
        self.post_message(
            ChromePressed(self._target, "stop" if self._busy else "send"),
        )
