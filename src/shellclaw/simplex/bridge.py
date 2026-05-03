"""Async WebSocket bridge to ``simplex-chat -p`` (bot API)."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import websockets

from .protocol import (
    _chat_cmd_error_message,
    extract_invitation_link_from_resp,
    extract_incoming_user_texts,
    first_chat_ref_from_new_chat_items,
    normalize_chat_ref,
)


class SimpleXError(Exception):
    """Raised when the SimpleX CLI returns an error or the bridge is misconfigured."""


class SimpleXBridge:
    """Spawn ``simplex-chat``, maintain one WebSocket, correlate commands, dispatch events."""

    def __init__(
        self,
        *,
        database_prefix: Path,
        port: int,
        executable: str = "simplex-chat",
        chat_ref: str = "",
        accept_any_chat: bool = False,
        on_inbound_user_text: Callable[[str], Awaitable[None]] | None = None,
    ) -> None:
        self._database_prefix = database_prefix
        self._port = int(port)
        self._executable = executable
        self._chat_ref = normalize_chat_ref(chat_ref) if chat_ref else ""
        self._accept_any_chat = accept_any_chat
        self._on_inbound = on_inbound_user_text

        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._recv_task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._detected_chat_ref: str | None = None

    @property
    def chat_ref(self) -> str:
        return self._chat_ref

    @property
    def is_running(self) -> bool:
        return self._ws is not None and self._recv_task is not None

    def set_chat_ref(self, ref: str) -> None:
        self._chat_ref = normalize_chat_ref(ref)

    def set_accept_any_chat(self, flag: bool) -> None:
        self._accept_any_chat = flag

    @property
    def detected_chat_ref(self) -> str | None:
        return self._detected_chat_ref

    async def start(self) -> None:
        if self.is_running:
            return
        self._detected_chat_ref = None
        self._database_prefix.parent.mkdir(parents=True, exist_ok=True)
        args = [
            self._executable,
            "-d",
            str(self._database_prefix),
            "-p",
            str(self._port),
            "-y",
            "--create-bot-display-name",
            "shellclaw",
        ]
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        except FileNotFoundError as exc:
            raise SimpleXError(
                f"SimpleX executable not found: {self._executable!r}"
            ) from exc

        uri = f"ws://127.0.0.1:{self._port}"
        last_exc: Exception | None = None
        for _ in range(50):
            try:
                self._ws = await websockets.connect(
                    uri,
                    ping_interval=20,
                    ping_timeout=60,
                )
                break
            except (OSError, asyncio.TimeoutError, websockets.exceptions.WebSocketException) as exc:
                last_exc = exc
                if self._proc.returncode is not None:
                    raise SimpleXError(
                        f"simplex-chat exited early (code {self._proc.returncode})"
                    ) from exc
                await asyncio.sleep(0.1)
        else:
            await self.stop()
            raise SimpleXError(f"Could not open WebSocket {uri!r}: {last_exc!r}")

        self._stopped.clear()
        self._recv_task = asyncio.create_task(self._recv_loop(), name="simplex_ws_recv")

    async def stop(self) -> None:
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
            self._recv_task = None

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

        if self._proc is not None:
            if self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    self._proc.kill()
                    await self._proc.wait()
            self._proc = None

        self._stopped.set()

    async def send_command(self, cmd: str, *, timeout: float = 120.0) -> dict[str, Any]:
        """Send a CLI command string and wait for the correlated ``resp`` object."""
        if self._ws is None:
            raise SimpleXError("WebSocket is not connected")
        cid = str(uuid.uuid4())
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[cid] = fut
        payload = json.dumps({"corrId": cid, "cmd": cmd}, separators=(",", ":"))
        await self._ws.send(payload)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(cid, None)

    async def request_connect_link(self) -> str:
        resp = await self.send_command("/connect", timeout=120.0)
        link = extract_invitation_link_from_resp(resp)
        if link:
            return link
        err = _chat_cmd_error_message(resp)
        raise SimpleXError(err or "Could not parse invitation link from /connect response")

    async def send_chat_text(self, text: str) -> None:
        if not self._chat_ref:
            raise SimpleXError("chat_ref is not set")
        cmd = _build_send_command(self._chat_ref, text)
        resp = await self.send_command(cmd, timeout=120.0)
        err = _chat_cmd_error_message(resp)
        if err:
            raise SimpleXError(err)

    async def _recv_loop(self) -> None:
        assert self._ws is not None
        try:
            async for raw in self._ws:
                data = _parse_json_dict(raw)
                if data is None:
                    continue
                cid = data.get("corrId")
                body = data.get("resp")
                if not isinstance(body, dict):
                    continue
                if isinstance(body, dict) and isinstance(cid, str) and cid in self._pending:
                    fut = self._pending.get(cid)
                    if fut is not None and not fut.done():
                        fut.set_result(body)
                if isinstance(body, dict):
                    await self._dispatch_incoming(body)
        except asyncio.CancelledError:
            raise
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            pass

    async def _dispatch_incoming(self, body: dict[str, Any]) -> None:
        if body.get("type") == "newChatItems":
            cref = first_chat_ref_from_new_chat_items(body)
            if cref:
                self._detected_chat_ref = normalize_chat_ref(cref)
        if body.get("type") != "newChatItems":
            return
        allowed: frozenset[str] | None
        if self._accept_any_chat or not self._chat_ref:
            allowed = None
        else:
            allowed = frozenset({normalize_chat_ref(self._chat_ref)})
        pairs = extract_incoming_user_texts(body, allowed_chat_refs=allowed)
        if not pairs or self._on_inbound is None:
            return
        for _cref, text in pairs:
            await self._on_inbound(text)


def _parse_json_dict(raw: str | bytes) -> dict[str, Any] | None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def _build_send_command(chat_ref: str, text: str) -> str:
    ref = normalize_chat_ref(chat_ref)
    if "\n" in text or "\r" in text or '"' in text or len(text) > 60000:
        composed = [{"msgContent": {"type": "text", "text": text}}]
        return f"/_send {ref} json {json.dumps(composed, separators=(',', ':'))}"
    # Single-line messages: ``/_send @n text ...`` (CLI syntax).
    return f"/_send {ref} text {text}"
