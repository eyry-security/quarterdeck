"""The default-spawned captain agent 'self': lifecycle, model, persona."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from quarterdeck.agent import AgentRunner
from quarterdeck.agent_registry import AgentRegistry
from quarterdeck.captain import (
    SELF_IDENTIFY,
    SELF_NAME,
    SELF_RUNBOOK,
    SELF_SYSTEM_PROMPT,
    ensure_captain,
    is_captain,
)
from quarterdeck.daemon import Daemon
from quarterdeck.models import DEFAULT_MODEL, resolve_model
from quarterdeck.runtime import Runtime
from quarterdeck.seed import SeedRunner
from quarterdeck.subscriptions import get_subscriptions


class FakeRunner:
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


def _named(registry: AgentRegistry, name: str) -> list:
    return [agent for agent in registry.list() if agent.name == name]


def _captain_count(registry: AgentRegistry) -> int:
    return len(_named(registry, SELF_NAME))


async def _wait_until(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met before timeout")
        await asyncio.sleep(0.02)


class TestCaptainCreation:
    def test_seed_boot_creates_exactly_one_captain(self, tmp_path):
        seed = SeedRunner("seed", home=tmp_path / "qd")
        asyncio.run(seed.boot_room())
        asyncio.run(seed.boot_room())  # idempotent
        assert _captain_count(seed.registry) == 1
        subs = get_subscriptions(SELF_NAME, home=seed.home)
        assert "#general" in subs["channels"]

    def test_existing_captain_without_model_gets_default(self, tmp_path):
        home = tmp_path / "qd"
        registry = AgentRegistry(home=home)
        registry.spawn(SELF_NAME, "stale", model=None)
        ensure_captain(home=home, registry=registry)
        agent = registry.get(SELF_NAME)
        assert agent.model == DEFAULT_MODEL

    def test_is_captain_is_case_and_space_insensitive(self):
        assert is_captain("self")
        assert is_captain(" SELF ")
        assert not is_captain("seed")
        assert not is_captain(None)

    def test_service_start_creates_captain(self, tmp_path):
        runtime = _runtime(tmp_path / "qd")
        runtime.start(wait_ready=5)
        try:
            assert _captain_count(runtime.registry) == 1
            agent = runtime.registry.get(SELF_NAME)
            assert agent.system_prompt == SELF_SYSTEM_PROMPT
            files = runtime.files
            assert files.read(SELF_NAME, "identify.md") == SELF_IDENTIFY
            assert files.read(SELF_NAME, "runbook.md") == SELF_RUNBOOK
            subs = get_subscriptions(SELF_NAME, home=runtime.home)
            assert "#general" in subs["channels"]
        finally:
            runtime.stop()

    def test_restart_keeps_exactly_one_captain(self, tmp_path):
        home = tmp_path / "qd"

        first = _runtime(home)
        first.start(wait_ready=5)
        try:
            created_at = first.registry.get(SELF_NAME).created_at
            assert _captain_count(first.registry) == 1
        finally:
            first.stop()

        second = _runtime(home)
        second.start(wait_ready=5)
        try:
            assert _captain_count(second.registry) == 1
            assert second.registry.get(SELF_NAME).created_at == created_at
        finally:
            second.stop()

    def test_ensure_is_idempotent_under_repeated_calls(self, tmp_path):
        home = tmp_path / "qd"
        registry = AgentRegistry(home=home)
        ensure_captain(home=home, registry=registry)
        ensure_captain(home=home, registry=registry)
        ensure_captain(home=home, registry=registry)
        assert _captain_count(registry) == 1


class TestCaptainModelAndPersona:
    def test_captain_uses_suite_default_model(self, tmp_path):
        home = tmp_path / "qd"
        ensure_captain(home=home)
        agent = AgentRegistry(home=home).get(SELF_NAME)
        assert resolve_model(agent.model) == DEFAULT_MODEL == "deepseek:deepseek-flash"
        assert "anthropic" not in (agent.model or "").lower()

    def test_persona_prompt_is_present_in_config(self, tmp_path):
        home = tmp_path / "qd"
        ensure_captain(home=home)
        prompt = AgentRegistry(home=home).get(SELF_NAME).system_prompt.lower()
        assert "captain" in prompt
        assert "orchestrator" in prompt
        assert "self" in prompt
        assert "eyry pro" in prompt
        assert "not available yet" in prompt or "not wired up yet" in prompt
        assert "no buzzwords" in prompt

    def test_identity_and_runbook_do_not_overclaim_platform_tools(self):
        for text in (SELF_IDENTIFY, SELF_RUNBOOK):
            lowered = text.lower()
            assert "self" in lowered
            assert "captain" in lowered
            assert "eyry pro" in lowered
            assert "not available yet" in lowered or "not wired up yet" in lowered

    def test_first_boot_briefs_captain_only_once(self, tmp_path):
        home = tmp_path / "qd"
        wakeup = home / "agents" / SELF_NAME / "wakeup.json"
        ensure_captain(home=home)
        assert wakeup.exists()
        briefing = wakeup.read_text(encoding="utf-8").lower()
        assert "self, the captain" in briefing
        assert "platform scan tools" in briefing
        wakeup.unlink()
        ensure_captain(home=home)  # restart: no second briefing
        assert not wakeup.exists()

    def test_captain_has_delegation_tools(self, tmp_path):
        runner = AgentRunner(SELF_NAME, home=tmp_path / "qd")
        names = {tool.name for tool in runner.all_tools()}
        for expected in ("quarterdeck_register_agent", "quarterdeck_wake",
                         "quarterdeck_dm", "quarterdeck_list"):
            assert expected in names

    def test_regular_agents_do_not_get_captain_management_tools(self, tmp_path):
        runner = AgentRunner("scout", home=tmp_path / "qd")
        names = {tool.name for tool in runner.all_tools()}
        assert "quarterdeck_register_agent" not in names
        assert "quarterdeck_wake" not in names


class TestCaptainRespawn:
    def test_stopped_captain_is_respawned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quarterdeck.daemon.SUPERVISE_INTERVAL", 0.05)
        daemon = Daemon(home=tmp_path / "qd",
                        runner_cls=FakeRunner, seed_cls=FakeSeed)

        async def main():
            boot = asyncio.create_task(daemon.run())
            try:
                await _wait_until(lambda: SELF_NAME in daemon.tasks)
                first_task = daemon.tasks[SELF_NAME]
                daemon.runners[SELF_NAME].stop()  # graceful kill
                await _wait_until(
                    lambda: daemon.tasks[SELF_NAME] is not first_task
                    and not daemon.tasks[SELF_NAME].done()
                )
                assert daemon.registry.exists(SELF_NAME)
            finally:
                daemon.stop()
                await asyncio.wait_for(boot, timeout=10)

        asyncio.run(main())

    def test_cancelled_captain_is_respawned(self, tmp_path, monkeypatch):
        monkeypatch.setattr("quarterdeck.daemon.SUPERVISE_INTERVAL", 0.05)
        daemon = Daemon(home=tmp_path / "qd",
                        runner_cls=FakeRunner, seed_cls=FakeSeed)

        async def main():
            boot = asyncio.create_task(daemon.run())
            try:
                await _wait_until(lambda: SELF_NAME in daemon.tasks)
                first_task = daemon.tasks[SELF_NAME]
                first_task.cancel()
                try:
                    await first_task
                except asyncio.CancelledError:
                    pass
                await _wait_until(
                    lambda: daemon.tasks[SELF_NAME] is not first_task
                    and not daemon.tasks[SELF_NAME].done()
                )
            finally:
                daemon.stop()
                await asyncio.wait_for(boot, timeout=10)

        asyncio.run(main())
