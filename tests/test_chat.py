"""Chat: post/history roundtrip, channel isolation, JSONL persistence."""

from __future__ import annotations

import json

import pytest

from quarterdeck.chat import Chat
from quarterdeck.events import CHAT_MESSAGE, EventBus


@pytest.fixture()
def chat(tmp_path):
    return Chat(home=tmp_path / "qd")


def test_post_and_history(chat):
    chat.post("#general", "you", "hello agents")
    chat.post("#general", "scout", "on the horizon, captain")
    msgs = chat.history("#general")
    assert len(msgs) == 2
    assert msgs[0]["author"] == "you" and msgs[0]["text"] == "hello agents"
    assert msgs[1]["author"] == "scout"
    assert msgs[0]["channel"] == "#general"
    assert msgs[0]["ts"] > 0


def test_history_limit(chat):
    for i in range(10):
        chat.post("#general", "you", f"msg {i}")
    msgs = chat.history("#general", 3)
    assert [m["text"] for m in msgs] == ["msg 7", "msg 8", "msg 9"]


def test_channels_isolated(chat):
    chat.post("#general", "you", "in general")
    chat.post("#alerts", "scout", "in alerts")
    assert len(chat.history("#general")) == 1
    assert len(chat.history("#alerts")) == 1
    assert chat.history("#nope") == []


def test_persists_across_instances(tmp_path):
    c1 = Chat(home=tmp_path / "qd")
    c1.post("#general", "you", "stored")
    c2 = Chat(home=tmp_path / "qd")
    assert c2.history("#general")[0]["text"] == "stored"
    log = tmp_path / "qd" / "chat" / "general.jsonl"
    assert json.loads(log.read_text().splitlines()[0])["author"] == "you"


def test_post_validates(chat):
    with pytest.raises(ValueError):
        chat.post("#general", "", "no author")
    with pytest.raises(ValueError):
        chat.post("", "you", "no channel")


def test_post_publishes_chat_message_event(tmp_path):
    bus = EventBus()
    got = []
    bus.subscribe(CHAT_MESSAGE, got.append)
    Chat(home=tmp_path / "qd", bus=bus).post("#general", "scout", "reporting in")
    assert len(got) == 1
    assert got[0].payload["author"] == "scout"
    assert got[0].payload["text"] == "reporting in"
