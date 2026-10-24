"""Wake/read regressions: one reader per cursor, mention delivery, WS parity."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from quarterdeck.agent import AgentRunner
from quarterdeck.chat import Chat
from quarterdeck.web import create_app


def _runner_with_capture(home, name="scout"):
    runner = AgentRunner(name, home=home)
    seen: list[str] = []

    async def capture(context: str) -> None:
        seen.append(context)

    runner._act = capture
    return runner, seen


def test_poll_keeps_mention_and_later_activity_separate(tmp_path):
    home = tmp_path / "qd"
    from quarterdeck import subscriptions

    subscriptions.subscribe("scout", ["#general"], home=home)
    chat = Chat(home=home)
    chat.post("#general", "human", "@scout take a look")
    runner, seen = _runner_with_capture(home)

    asyncio.run(runner._poll())
    assert len(seen) == 1 and "take a look" in seen[0]

    for i in range(5):
        chat.post("#general", "human", f"room activity {i}")
    asyncio.run(runner._poll())
    assert len(seen) == 2
    assert "room activity 4" in seen[1]
    assert "take a look" not in seen[1]


def test_poll_delivers_mention_even_when_unsubscribed(tmp_path):
    home = tmp_path / "qd"
    Chat(home=home).post("#random", "human", "@scout meet me here")
    runner, seen = _runner_with_capture(home)

    asyncio.run(runner._poll())
    assert len(seen) == 1
    assert "meet me here" in seen[0]
    assert "#random" in seen[0]


class _WakeDaemon:
    def __init__(self):
        self.runners = {"scout": object()}
        self.calls: list[tuple[str, str]] = []

    def wake_agent(self, name, reason="dm"):
        self.calls.append((name, reason))
        return True


def test_websocket_and_rest_posts_use_the_same_wake_path(tmp_path):
    daemon = _WakeDaemon()
    client = TestClient(create_app(home=tmp_path, daemon=daemon))

    client.post("/api/channels/%23general/messages",
                json={"author": "human", "text": "@scout via rest"})
    assert daemon.calls[-1] == ("scout", "mention")

    with client.websocket_connect("/ws/%23general") as ws:
        ws.receive_json()  # history
        ws.send_text(json.dumps({
            "type": "post", "author": "human", "text": "@scout via websocket",
        }))
        ws.receive_json()  # echo of the stored message
    assert daemon.calls[-1] == ("scout", "mention")


def test_websocket_activity_wakes_subscribers_like_rest(tmp_path):
    from quarterdeck import subscriptions

    daemon = _WakeDaemon()
    subscriptions.subscribe("scout", ["#general"], home=tmp_path)
    client = TestClient(create_app(home=tmp_path, daemon=daemon))

    with client.websocket_connect("/ws/%23general") as ws:
        ws.receive_json()  # history
        ws.send_text(json.dumps({
            "type": "post", "author": "human", "text": "plain websocket chat",
        }))
        ws.receive_json()  # echo
    assert daemon.calls[-1] == ("scout", "channel-activity")

    client.post("/api/channels/%23general/messages",
                json={"author": "human", "text": "plain rest chat"})
    assert daemon.calls[-1] == ("scout", "channel-activity")
