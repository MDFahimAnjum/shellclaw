"""Unit tests for SimpleX WebSocket JSON parsing helpers."""

from __future__ import annotations

from shellclaw.simplex.protocol import (
    extract_invitation_link_from_resp,
    extract_incoming_user_texts,
    first_chat_ref_from_new_chat_items,
    normalize_chat_ref,
    parse_chat_refs_from_chats_text,
)


def test_normalize_chat_ref() -> None:
    assert normalize_chat_ref("") == ""
    assert normalize_chat_ref("3") == "@3"
    assert normalize_chat_ref("@2") == "@2"
    assert normalize_chat_ref("#5") == "#5"


def test_extract_invitation_link_invitation_type() -> None:
    link = "https://simplex.chat/inv#test_one_line"
    resp = {
        "type": "invitation",
        "connLinkInvitation": {"connLink": link},
    }
    assert extract_invitation_link_from_resp(resp) == link


def test_extract_invitation_link_nested_walk() -> None:
    link = "https://simplex.chat/inv#nested"
    resp = {"type": "sentConfirmation", "connection": {"outer": {"connReq": link}}}
    assert extract_invitation_link_from_resp(resp) == link


def test_extract_incoming_user_texts_filters_chat_ref() -> None:
    body = {
        "type": "newChatItems",
        "chatItems": [
            {
                "chatInfo": {"type": "direct", "contact": {"contactId": 1}},
                "chatItem": {
                    "chatDir": {"type": "directRcv"},
                    "content": {
                        "type": "rcvMsgContent",
                        "msgContent": {"type": "text", "text": "hello"},
                    },
                },
            },
            {
                "chatInfo": {"type": "direct", "contact": {"contactId": 2}},
                "chatItem": {
                    "chatDir": {"type": "directRcv"},
                    "content": {
                        "type": "rcvMsgContent",
                        "msgContent": {"type": "text", "text": "other"},
                    },
                },
            },
        ],
    }
    allowed = frozenset({"@1"})
    pairs = extract_incoming_user_texts(body, allowed_chat_refs=allowed)
    assert pairs == [("@1", "hello")]


def test_extract_incoming_accepts_string_contact_id() -> None:
    body = {
        "type": "newChatItems",
        "chatItems": [
            {
                "chatInfo": {"type": "direct", "contact": {"contactId": "3"}},
                "chatItem": {
                    "chatDir": "directRcv",
                    "content": {
                        "type": "rcvMsgContent",
                        "msgContent": {"type": "text", "text": "hi"},
                    },
                },
            }
        ],
    }
    pairs = extract_incoming_user_texts(body, allowed_chat_refs=frozenset({"@3"}))
    assert pairs == [("@3", "hi")]


def test_first_chat_ref_from_new_chat_items() -> None:
    body = {
        "type": "newChatItems",
        "chatItems": [
            {
                "chatInfo": {"type": "direct", "contact": {"contactId": 7}},
                "chatItem": {"chatDir": "directRcv", "content": {"type": "text", "text": "x"}},
            }
        ],
    }
    assert first_chat_ref_from_new_chat_items(body) == "@7"


def test_parse_chat_refs_from_chats_text() -> None:
    text = "note @1 and #2 duplicate @1"
    assert parse_chat_refs_from_chats_text(text) == ["@1", "#2"]
