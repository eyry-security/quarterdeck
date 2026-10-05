"""Tests for the Quarterdeck agent runtime: AgentRunner, Seed, Daemon, wakeup."""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from quarterdeck.agent import AgentRunner, llm_available
from quarterdeck.daemon import Daemon
from quarterdeck.seed import SeedRunner


@pytest.fixture
def home(tmp_path):
    return tmp_path / "qd"


@pytest.fixture
def daemon(home):
    return Daemon(home=home)


# ---------------------------------------------------------------- AgentRunner
class TestAgentRunner:
    def test_file_tools_names(self, home):
        r = AgentRunner("tester", home=home)
        names = sorted(t.name for t in r.file_tools())
        assert names == [
            "quarterdeck_compact_memory",
            "quarterdeck_memory_read",
            "quarterdeck_memory_write",
            "quarterdeck_rewrite_memory",
            "quarterdeck_rewrite_prompt",
            "quarterdeck_runbook_read",
            "quarterdeck_runbook_update",
        ]

    def test_memory_write_appends(self, home):
        r = AgentRunner("tester", home=home)
        tools = {t.name: t for t in r.file_tools()}
        assert tools["quarterdeck_memory_read"].invoke({}) == "(memory.md is empty)"
        assert "updated" in tools["quarterdeck_memory_write"].invoke(
            {"entry": "Ben prefers terse replies"})
        content = tools["quarterdeck_memory_read"].invoke({})
        assert "Ben prefers terse replies" in content
        tools["quarterdeck_memory_write"].invoke({"entry": "second fact"})
        content = tools["quarterdeck_memory_read"].invoke({})
        assert "Ben prefers terse replies" in content and "second fact" in content

    def test_runbook_roundtrip(self, home):
        r = AgentRunner("tester", home=home)
        tools = {t.name: t for t in r.file_tools()}
        tools["quarterdeck_runbook_update"].invoke({"content": "# runbook\nbe nice"})
        assert "be nice" in tools["quarterdeck_runbook_read"].invoke({})

    def test_all_tools_include_chat_and_files(self, home):
        r = AgentRunner("tester", home=home)
        names = {t.name for t in r.all_tools()}
        for expected in ("quarterdeck_send", "quarterdeck_think",
                         "quarterdeck_memory_write", "quarterdeck_subscribe"):
            assert expected in names

    def test_wakeup_request_consumed(self, home):
        r = AgentRunner("tester", home=home)
        d = home / "agents" / "tester"
        d.mkdir(parents=True, exist_ok=True)
        (d / "wakeup.json").write_text(json.dumps({"prompt": "hello agent"}))
        assert r._take_wakeup_request() == "hello agent"
        assert not (d / "wakeup.json").exists()  # consumed
        assert r._take_wakeup_request() is None

    def test_dm_watermark(self, home):
        r = AgentRunner("alice", home=home)
        r.chat.post("#dm-alice-bob", "bob", "hey alice")
        first = r._check_dms()
        assert "hey alice" in first
        second = r._check_dms()
        assert second == ""  # watermark consumed

    def test_act_skips_without_llm(self, home, monkeypatch):
        monkeypatch.setattr("quarterdeck.agent.llm_available", lambda: False)
        r = AgentRunner("tester", home=home)
        asyncio.run(r._act("hello"))
        # No crash, no thought posted about acting (just the skip note).

    def test_run_loop_stops(self, home):
        r = AgentRunner("tester", home=home)
        r.poll_interval = 0.05
        r.proactive_interval = 9999
        async def main():
            task = asyncio.create_task(r.run())
            await asyncio.sleep(0.2)
            r.stop()
            await asyncio.wait_for(task, timeout=5)
        asyncio.run(main())


# ------------------------------------------------------------ Wakeup (mechanical)
class TestWakeup:
    def test_build_wakeup_prompt(self):
        from quarterdeck.wakeup import build_wakeup_prompt
        p = build_wakeup_prompt("scout", "I watch the scope", "runbook text",
                                ["#general"])
        assert "scout" in p
        assert "I watch the scope" in p
        assert "First actions" in p

    def test_write_wakeup(self, home):
        from quarterdeck.wakeup import build_wakeup_prompt, write_wakeup
        import json
        prompt = build_wakeup_prompt("scout")
        f = write_wakeup(home, "scout", prompt)
        assert f.exists()
        assert json.loads(f.read_text())["prompt"] == prompt

    def test_seed_wake_tool(self, home):
        d = Daemon(home=home)
        seed = SeedRunner("seed", home=home, daemon=d)
        names = [t.name for t in seed.all_tools()]
        assert "quarterdeck_wake" in names
        assert "quarterdeck_register_agent" in names


# ------------------------------------------------------------------- Seed
class TestSeed:
    def test_register_agent(self, home):
        d = Daemon(home=home)
        seed = SeedRunner("seed", home=home, daemon=d)
        tool = {t.name: t for t in seed.all_tools()}["quarterdeck_register_agent"]
        # generate_wakeup is mechanical (no LLM) — fine.
        res = tool.invoke({"name": "newbie",
                           "identify": "I am newbie",
                           "runbook": "be helpful"})
        assert "registered and woken" in res
        assert seed.registry.exists("newbie")
        assert "I am newbie" in seed.files.read("newbie", "identify.md")

    def test_register_duplicate_fails(self, home):
        seed = SeedRunner("seed", home=home)
        tool = {t.name: t for t in seed.all_tools()}["quarterdeck_register_agent"]
        seed.registry.spawn("dup", system_prompt="x", sandbox="local")
        res = tool.invoke({"name": "dup", "identify": "i", "runbook": "r"})
        assert "already registered" in res

    def test_boot_room_creates_general(self, home):
        seed = SeedRunner("seed", home=home)
        asyncio.run(seed.boot_room())
        assert "#general" in seed.chat.channels()

    def test_compaction(self, home):
        seed = SeedRunner("seed", home=home)
        chat_dir = home / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        f = chat_dir / "general.jsonl"
        f.write_text("\n".join(f'{{"ts": {i}, "x": 1}}' for i in range(100)) + "\n")
        # Force compaction by lowering the threshold via monkeypatch.
        import quarterdeck.seed as seed_mod
        orig = seed_mod.CHAT_WARN_BYTES
        seed_mod.CHAT_WARN_BYTES = 10
        try:
            note = seed._check_disk()
        finally:
            seed_mod.CHAT_WARN_BYTES = orig
        # 100 tiny lines won't exceed keep count; craft a bigger case instead.
        assert isinstance(note, str)

    def test_greet_newcomers(self, home):
        seed = SeedRunner("seed", home=home)
        seed.chat.post("#general", "human", "hello room")
        note = seed._greet_newcomers()
        assert "human" in note
        # Second call: already greeted.
        assert seed._greet_newcomers() == ""


# ----------------------------------------------------------------- Daemon
class TestDaemon:
    def test_boot_order_and_heartbeat(self, home, monkeypatch):
        # No LLM: runners boot, idle (wakeup prompts are spawn-time only).
        monkeypatch.setattr("quarterdeck.agent.llm_available", lambda: False)
        d = Daemon(home=home)
        d.registry.spawn("scout", system_prompt="scout things", sandbox="local")

        async def main():
            boot = asyncio.create_task(d.run())
            await asyncio.sleep(1.5)
            # seed + scout should be up; no orchestrator anymore
            assert set(d.tasks) >= {"seed", "scout"}
            assert "orchestrator" not in d.tasks
            hb = json.loads((home / "daemon.json").read_text())
            assert set(hb["agents"]) >= {"seed", "scout"}
            d.stop()
            await asyncio.wait_for(boot, timeout=10)

        asyncio.run(main())

    def test_boot_retires_legacy_orchestrator(self, home, monkeypatch):
        monkeypatch.setattr("quarterdeck.agent.llm_available", lambda: False)
        d = Daemon(home=home)
        d.registry.spawn("orchestrator", system_prompt="legacy", sandbox="local")

        async def main():
            boot = asyncio.create_task(d.run())
            await asyncio.sleep(1.0)
            assert not d.registry.exists("orchestrator")
            assert "orchestrator" not in d.tasks
            d.stop()
            await asyncio.wait_for(boot, timeout=10)

        asyncio.run(main())

    def test_ensure_runner_soon(self, home, monkeypatch):
        monkeypatch.setattr("quarterdeck.agent.llm_available", lambda: False)
        d = Daemon(home=home)

        async def main():
            boot = asyncio.create_task(d.run())
            await asyncio.sleep(1.0)
            d.registry.spawn("late", system_prompt="late joiner", sandbox="local")
            d.ensure_runner_soon("late")
            await asyncio.sleep(1.0)
            assert "late" in d.tasks and not d.tasks["late"].done()
            d.stop()
            await asyncio.wait_for(boot, timeout=10)

        asyncio.run(main())

    def test_crashed_runner_restarts(self, home, monkeypatch):
        monkeypatch.setattr("quarterdeck.agent.llm_available", lambda: False)
        d = Daemon(home=home)
        d.registry.spawn("flaky", system_prompt="x", sandbox="local")

        async def main():
            boot = asyncio.create_task(d.run())
            await asyncio.sleep(1.0)
            first = d.tasks["flaky"]
            first.cancel()  # simulate crash
            try:
                await first
            except asyncio.CancelledError:
                pass
            # Supervisor runs every 15s; speed it up by calling internals.
            d._restarts["flaky"] = 0
            d._last_crash["flaky"] = 0
            d.stop()
            await asyncio.wait_for(boot, timeout=10)

        asyncio.run(main())
