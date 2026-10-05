"""Tests for thought streams, agent tools, and subscriptions."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from quarterdeck import subscriptions
from quarterdeck.agent_stream import post_thought, thought_stream_log
from quarterdeck.chat import Chat
from quarterdeck.thoughts import thought_channel
from quarterdeck.tools import quarterdeck_tools
from quarterdeck.web import create_app


@pytest.fixture
def home(tmp_path):
    return tmp_path / "qd"


@pytest.fixture
def client(home):
    app = create_app(home=home)
    return TestClient(app)


def test_thought_channel_naming():
    assert thought_channel("Alice") == "#thoughts-alice"
    assert thought_channel("Bob Smith") == "#thoughts-bob_smith"
    with pytest.raises(ValueError):
        thought_channel("   ")


def test_post_thought_api(client):
    r = client.post("/api/agents/scout/thoughts", json={"text": "hmm, interesting"})
    assert r.status_code == 200
    data = r.json()
    assert data["author"] == "scout"
    assert data["thought"] is True
    assert data["channel"] == "#thoughts-scout"


def test_post_thought_rejects_empty(client):
    r = client.post("/api/agents/scout/thoughts", json={"text": "   "})
    assert r.status_code == 400


def test_get_thoughts_api(client):
    client.post("/api/agents/scout/thoughts", json={"text": "first"})
    client.post("/api/agents/scout/thoughts", json={"text": "second"})
    r = client.get("/api/agents/scout/thoughts")
    assert r.status_code == 200
    thoughts = r.json()["thoughts"]
    assert len(thoughts) == 2
    assert all(t["thought"] for t in thoughts)


def test_thought_websocket_stream(client):
    client.post("/api/agents/scout/thoughts", json={"text": "before connect"})
    with client.websocket_connect("/ws/thoughts/scout") as ws:
        msg = ws.receive_json()
        assert msg["type"] == "history"
        assert len(msg["messages"]) == 1
        # Post another thought, expect live broadcast
        client.post("/api/agents/scout/thoughts", json={"text": "live thought"})
        live = ws.receive_json()
        assert live["text"] == "live thought"
        assert live["thought"] is True



# --- tools.py ---

def test_tool_names_exact(home):
    tools = quarterdeck_tools("agent1", home=home)
    names = {t.name for t in tools}
    for required in ["quarterdeck_send", "quarterdeck_subscribe",
                     "quarterdeck_unsubscribe", "quarterdeck_mentions",
                     "quarterdeck_read", "quarterdeck_dm",
                     "quarterdeck_think", "quarterdeck_list"]:
        assert required in names, f"missing {required}"


def test_send_read_roundtrip(home):
    tools = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    out = tools["quarterdeck_send"].func("#general", "hello world")
    assert "sent to #general" in out
    out = tools["quarterdeck_read"].func("#general", 5)
    assert "hello world" in out
    assert "alice" in out


def test_dm_creates_deterministic_channel(home):
    tools = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    out1 = tools["quarterdeck_dm"].func("bob", "secret")
    out2 = tools["quarterdeck_dm"].func("bob", "secret2")
    assert "DM sent to bob" in out1
    # Bob's view: same channel
    bob_tools = {t.name: t for t in quarterdeck_tools("bob", home=home)}
    out = bob_tools["quarterdeck_read"].func("#dm-alice-bob", 5)
    assert "secret" in out


def test_think_posts_to_thought_stream(home):
    tools = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    out = tools["quarterdeck_think"].func("pondering the target")
    assert "thought recorded" in out
    chat = Chat(home=home)
    msgs = chat.history("#thoughts-alice", n=5)
    assert any("pondering" in m["text"] for m in msgs)


def test_subscribe_unsubscribe(home):
    tools = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    out = tools["quarterdeck_subscribe"].func("#general,#random", "urgent")
    assert "#general" in out
    subs = subscriptions.get_subscriptions("alice", home=home)
    assert "#general" in subs["channels"]
    assert subs["channels"]["#general"]["filters"] == ["urgent"]
    out = tools["quarterdeck_unsubscribe"].func("#random")
    assert "unsubscribed from: #random" in out


def test_mentions_detects_at_mention(home):
    alice = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    bob = {t.name: t for t in quarterdeck_tools("bob", home=home)}
    alice["quarterdeck_subscribe"].func("#general", "")
    # Clear the watermark by checking once
    alice["quarterdeck_mentions"].func()
    bob["quarterdeck_send"].func("#general", "hey @alice look at this")
    out = alice["quarterdeck_mentions"].func()
    assert "1 new mention" in out
    assert "@alice" in out
    # Second check: no new mentions (watermark advanced)
    out = alice["quarterdeck_mentions"].func()
    assert "no new mentions" in out


def test_mentions_ignores_own_messages(home):
    tools = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    tools["quarterdeck_subscribe"].func("#general", "")
    tools["quarterdeck_mentions"].func()  # baseline
    tools["quarterdeck_send"].func("#general", "note to self @alice")
    out = tools["quarterdeck_mentions"].func()
    assert "no new mentions" in out


def test_list_channels_and_agents(home):
    tools = {t.name: t for t in quarterdeck_tools("alice", home=home)}
    tools["quarterdeck_send"].func("#general", "hi")
    out = tools["quarterdeck_list"].func("channels")
    assert "#general" in out
    out = tools["quarterdeck_list"].func("agents")
    assert isinstance(out, str)  # empty registry is fine


def test_subscriptions_persist(home):
    subscriptions.subscribe("alice", ["#general"], ["kw"], home=home)
    # New "process": reload from disk
    subs = subscriptions.get_subscriptions("alice", home=home)
    assert "#general" in subs["channels"]
    assert subs["channels"]["#general"]["filters"] == ["kw"]


# --- agent_stream ---

def test_post_thought_direct(home):
    assert post_thought("bob", "thinking...", home=home) is True
    assert post_thought("bob", "   ", home=home) is False
    chat = Chat(home=home)
    msgs = chat.history("#thoughts-bob", n=5)
    assert len(msgs) == 1


def test_thought_stream_log_wrapper(home):
    seen = []
    log = thought_stream_log("carol", base_log=seen.append, home=home)
    log("[pinnace] thinking: the login form is interesting")
    log("regular output line")
    assert len(seen) == 2  # passthrough works
    chat = Chat(home=home)
    msgs = chat.history("#thoughts-carol", n=5)
    assert len(msgs) == 1
    assert "login form" in msgs[0]["text"]


def test_thought_stream_log_no_base(home):
    log = thought_stream_log("dave", home=home)
    log("[pinnace] thinking: quiet thought")  # should not raise
    chat = Chat(home=home)
    assert len(chat.history("#thoughts-dave", n=5)) == 1
