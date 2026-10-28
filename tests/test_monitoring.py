"""Channel monitoring: defaults, wake policy, cooldown, and wake history."""

from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient

from quarterdeck import subscriptions
from quarterdeck.agent import AgentRunner
from quarterdeck.agent_registry import AgentRegistry
from quarterdeck.captain import SELF_RUNBOOK, SELF_SYSTEM_PROMPT
from quarterdeck.chat import Chat
from quarterdeck.events import APLOMADO_SCAN_COMPLETED, CHAT_MESSAGE, Event
from quarterdeck.groups import GroupStore
from quarterdeck.monitoring import Monitor
from quarterdeck.web import create_app


class _Wake:
    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def __call__(self, agent, reason):
        self.calls.append((agent, reason))
        return True


def _setup(tmp_path, *, cooldown=60.0):
    home = tmp_path / "qd"
    chat = Chat(home=home)
    registry = AgentRegistry(home=home)
    for name in ("self", "scout", "atlas"):
        registry.spawn(name, system_prompt=name, sandbox="local")
    groups = GroupStore(home=home)
    wake = _Wake()
    monitor = Monitor(
        home=home, chat=chat, registry=registry, groups=groups,
        wake=wake, cooldown=cooldown,
    )
    return home, chat, registry, groups, monitor, wake


def _group(groups, name, members):
    return groups.create(name, members, taken=groups.channels())


def test_captain_monitors_member_channels_and_general(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    _group(groups, "ops", ["self", "scout"])
    _group(groups, "private", ["atlas"])
    subscriptions.subscribe("self", ["#alerts"], home=home)

    self_channels = monitor.monitored_channels("self")
    assert "#general" in self_channels
    assert "#ops" in self_channels
    assert "#alerts" in self_channels
    assert "#private" not in self_channels
    assert monitor.monitored_channels("scout") == ["#ops"]
    assert monitor.monitored_channels("atlas") == ["#private"]


def test_operator_question_wakes_captain_only_in_monitored_channel(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    _group(groups, "ops", ["self", "scout"])

    chat.post("#ops", "operator", "does the scan need a rerun?")
    monitored = chat.history("#ops", n=1)[-1]
    monitor.observe(monitored)
    assert wake.calls == [("self", "monitor:operator-question")]

    chat.post("#random", "operator", "does anyone read this?")
    unmonitored = chat.history("#random", n=1)[-1]
    monitor.observe(unmonitored)
    assert len(wake.calls) == 1


def test_mention_wakes_named_non_captain_even_when_unmonitored(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)

    chat.post("#random", "operator", "@scout please check the logs")
    msg = chat.history("#random", n=1)[-1]
    monitor.observe(msg)
    assert wake.calls == [("scout", "monitor:mention")]


def test_cooldown_batches_a_burst_into_one_wake(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path, cooldown=60.0)
    _group(groups, "ops", ["self", "scout"])

    for i in range(6):
        chat.post("#ops", "operator", f"question {i}?")
        monitor.observe(chat.history("#ops", n=1)[-1])

    assert wake.calls == [("self", "monitor:operator-question")]
    assert len(monitor.wakes(agent="self")) == 1
    record = monitor.wakes(agent="self")[0]
    assert record["channel"] == "#ops"
    assert record["trigger"] == "operator-question"
    assert record["message_id"]


def test_wake_log_is_written_and_exposed_by_api(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    chat.post("#general", "operator", "@self status?")
    msg = chat.history("#general", n=1)[-1]
    monitor.observe(msg)

    record = monitor.wakes()[0]
    assert record["agent"] == "self"
    assert record["channel"] == "#general"
    assert record["trigger"] == "mention"
    assert record["message_id"] == msg["id"]

    client = TestClient(create_app(home=home))
    response = client.get("/api/monitoring/wakes")
    assert response.status_code == 200
    wakes = response.json()["wakes"]
    assert wakes[-1]["message_id"] == msg["id"]
    info = client.get("/api/monitoring/self").json()
    assert "#general" in info["channels"]
    assert info["wakes"][-1]["agent"] == "self"


def test_event_bus_message_wakes_captain(tmp_path):
    from quarterdeck.events import EventBus

    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    bus = EventBus()
    monitor.bus = bus
    monitor.start()
    try:
        chat = Chat(home=home, bus=bus)
        chat.post("#general", "operator", "can you look at this?")
    finally:
        monitor.stop()
    assert wake.calls == [("self", "monitor:operator-question")]


def test_scan_completion_event_wakes_subscribed_captain(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    event = Event(
        type=APLOMADO_SCAN_COMPLETED,
        payload={"channel": "#general", "data": {"summary": "done"}},
    )
    monitor.handle_event(event)
    assert wake.calls == [("self", f"monitor:event:{APLOMADO_SCAN_COMPLETED}")]
    record = monitor.wakes(agent="self")[0]
    assert record["message_id"] == event.id


def test_monitor_wake_context_is_consumed_by_agent_poll(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    chat.post("#general", "operator", "are you watching?")
    monitor.observe(chat.history("#general", n=1)[-1])

    runner = AgentRunner("self", home=home)
    seen: list[str] = []

    async def capture(context: str) -> None:
        seen.append(context)

    runner._act = capture
    asyncio.run(runner._poll())
    assert len(seen) == 1
    assert "MONITORED CHANNEL WAKE" in seen[0]
    assert "are you watching?" in seen[0]


def test_captain_prompt_contains_monitoring_duties():
    prompt = SELF_SYSTEM_PROMPT.lower()
    assert "monitoring duties" in prompt
    assert "monitored channel" in prompt
    assert "stay silent" in prompt or "silence" in prompt
    runbook = SELF_RUNBOOK.lower()
    assert "channel monitoring" in runbook
    assert "bias to silence" in runbook


def test_monitored_set_can_be_overridden(tmp_path):
    home, chat, registry, groups, monitor, wake = _setup(tmp_path)
    assert monitor.set_monitored("scout", ["#alerts"]) == ["#alerts"]
    chat.post("#alerts", "operator", "@scout ping")
    monitor.observe(chat.history("#alerts", n=1)[-1])
    assert wake.calls == [("scout", "monitor:mention")]
    stored = json.loads((home / "monitoring.json").read_text())
    assert stored["agents"]["scout"] == ["#alerts"]
