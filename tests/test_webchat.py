"""Tests for Quarterdeck webchat: agent files, web API, websocket live-tail."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from quarterdeck.agent_files import AGENT_FILES, AgentFiles
from quarterdeck.web import create_app, dm_channel


@pytest.fixture()
def tmp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUARTERDECK_HOME", str(tmp_path))
    return tmp_path


@pytest.fixture()
def files(tmp_home):
    return AgentFiles(home=tmp_home)


@pytest.fixture()
def client(tmp_home):
    app = create_app(home=tmp_home)
    return TestClient(app)


class TestAgentFiles:
    def test_filenames_exact(self):
        assert AGENT_FILES == ("memory.md", "runbook.md", "identify.md")

    def test_roundtrip(self, files):
        files.write("scout", "memory.md", "# Scout\nLikes subdomains.")
        assert files.read("scout", "memory.md") == "# Scout\nLikes subdomains."

    def test_missing_reads_empty(self, files):
        assert files.read("ghost", "identify.md") == ""

    def test_rejects_unknown_file(self, files):
        with pytest.raises(ValueError):
            files.write("scout", "notes.txt", "x")

    def test_all_returns_three(self, files):
        files.write("scout", "runbook.md", "run")
        d = files.all("scout")
        assert set(d) == set(AGENT_FILES)
        assert d["runbook.md"] == "run"


class TestWebAPI:
    def test_channels_empty_initially(self, client):
        r = client.get("/api/channels")
        assert r.status_code == 200
        assert r.json() == {"channels": []}

    def test_post_and_history(self, client):
        r = client.post("/api/channels/%23general/messages",
                        json={"author": "human", "text": "hello room"})
        assert r.status_code == 200
        assert r.json()["text"] == "hello room"
        h = client.get("/api/channels/%23general/messages").json()
        assert len(h["messages"]) == 1
        assert h["messages"][0]["author"] == "human"

    def test_create_channel(self, client):
        r = client.post("/api/channels", json={"name": "ops"})
        assert r.status_code == 200
        assert r.json()["channel"] == "#ops"

    def test_dm_creates_deterministic_channel(self, client):
        r = client.post("/api/dm", json={
            "sender": "human", "recipient": "scout", "text": "hey"})
        assert r.status_code == 200
        assert r.json()["channel"] == "#dm-human-scout"
        # reverse order -> same channel
        assert dm_channel("scout", "human") == "#dm-human-scout"

    def test_agent_files_api(self, client):
        r = client.put("/api/agents/scout/files/memory.md",
                       json={"content": "# memory"})
        assert r.status_code == 200
        r = client.get("/api/agents/scout/files/memory.md")
        assert r.json()["content"] == "# memory"
        r = client.get("/api/agents/scout/files")
        assert r.json()["files"]["memory.md"] == "# memory"

    def test_agent_file_rejects_unknown(self, client):
        r = client.get("/api/agents/scout/files/notes.txt")
        assert r.status_code == 404

    def test_websocket_live_tail(self, client):
        with client.websocket_connect("/ws/%23general") as ws:
            first = ws.receive_json()
            assert first["type"] == "history"
            assert first["messages"] == []
            ws.send_text(json.dumps(
                {"type": "post", "author": "human", "text": "live hello"}))
            msg = ws.receive_json()
            assert msg["text"] == "live hello"
            assert msg["author"] == "human"

    def test_websocket_receives_rest_posts(self, client):
        with client.websocket_connect("/ws/%23general") as ws:
            ws.receive_json()  # history
            client.post("/api/channels/%23general/messages",
                        json={"author": "scout", "text": "via rest"})
            msg = ws.receive_json()
            assert msg["text"] == "via rest"
            assert msg["author"] == "scout"
