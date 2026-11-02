"""LR-47: qd-agent containers have caps and are removed at task end."""

from __future__ import annotations

import sys
import types

from quarterdeck.agent import AgentRunner
from quarterdeck.persistent_sandbox import PersistentDockerSandbox


class FakeContainer:
    def __init__(self):
        self.removed = False

    def remove(self, force=False):
        self.removed = force


class FakeContainers:
    def __init__(self):
        self.calls = []
        self.container = FakeContainer()

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return self.container


class FakeClient:
    def __init__(self):
        self.containers = FakeContainers()

    def ping(self):
        return True


def _fake_docker(monkeypatch, client):
    monkeypatch.setitem(
        sys.modules, "docker", types.SimpleNamespace(from_env=lambda: client))


def test_persistent_sandbox_sets_caps_and_close_removes_container(
        tmp_path, monkeypatch):
    client = FakeClient()
    _fake_docker(monkeypatch, client)
    monkeypatch.setenv("QD_AGENT_MEMORY", "768m")
    monkeypatch.setenv("QD_AGENT_CPUS", "0.5")

    sandbox = PersistentDockerSandbox("scout", home=tmp_path)
    call = client.containers.calls[0]
    assert call["mem_limit"] == "768m"
    assert call["nano_cpus"] == 500_000_000
    assert str(tmp_path / "agents" / "scout" / "work") in call["volumes"]

    sandbox.close()
    assert client.containers.container.removed is True


def test_agent_runner_releases_its_task_sandbox():
    class FakeSandbox:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    runner = AgentRunner("scout")
    sandbox = FakeSandbox()
    runner._sandbox = sandbox
    runner._sandbox_kind = "docker"

    runner.release_sandbox()

    assert sandbox.closed is True
    assert runner._sandbox is None


def test_task_end_closes_sandbox_on_success_and_failure(tmp_path, monkeypatch):
    import asyncio
    from types import SimpleNamespace

    class FakeAgent:
        def __init__(self, fail=False):
            self.fail = fail

        def run(self, prompt):
            if self.fail:
                raise RuntimeError("task failed")
            return SimpleNamespace(
                usage=[], final="done", structured=None, turns=1)

    class TaskSandbox:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    for fail in (False, True):
        runner = AgentRunner("scout", home=tmp_path / f"case-{fail}")
        sandbox = TaskSandbox()
        runner._sandbox = sandbox
        runner._sandbox_kind = "docker"
        monkeypatch.setattr(
            runner, "build_pinnace", lambda fail=fail: FakeAgent(fail))

        asyncio.run(runner._act_inner("do the task"))

        assert sandbox.closed is True
        assert runner._sandbox is None
