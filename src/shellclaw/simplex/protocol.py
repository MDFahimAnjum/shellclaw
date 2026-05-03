"""Parse SimpleX Chat CLI WebSocket JSON (command responses and events)."""

from __future__ import annotations

import json
import re
from typing import Any

# One-line invitation / contact links from SimpleX.
_LINK_HINTS = (
    "https://simplex.chat",
    "http://simplex.chat",
    "simplex:",
)


def normalize_chat_ref(ref: str) -> str:
    r = (ref or "").strip()
    if not r:
        return ""
    if r[0] in "@#":
        return r
    return f"@{r}"


def looks_like_simplex_link(text: str) -> bool:
    t = text.strip()
    return bool(t) and any(h in t for h in _LINK_HINTS)


def _walk_strings(obj: Any, found: list[str]) -> None:
    if isinstance(obj, str):
        found.append(obj)
    elif isinstance(obj, dict):
        for v in obj.values():
            _walk_strings(v, found)
    elif isinstance(obj, list):
        for v in obj:
            _walk_strings(v, found)


def extract_invitation_link_from_resp(resp: dict[str, Any]) -> str | None:
    """Best-effort extract one-line SimpleX link from a ``/connect`` command ``resp``."""
    if not isinstance(resp, dict):
        return None
    err = _chat_cmd_error_message(resp)
    if err:
        return None

    t = resp.get("type")
    # APIAddContact / interactive ``/connect`` — invitation with created link.
    if t == "invitation":
        inv = resp.get("connLinkInvitation")
        if isinstance(inv, dict):
            for key in ("connLink", "connectionLink", "link", "uri", "simplexChatUri"):
                v = inv.get(key)
                if isinstance(v, str) and looks_like_simplex_link(v):
                    return v.strip()
        conn = resp.get("connection")
        if isinstance(conn, dict):
            link = _link_from_pending_connection(conn)
            if link:
                return link

    if t in ("sentConfirmation", "sentInvitation"):
        conn = resp.get("connection")
        if isinstance(conn, dict):
            link = _link_from_pending_connection(conn)
            if link:
                return link

    strings: list[str] = []
    _walk_strings(resp, strings)
    for s in strings:
        if looks_like_simplex_link(s) and "\n" not in s.strip():
            return s.strip()
    return None


def _link_from_pending_connection(conn: dict[str, Any]) -> str | None:
    for key in ("connReq", "connLink", "connectionLink", "simplexChatUri", "uri"):
        v = conn.get(key)
        if isinstance(v, str) and looks_like_simplex_link(v):
            return v.strip()
    strings: list[str] = []
    _walk_strings(conn, strings)
    for s in strings:
        if looks_like_simplex_link(s):
            return s.strip()
    return None


def _chat_cmd_error_message(resp: dict[str, Any]) -> str | None:
    if resp.get("type") != "chatCmdError":
        return None
    err = resp.get("chatError")
    if not isinstance(err, dict):
        return "chatCmdError"
    et = err.get("errorType")
    if isinstance(et, dict):
        msg = et.get("message")
        if isinstance(msg, str):
            return msg
        inner = et.get("type")
        if isinstance(inner, str):
            return inner
    return "chatCmdError"


def chat_ref_from_chat_info(chat_info: dict[str, Any]) -> str | None:
    """Derive ``@n`` / ``#n`` from a ``chatInfo`` object inside ``newChatItems``."""
    if not isinstance(chat_info, dict):
        return None
    kind = chat_info.get("type")
    if kind == "direct":
        c = chat_info.get("contact")
        if isinstance(c, dict):
            cid = _coerce_int_id(c.get("contactId"))
            if cid is not None:
                return f"@{cid}"
    if kind == "group":
        g = chat_info.get("groupInfo") or chat_info.get("group")
        if isinstance(g, dict):
            gid = _coerce_int_id(g.get("groupId"))
            if gid is not None:
                return f"#{gid}"
    return None


def _coerce_int_id(val: Any) -> int | None:
    if val is None or isinstance(val, bool):
        return None
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        return int(val)
    if isinstance(val, str) and val.strip().isdigit():
        return int(val.strip())
    return None


def _cidirection_is_inbound(direction: Any) -> bool:
    """True when ``chatDir`` / ``CIDirection`` means traffic from the peer (not our client)."""
    if direction is None:
        return False
    if isinstance(direction, dict):
        direction = direction.get("type") or direction.get("tag")
    if not isinstance(direction, str):
        return False
    d = direction.lower()
    # Current SimpleX JSON (bots/api/TYPES.md): directRcv, groupRcv, …
    if d in ("directrcv", "grouprcv", "channelrcv", "localrcv"):
        return True
    # Older / alternate spellings
    if d in ("received", "rcv", "rcvdirect", "rcvgroup"):
        return True
    return False


def _text_from_msg_content(mc: Any) -> str | None:
    if not isinstance(mc, dict):
        return None
    mtype = mc.get("type")
    if mtype == "text":
        txt = mc.get("text")
        if isinstance(txt, str):
            return txt
    if mtype == "link":
        l = mc.get("link")
        if isinstance(l, str):
            return l
    return None


def _text_from_rcv_chat_item(chat_item: dict[str, Any]) -> str | None:
    """Extract user-visible text from a ``ChatItem`` (``CIContent`` + ``MsgContent``)."""
    content = chat_item.get("content")
    if isinstance(content, dict):
        ctype = content.get("type")
        if ctype == "rcvMsgContent":
            mc = content.get("msgContent")
            if isinstance(mc, dict):
                t = _text_from_msg_content(mc)
                if t is not None:
                    return t.strip() or None
        # Flat MsgContent on ``content`` (older / alternate encodings)
        t = _text_from_msg_content(content)
        if t is not None:
            return t.strip() or None
    return _text_from_chat_item_body(chat_item)


def _text_from_chat_item_body(body: dict[str, Any]) -> str | None:
    """Extract user-visible plain text from a ``chatItem`` JSON object."""
    if not isinstance(body, dict):
        return None
    content = body.get("content")
    if isinstance(content, dict):
        t = _text_from_msg_content(content)
        if t is not None:
            return t
    # Some shapes nest msgContent / formatted.
    mc = body.get("msgContent")
    if isinstance(mc, dict):
        t = _text_from_msg_content(mc)
        if t is not None:
            return t
    ci = body.get("chatItem")
    if isinstance(ci, dict):
        return _text_from_chat_item_body(ci)
    return None


def extract_incoming_user_texts(
    resp: dict[str, Any],
    *,
    allowed_chat_refs: frozenset[str] | None,
) -> list[tuple[str, str]]:
    """Return ``(chat_ref, text)`` for received user text from ``newChatItems`` events.

    ``allowed_chat_refs`` — if set, only items whose ``chat_ref`` is in this set
    (after :func:`normalize_chat_ref`). If ``None``, accept any direct received text
    (used during first-time setup to learn ``chat_ref``).
    """
    if resp.get("type") != "newChatItems":
        return []
    items = resp.get("chatItems")
    if not isinstance(items, list):
        return []
    out: list[tuple[str, str]] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        chat_info = entry.get("chatInfo")
        cref = chat_ref_from_chat_info(chat_info) if isinstance(chat_info, dict) else None
        if cref is None:
            continue
        if allowed_chat_refs is not None:
            n = normalize_chat_ref(cref)
            if n not in allowed_chat_refs:
                continue
        chat_item = entry.get("chatItem")
        if not isinstance(chat_item, dict):
            continue
        direction = chat_item.get("chatDir") or chat_item.get("direction")
        if not _cidirection_is_inbound(direction):
            continue
        text = _text_from_rcv_chat_item(chat_item)
        if text is None or not text.strip():
            continue
        out.append((cref, text.strip()))
    return out


def parse_ws_json(raw: str) -> dict[str, Any] | None:
    try:
        val = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    return val if isinstance(val, dict) else None


def response_body(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return the ``resp`` object from a WebSocket frame (command response or event)."""
    r = payload.get("resp")
    if isinstance(r, dict):
        return r
    # Some builds may send the record at top level with ``type``.
    if "type" in payload and "corrId" not in payload:
        return payload
    return None


def first_chat_ref_from_new_chat_items(resp: dict[str, Any]) -> str | None:
    """First ``@n`` / ``#n`` found in a ``newChatItems`` payload (for setup auto-detect)."""
    if resp.get("type") != "newChatItems":
        return None
    items = resp.get("chatItems")
    if not isinstance(items, list):
        return None
    for entry in items:
        if not isinstance(entry, dict):
            continue
        ci = entry.get("chatInfo")
        if isinstance(ci, dict):
            r = chat_ref_from_chat_info(ci)
            if r:
                return r
    return None


def parse_chat_refs_from_chats_text(text: str) -> list[str]:
    """Very loose parse of ``/chats`` plain-text output for ``@n`` / ``#n`` tokens."""
    refs = re.findall(r"[@#]\d+", text)
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out
