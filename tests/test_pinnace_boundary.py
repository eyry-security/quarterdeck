"""Approved Pinnace API usage and sandbox cleanup."""

from types import SimpleNamespace

import pytest

import pinnace
from quarterdeck.agent_registry import Agent
from quarterdeck.scheduler import default_runner


class FakeSandbox:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def test_default_runner_uses_config_session_root_and_closes(tmp_path, monkeypatch):
    sandbox = FakeSandbox()
    captured = {}

    class Config:
        @classmethod
        def resolve(cls, **values):
            captured.update(values)
            return values

    class PinnaceAgent:
        @classmethod
        def from_config(cls, config):
            assert config["model"] is None
            assert config["session_id"].startswith("agent-")
            assert config["session_id"] != "agent-scout"
            return cls()

        def run(self, prompt):
            return SimpleNamespace(
                final="done", structured={"ok": True}, turns=2,
            )

    class SessionStore:
        def __init__(self, root):
            captured["session_root"] = root

    monkeypatch.setattr(pinnace, "DockerSandbox", lambda: sandbox)
    monkeypatch.setattr(pinnace, "AgentConfig", Config)
    monkeypatch.setattr(pinnace, "PinnaceAgent", PinnaceAgent)
    monkeypatch.setattr(pinnace, "SessionStore", SessionStore)

    agent = Agent("scout", "watch", model=None, sandbox="docker")
    summary = default_runner(agent, "check", home=tmp_path)
    assert "2 turn(s)" in summary and "done" in summary
    assert captured["model"] is None
    assert captured["session_id"].startswith("agent-")
    assert captured["session_id"] != "agent-scout"
    assert captured["session_root"] == tmp_path / "pinnace"
    assert sandbox.closed


def test_default_runner_closes_when_agent_construction_fails(tmp_path, monkeypatch):
    sandbox = FakeSandbox()

    class Config:
        @classmethod
        def resolve(cls, **values):
            return values

    class BrokenAgent:
        @classmethod
        def from_config(cls, config):
            raise RuntimeError("construction failed")

    monkeypatch.setattr(pinnace, "DockerSandbox", lambda: sandbox)
    monkeypatch.setattr(pinnace, "AgentConfig", Config)
    monkeypatch.setattr(pinnace, "PinnaceAgent", BrokenAgent)
    monkeypatch.setattr(pinnace, "SessionStore", lambda root: object())

    with pytest.raises(RuntimeError, match="construction failed"):
        default_runner(Agent("scout", "watch"), "check", home=tmp_path)
    assert sandbox.closed


def test_unattended_local_runner_requires_explicit_permission(tmp_path):
    agent = Agent("scout", "watch", sandbox="local")
    with pytest.raises(RuntimeError, match="disabled"):
        default_runner(agent, "check", home=tmp_path)
