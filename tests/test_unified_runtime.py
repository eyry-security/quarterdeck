"""End-to-end wake path through the one supervised runtime."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient

from quarterdeck.agent_registry import AgentRegistry
from quarterdeck.chat import Chat
from quarterdeck.runtime import Runtime
from quarterdeck.web import create_app


class FakeRunner:
    """Minimal runner: a wake performs one deterministic 'agent turn'."""

    def __init__(self, name, home=None, daemon=None, allow_local=False):
        self.name = name
        self.home = Path(home)
        self.daemon = daemon
        self.allow_local = allow_local
        self.chat = Chat(home=self.home)
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._wake_reason = ""
        self._thinking = False
        self.turns = 0

    def wake_now(self, reason=""):
        self._wake_reason = reason or "wake"
        self._wake.set()

    def stop(self):
        self._stop.set()

    async def run(self):
        while not self._stop.is_set():
            stop_task = asyncio.create_task(self._stop.wait())
            wake_task = asyncio.create_task(self._wake.wait())
            try:
                await asyncio.wait(
                    {stop_task, wake_task}, timeout=5,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            finally:
                for task in (stop_task, wake_task):
                    if not task.done():
                        task.cancel()
            if self._wake.is_set():
                self._wake.clear()
                if self.name == "scout":
                    self._thinking = True
                    self.turns += 1
                    self.chat.post("#general", "scout",
                                   f"turn complete ({self._wake_reason})")
                    self._thinking = False


class FakeSeed(FakeRunner):
    async def boot_room(self):
        return None


def test_websocket_message_wakes_agent_and_chat_output_flows_back(tmp_path):
    home = tmp_path / "qd"
    AgentRegistry(home=home).spawn("scout", "watch the room", sandbox="local")
    runtime = Runtime(home, allow_local=True,
                      runner_cls=FakeRunner, seed_cls=FakeSeed)
    runtime.start(wait_ready=5)
    try:
        client = TestClient(create_app(runtime=runtime))
        with client.websocket_connect("/ws/%23general") as ws:
            ws.receive_json()  # history
            ws.send_text(json.dumps({
                "type": "post", "author": "human", "text": "@scout status?",
            }))
            seen = []
            for _ in range(12):
                msg = ws.receive_json()
                seen.append(msg)
                if msg.get("author") == "scout" and "turn complete" in msg.get("text", ""):
                    break
        assert any(m.get("text") == "@scout status?" for m in seen)
        agent_msgs = [m for m in seen if m.get("author") == "scout"]
        assert agent_msgs, seen
        assert "mention" in agent_msgs[-1]["text"]
        assert runtime.daemon.runners["scout"].turns == 1
    finally:
        runtime.stop()


def test_runtime_components_share_one_event_bus_and_chat(tmp_path):
    runtime = Runtime(tmp_path / "qd")
    assert runtime.chat.bus is runtime.bus
    assert runtime.rules.bus is runtime.bus
    assert runtime.chatops.bus is runtime.bus
    assert runtime.scheduler.bus is runtime.bus
