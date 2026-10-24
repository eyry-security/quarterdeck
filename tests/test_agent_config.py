"""Registry config is honored on every wake path; sandboxing fails closed."""

from __future__ import annotations

from types import SimpleNamespace

import pinnace
import pytest

from quarterdeck.agent import AgentRunner
from quarterdeck.agent_config import (
    SandboxUnavailable,
    build_sandbox,
    session_id_for,
)
from quarterdeck.agent_registry import Agent, AgentRegistry
from quarterdeck.scheduler import default_runner


class FakeSandbox:
    def __init__(self, *args, **kwargs):
        self.closed = False

    def close(self):
        self.closed = True


def _fake_pinnace(monkeypatch, captured):
    class Config:
        @classmethod
        def resolve(cls, **values):
            captured.update(values)
            return values

    class PinnaceAgent:
        @classmethod
        def from_config(cls, config):
            captured["built_config"] = config
            return cls()

        def run(self, prompt):
            return SimpleNamespace(
                final="done", structured=None, turns=1,
            )

    class SessionStore:
        def __init__(self, root):
            captured["session_root"] = root

    monkeypatch.setattr(pinnace, "AgentConfig", Config)
    monkeypatch.setattr(pinnace, "PinnaceAgent", PinnaceAgent)
    monkeypatch.setattr(pinnace, "SessionStore", SessionStore)


def test_scheduler_runner_honors_registry_and_shared_session(tmp_path, monkeypatch):
    captured = {}
    _fake_pinnace(monkeypatch, captured)
    monkeypatch.setattr(pinnace, "DockerSandbox", FakeSandbox)
    agent = Agent("scout", "registry prompt", model=None, sandbox="docker",
                  max_turns=7)

    summary = default_runner(agent, "go", home=tmp_path)

    assert "done" in summary
    assert captured["session_id"] == session_id_for(agent)
    assert captured["session_id"].startswith("agent-")
    assert captured["max_turns"] == 7
    assert captured["system_prompt"] == "registry prompt"
    assert captured["agent_id"] == "scout"


def test_daemon_runner_honors_registry_and_shared_session(tmp_path, monkeypatch):
    home = tmp_path / "qd"
    AgentRegistry(home=home).spawn(
        "scout", "registry prompt", sandbox="local", max_turns=9
    )
    captured = {}
    _fake_pinnace(monkeypatch, captured)
    runner = AgentRunner("scout", home=home, allow_local=True)
    fake = FakeSandbox()
    monkeypatch.setattr(runner, "sandbox", lambda: fake)

    runner.build_pinnace()

    assert captured["session_id"] == session_id_for("scout")
    assert captured["max_turns"] == 9
    assert captured["system_prompt"].count("registry prompt") == 1
    assert captured["agent_id"] == "scout"


def test_docker_failure_refuses_instead_of_using_local(tmp_path, monkeypatch):
    def down(*args, **kwargs):
        raise RuntimeError("docker daemon not reachable")

    monkeypatch.setattr(pinnace, "DockerSandbox", down)
    monkeypatch.setattr(
        pinnace, "LocalSandbox",
        lambda *a, **k: pytest.fail("unsafe local fallback was used"),
    )
    agent = Agent("scout", "watch", sandbox="docker")

    with pytest.raises(SandboxUnavailable, match="Refusing to start"):
        default_runner(agent, "go", home=tmp_path)


def test_daemon_sandbox_fails_closed_without_local_fallback(tmp_path, monkeypatch):
    import quarterdeck.persistent_sandbox as persistent

    home = tmp_path / "qd"
    AgentRegistry(home=home).spawn("scout", "watch", sandbox="docker")

    def down(*args, **kwargs):
        raise RuntimeError("image qd-agent:latest missing")

    monkeypatch.setattr(persistent, "PersistentDockerSandbox", down)
    monkeypatch.setattr(
        pinnace, "LocalSandbox",
        lambda *a, **k: pytest.fail("unsafe local fallback was used"),
    )
    runner = AgentRunner("scout", home=home)

    with pytest.raises(SandboxUnavailable, match="Refusing to start"):
        runner.sandbox()


def test_local_sandbox_requires_explicit_development_flag(tmp_path, monkeypatch):
    monkeypatch.delenv("QUARTERDECK_ALLOW_LOCAL", raising=False)
    agent = Agent("scout", "watch", sandbox="local")
    with pytest.raises(SandboxUnavailable, match="disabled"):
        build_sandbox(agent, home=tmp_path)

    monkeypatch.setattr(pinnace, "LocalSandbox", FakeSandbox)
    assert isinstance(build_sandbox(agent, home=tmp_path, allow_local=True),
                      FakeSandbox)

    monkeypatch.setenv("QUARTERDECK_ALLOW_LOCAL", "1")
    assert isinstance(build_sandbox(agent, home=tmp_path), FakeSandbox)


def test_explicit_development_flag_allows_docker_failure_fallback(tmp_path,
                                                                  monkeypatch):
    monkeypatch.delenv("QUARTERDECK_ALLOW_LOCAL", raising=False)

    def down(*args, **kwargs):
        raise RuntimeError("docker daemon not reachable")

    monkeypatch.setattr(pinnace, "DockerSandbox", down)
    monkeypatch.setattr(pinnace, "LocalSandbox", FakeSandbox)
    agent = Agent("scout", "watch", sandbox="docker")

    assert isinstance(build_sandbox(agent, home=tmp_path, allow_local=True),
                      FakeSandbox)
