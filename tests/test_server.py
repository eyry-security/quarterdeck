"""Tests for persistent server mode: health, seed-on-empty, restart state."""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi.testclient import TestClient

from quarterdeck import __version__
from quarterdeck.runtime import Runtime
from quarterdeck.server import (
    DEFAULT_PORT,
    DEFAULT_STATE_DIR,
    resolve_port,
    resolve_state_dir,
)
from quarterdeck.web import create_app


class FakeRunner:
    """Minimal runner so restart tests never touch Docker or an LLM."""

    def __init__(self, name, home=None, daemon=None, allow_local=False):
        self.name = name
        self.home = Path(home)
        self.daemon = daemon
        self.allow_local = allow_local
        self._stop = asyncio.Event()

    def wake_now(self, reason=""):
        pass

    def stop(self):
        self._stop.set()

    async def run(self):
        await self._stop.wait()


class FakeSeed(FakeRunner):
    async def boot_room(self):
        return None


def _runtime(home: Path) -> Runtime:
    return Runtime(home, runner_cls=FakeRunner, seed_cls=FakeSeed)


class TestResolution:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("QD_PORT", raising=False)
        monkeypatch.delenv("QD_STATE_DIR", raising=False)
        monkeypatch.delenv("QUARTERDECK_HOME", raising=False)
        assert resolve_port() == DEFAULT_PORT == 4097
        assert resolve_state_dir() == DEFAULT_STATE_DIR
        assert str(DEFAULT_STATE_DIR) == "/var/lib/quarterdeck"

    def test_env_and_explicit_override(self, monkeypatch):
        monkeypatch.setenv("QD_PORT", "4199")
        monkeypatch.setenv("QD_STATE_DIR", "/tmp/qd-env-state")
        assert resolve_port() == 4199
        assert resolve_state_dir() == Path("/tmp/qd-env-state")
        assert resolve_port(5001) == 5001
        assert resolve_state_dir("/tmp/qd-flag-state") == Path("/tmp/qd-flag-state")

    def test_legacy_quarterdeck_home_fallback(self, monkeypatch):
        monkeypatch.delenv("QD_STATE_DIR", raising=False)
        monkeypatch.setenv("QUARTERDECK_HOME", "/tmp/qd-legacy-state")
        assert resolve_state_dir() == Path("/tmp/qd-legacy-state")


class TestHealth:
    def test_health_shape(self, tmp_path):
        runtime = _runtime(tmp_path / "state")
        runtime.start(wait_ready=5)
        try:
            client = TestClient(create_app(runtime=runtime))
            response = client.get("/health")
            assert response.status_code == 200
            body = response.json()
            assert body == {
                "ok": True,
                "version": __version__,
                "agents": len(runtime.registry.list()),
            }
            assert body["agents"] == 2  # seed + self (captain)
        finally:
            runtime.stop()

    def test_embedded_room_alias_serves_ui(self, tmp_path):
        runtime = _runtime(tmp_path / "state")
        runtime.start(wait_ready=5)
        try:
            client = TestClient(create_app(runtime=runtime))
            response = client.get("/agents/room")
            assert response.status_code == 200
            assert "text/html" in response.headers["content-type"]
        finally:
            runtime.stop()


class TestSeedOnEmpty:
    def test_first_run_creates_seed_and_general(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quarterdeck.agent.llm_available", lambda: False)
        home = tmp_path / "state"
        assert not (home / "agents.json").exists()

        runtime = Runtime(home)
        runtime.start(wait_ready=10)
        try:
            assert runtime.registry.exists("seed")
            assert runtime.registry.exists("self")
            assert "#general" in runtime.chat.channels()
            assert (home / "agents.json").exists()
            assert (home / "chat" / "general.jsonl").exists()
        finally:
            runtime.stop()


class TestPersistenceAcrossRestart:
    def test_registry_rooms_and_messages_survive(self, tmp_path):
        home = tmp_path / "state"

        first = _runtime(home)
        first.start(wait_ready=5)
        try:
            first.registry.spawn("scout", "watch the horizon", sandbox="local")
            first.chat.post("#general", "human", "persist me")
        finally:
            first.stop()

        second = _runtime(home)
        second.start(wait_ready=5)
        try:
            assert second.registry.exists("scout")
            history = second.chat.history("#general", n=10)
            assert any(m["text"] == "persist me" for m in history)
            client = TestClient(create_app(runtime=second))
            assert client.get("/health").json()["agents"] == 3  # seed + self + scout
        finally:
            second.stop()
