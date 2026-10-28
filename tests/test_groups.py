"""Group chats: conversation list, membership, and mention routing."""

from __future__ import annotations

from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from quarterdeck.web import create_app


class _WakeDaemon:
    def __init__(self, names=("self", "scout", "atlas")):
        self.runners = {name: object() for name in names}
        self.calls: list[tuple[str, str]] = []

    def wake_agent(self, name, reason="dm"):
        self.calls.append((name, reason))
        return True


@pytest.fixture()
def daemon():
    return _WakeDaemon()


@pytest.fixture()
def client(tmp_path, daemon):
    return TestClient(create_app(home=tmp_path, daemon=daemon))


def _add_agents(client, names=("self", "scout", "atlas")):
    for name in names:
        client.post("/api/agents", json={"name": name, "system_prompt": name})


def _create_group(client, name="ops", members=("self", "scout")):
    response = client.post(
        "/api/groups", json={"name": name, "members": list(members)})
    assert response.status_code == 200
    return response.json()


def _channel_url(channel):
    return quote(channel, safe="")


def _post_as_agent(client, channel, author, text):
    return client.app.state.room.chat.post(channel, author, text)


class TestConversationList:
    def test_pinned_captain_chat_previews_and_unread(self, client):
        _add_agents(client)
        group = _create_group(client)
        client.post(
            f"/api/channels/{_channel_url(group['channel'])}/messages",
            json={"author": "operator", "text": "kickoff"})
        _post_as_agent(client, group["channel"], "scout", "on it")
        _post_as_agent(client, "#general", "self", "Welcome aboard")

        rows = client.get("/api/conversations").json()["conversations"]
        assert rows[0]["channel"] == "#general"
        assert rows[0]["pinned"] is True
        assert rows[0]["captain"] is True
        assert any(m["name"] == "self" for m in rows[0]["members"])
        assert rows[0]["preview"] == "self: Welcome aboard"
        assert rows[0]["unread"] == 1

        group_row = next(r for r in rows if r["channel"] == group["channel"])
        assert group_row["preview"] == "scout: on it"
        assert group_row["unread"] == 1
        assert [m["name"] for m in group_row["members"]] == ["self", "scout"]

        client.post(f"/api/conversations/{_channel_url(group['channel'])}/read")
        rows = client.get("/api/conversations").json()["conversations"]
        group_row = next(r for r in rows if r["channel"] == group["channel"])
        assert group_row["unread"] == 0

    def test_empty_group_still_appears(self, client):
        _add_agents(client)
        group = _create_group(client, name="quiet")
        rows = client.get("/api/conversations").json()["conversations"]
        assert any(r["channel"] == group["channel"] for r in rows)
        assert group["channel"] in client.get("/api/channels").json()["channels"]


class TestGroupMembership:
    def test_creation_joins_exactly_the_selected_agents(self, client):
        _add_agents(client)
        group = _create_group(client, name="triage",
                              members=("self", "scout"))
        assert group["members"] == ["self", "scout"]
        assert group["channel"] == "#triage"

        stored = client.get(f"/api/groups/{_channel_url(group['channel'])}").json()
        assert stored["members"] == ["self", "scout"]

        members = client.get(
            f"/api/channels/{_channel_url(group['channel'])}/members").json()["members"]
        assert [m["name"] for m in members] == ["self", "scout"]
        assert "atlas" not in [m["name"] for m in members]

    def test_channel_create_accepts_members(self, client):
        _add_agents(client)
        response = client.post(
            "/api/channels",
            json={"name": "deck", "members": ["self", "atlas"]})
        assert response.status_code == 200
        assert response.json()["members"] == ["self", "atlas"]
        assert client.get("/api/groups/%23deck").json()["members"] == [
            "self", "atlas"]

    def test_members_can_be_added_and_removed(self, client):
        _add_agents(client)
        group = _create_group(client)
        client.post(f"/api/channels/{_channel_url(group['channel'])}/members",
                    json={"members": ["atlas"]})
        members = client.get(
            f"/api/channels/{_channel_url(group['channel'])}/members").json()["members"]
        assert [m["name"] for m in members] == ["self", "scout", "atlas"]

        client.delete(f"/api/channels/{_channel_url(group['channel'])}/members/atlas")
        members = client.get(
            f"/api/channels/{_channel_url(group['channel'])}/members").json()["members"]
        assert [m["name"] for m in members] == ["self", "scout"]

    def test_leave_archives_for_the_operator(self, client):
        _add_agents(client)
        group = _create_group(client, name="fleeting")
        client.post(f"/api/groups/{_channel_url(group['channel'])}/leave")
        rows = client.get("/api/conversations").json()["conversations"]
        assert all(r["channel"] != group["channel"] for r in rows)
        archived = client.get(
            "/api/conversations?include_archived=true").json()["conversations"]
        assert any(r["channel"] == group["channel"] for r in archived)


class TestMentionRouting:
    def test_mention_wakes_only_the_mentioned_member(self, client, daemon):
        from quarterdeck import subscriptions

        _add_agents(client)
        group = _create_group(client, members=("self", "scout", "atlas"))
        # Even if other members subscribe, a group message is mention-driven.
        subscriptions.subscribe("scout", [group["channel"]], home=client.app.state.room.chat.home)
        subscriptions.subscribe("atlas", [group["channel"]], home=client.app.state.room.chat.home)

        client.post(
            f"/api/channels/{_channel_url(group['channel'])}/messages",
            json={"author": "operator", "text": "@scout check the logs"})

        assert daemon.calls == [("scout", "mention")]

    def test_unmentioned_group_message_wakes_nobody(self, client, daemon):
        _add_agents(client)
        group = _create_group(client, members=("self", "scout", "atlas"))
        client.post(
            f"/api/channels/{_channel_url(group['channel'])}/messages",
            json={"author": "operator", "text": "general update"})
        assert daemon.calls == []


class TestGroupChatUI:
    def _html(self):
        from pathlib import Path
        return (Path(__file__).resolve().parent.parent / "quarterdeck" /
                "static" / "index.html").read_text(encoding="utf-8")

    def test_home_screen_renders_conversation_list(self):
        html = self._html()
        assert 'id="conversations"' in html
        assert "renderConversations" in html
        assert "loadConversations" in html
        assert "conv-avatars" in html
        assert "conv-preview" in html
        assert "conv-unread" in html
        assert "PINNED" in html or "conv-pin" in html

    def test_new_group_chat_picks_agents_with_captain_preselected(self):
        html = self._html()
        assert 'id="group-modal"' in html
        assert 'id="group-name"' in html
        assert 'id="group-roster"' in html
        assert "createGroupChat" in html
        assert 'isCaptainName(a.name) ? "checked"' in html
        assert "Create chat" in html

    def test_mention_autocomplete_and_presence(self):
        html = self._html()
        assert 'id="mention-menu"' in html
        assert "updateMentionMenu" in html
        assert "chooseMention" in html
        assert 'id="chan-members"' in html
        assert 'id="chan-typing"' in html
        assert "captain-badge" in html
        assert "is working" in html or "working\\u2026" in html or "working…" in html

    def test_human_identity_is_locked_to_operator(self):
        html = self._html()
        assert 'const nick = () => "operator";' in html
        assert 'id="nick" class="identity-chip"' in html
        assert ">operator</span>" in html
