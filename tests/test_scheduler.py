"""Scheduler: ticks fire due entries with an injected fake runner (no API keys,
no Docker, no real PinnaceAgent)."""

from __future__ import annotations

import threading
import time

import pytest

from quarterdeck.agent_registry import IDLE, WORKING, AgentRegistry
from quarterdeck.chat import Chat
from quarterdeck.events import EventBus, TICK
from quarterdeck.scheduler import Scheduler, SchedulerError


class FakeRunner:
    """Injectable stand-in for PinnaceAgent. Records wakes."""

    def __init__(self, fail: bool = False):
        self.calls: list[tuple[str, str]] = []
        self.fail = fail
        self._gate = threading.Event()

    def __call__(self, agent, prompt):
        self.calls.append((agent.name, prompt))
        self._gate.set()
        if self.fail:
            raise RuntimeError("model exploded")
        return f"fake summary for {agent.name}"


@pytest.fixture()
def world(tmp_path):
    home = tmp_path / "qd"
    registry = AgentRegistry(home=home)
    chat = Chat(home=home)
    bus = EventBus()
    registry.spawn("scout", "watch things")
    runner = FakeRunner()
    sched = Scheduler(registry, chat, bus=bus, runner=runner, home=home)
    return registry, chat, bus, sched, runner


def _make_due(sched: Scheduler, name: str) -> None:
    sched.add(name, "scout", 3600, "check the horizon, {agent_name}")
    sched._entries[name].last_run = 0.0  # force due


def test_add_validates(world):
    _, _, _, sched, _ = world
    with pytest.raises(SchedulerError):
        sched.add("s1", "ghost", 60, "hi")  # unknown agent
    with pytest.raises(SchedulerError):
        sched.add("s1", "scout", 0, "hi")  # non-positive interval
    sched.add("s1", "scout", 60, "hi")
    with pytest.raises(SchedulerError):
        sched.add("s1", "scout", 60, "hi")  # duplicate


def test_tick_fires_due_entry_and_posts_to_chat(world):
    registry, chat, _, sched, runner = world
    _make_due(sched, "morning-watch")
    fired = sched.tick_once()
    assert fired == ["morning-watch"]
    assert runner._gate.wait(timeout=5), "runner never called"
    time.sleep(0.2)  # let the worker thread post + reset state
    assert runner.calls == [("scout", "check the horizon, scout")]  # template rendered
    msgs = chat.history("#general")
    assert len(msgs) == 1
    assert msgs[0]["author"] == "scout"
    assert "fake summary" in msgs[0]["text"]
    assert registry.get("scout").state == IDLE  # agent sleeps again


def test_tick_skips_not_due(world):
    _, chat, _, sched, runner = world
    sched.add("later", "scout", 3600, "hi")  # last_run = now, not due
    assert sched.tick_once() == []
    assert runner.calls == []
    assert chat.history("#general") == []


def test_runner_error_posts_failure_and_agent_sleeps(world):
    registry, chat, _, sched, _ = world
    sched.runner = FakeRunner(fail=True)
    _make_due(sched, "doomed")
    sched.tick_once()
    time.sleep(0.3)
    msgs = chat.history("#general")
    assert len(msgs) == 1
    assert "run failed" in msgs[0]["text"] and "model exploded" in msgs[0]["text"]
    assert registry.get("scout").state == IDLE


def test_start_stop_loop_publishes_ticks(world):
    registry, chat, bus, sched, runner = world
    ticks = []
    bus.subscribe(TICK, ticks.append)
    sched.add("fast", "scout", 1, "hi")
    sched._entries["fast"].last_run = 0.0
    sched.start()
    assert runner._gate.wait(timeout=5)
    sched.stop()
    assert ticks, "no tick events published while running"
    assert not sched._thread or not sched._thread.is_alive()


def test_schedules_persist_and_retire_drops_them(tmp_path):
    home = tmp_path / "qd"
    registry = AgentRegistry(home=home)
    chat = Chat(home=home)
    registry.spawn("scout", "x")
    s1 = Scheduler(registry, chat, runner=lambda a, p: "ok", home=home)
    s1.add("watch", "scout", 60, "hi")
    s2 = Scheduler(registry, chat, runner=lambda a, p: "ok", home=home)
    assert [e.name for e in s2.list()] == ["watch"]
    assert s2.remove_for_agent("scout") == 1
    assert s2.list() == []
    with pytest.raises(SchedulerError):
        s2.remove("watch")
