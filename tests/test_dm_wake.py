"""Tests for instant DM wake, cid echo, and cross-thread post fanout."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from quarterdeck.agent import AgentRunner
from quarterdeck.chat import Chat, add_post_listener, remove_post_listener
from quarterdeck.daemon import Daemon


@pytest.fixture
def home(tmp_path):
    return tmp_path / "qd"


class TestCid:
    def test_post_stores_cid(self, home):
        chat = Chat(home=home)
        msg = chat.post("#general", "human", "hello", cid="abc-123")
        assert msg["cid"] == "abc-123"
        hist = chat.history("#general")
        assert hist[-1]["cid"] == "abc-123"

    def test_post_without_cid_has_no_cid_key(self, home):
        chat = Chat(home=home)
        msg = chat.post("#general", "human", "hello")
        assert "cid" not in msg


class TestPostListeners:
    def test_listener_fires_on_post(self, home):
        seen = []
        def on_post(ch, m):
            seen.append((ch, m))
        add_post_listener(on_post)
        try:
            Chat(home=home).post("#general", "human", "hi")
        finally:
            remove_post_listener(on_post)
        assert len(seen) == 1
        assert seen[0][0] == "#general"
        assert seen[0][1]["text"] == "hi"

    def test_listener_fires_across_chat_instances(self, home):
        # Simulates the daemon thread writing with its own Chat instance.
        seen = []
        def on_post(ch, m):
            seen.append((ch, m))
        add_post_listener(on_post)
        try:
            Chat(home=home).post("#thoughts-scout", "scout", "thinking...")
        finally:
            remove_post_listener(on_post)
        assert len(seen) == 1
        assert seen[0][0] == "#thoughts-scout"

    def test_listener_exception_does_not_break_post(self, home):
        def bad(ch, m):
            raise RuntimeError("boom")
        add_post_listener(bad)
        try:
            msg = Chat(home=home).post("#general", "human", "hi")
        finally:
            remove_post_listener(bad)
        assert msg["text"] == "hi"


class TestWakeNow:
    def test_wake_now_sets_event(self, home):
        r = AgentRunner("scout", home=home)
        assert not r._wake_event.is_set()
        r.wake_now("dm")
        assert r._wake_event.is_set()
        assert r._wake_reason == "dm"

    def test_wake_now_default_reason(self, home):
        r = AgentRunner("scout", home=home)
        r.wake_now()
        assert r._wake_reason == "wake"


class TestDaemonWake:
    def test_wake_unknown_agent_false(self, home):
        d = Daemon(home=home)
        assert d.wake_agent("nope", "dm") is False

    def test_wake_without_loop_false(self, home):
        d = Daemon(home=home)
        # register a fake runner but no running loop
        r = AgentRunner("scout", home=home)
        d.runners["scout"] = r
        assert d.wake_agent("scout", "dm") is False

    def test_wake_agent_signals_runner(self, home):
        async def go():
            d = Daemon(home=home)
            d._loop = asyncio.get_running_loop()  # normally set in daemon.run()
            await d._start_runner("scout", AgentRunner)
            real = d.runners["scout"]
            assert d.wake_agent("scout", "dm") is True
            await asyncio.sleep(0.05)  # let call_soon_threadsafe fire
            # The runner consumes + clears the event when it wakes; the
            # reason persists, proving wake_now ran.
            assert real._wake_reason == "dm"
            # cleanup: stop the runner task
            for t in list(d.tasks.values()):
                t.cancel()
        asyncio.run(go())


class TestDmPollPriority:
    def test_check_dms_finds_new_dm(self, home):
        chat = Chat(home=home)
        chat.post("#dm-human-scout", "human", "hey scout")
        r = AgentRunner("scout", home=home)
        out = r._check_dms()
        assert "hey scout" in out
        assert "#dm-human-scout" in out
        # second poll: watermark consumed, nothing new
        assert r._check_dms() == ""

    def test_check_dms_ignores_own_messages(self, home):
        chat = Chat(home=home)
        chat.post("#dm-human-scout", "scout", "my own reply")
        r = AgentRunner("scout", home=home)
        assert r._check_dms() == ""

    def test_check_dms_ignores_other_agent_dms(self, home):
        chat = Chat(home=home)
        chat.post("#dm-human-scout2", "human", "not for scout")
        r = AgentRunner("scout", home=home)
        assert r._check_dms() == ""


class TestModelSwitcher:
    def test_registry_set_model(self, home):
        from quarterdeck.agent_registry import AgentRegistry
        reg = AgentRegistry(home=home)
        reg.spawn("scout", system_prompt="x")
        a = reg.set_model("scout", "anthropic:claude-haiku-4-5")
        assert a.model == "anthropic:claude-haiku-4-5"
        assert reg.get("scout").model == "anthropic:claude-haiku-4-5"
        # reset to default
        reg.set_model("scout", "")
        assert reg.get("scout").model is None

    def test_registry_set_model_unknown_agent(self, home):
        from quarterdeck.agent_registry import AgentRegistry, RegistryError
        reg = AgentRegistry(home=home)
        with pytest.raises(RegistryError):
            reg.set_model("ghost", "anthropic:claude-haiku-4-5")

    def test_patch_agent_model_api(self, home):
        from fastapi.testclient import TestClient
        from quarterdeck.agent_registry import AgentRegistry
        from quarterdeck.web import create_app
        AgentRegistry(home=home).spawn("scout", system_prompt="x")
        client = TestClient(create_app(home=home))
        r = client.patch("/api/agents/scout", json={"model": "anthropic:claude-haiku-4-5"})
        assert r.status_code == 200
        assert r.json()["model"] == "anthropic:claude-haiku-4-5"
        r = client.patch("/api/agents/ghost", json={"model": "x"})
        assert r.status_code == 404

    def test_list_models_api(self, home):
        from fastapi.testclient import TestClient
        from quarterdeck.web import create_app
        client = TestClient(create_app(home=home))
        r = client.get("/api/models")
        assert r.status_code == 200
        models = r.json()["models"]
        assert "anthropic:claude-opus-4-6" in models

    def test_runner_rebuilds_on_model_change(self, home):
        from quarterdeck.agent_registry import AgentRegistry
        reg = AgentRegistry(home=home)
        reg.spawn("scout", system_prompt="x", model="anthropic:claude-haiku-4-5")
        r = AgentRunner("scout", home=home)
        first = r.build_pinnace()
        assert r._pinnace_model == "anthropic:claude-haiku-4-5"
        assert r.build_pinnace() is first  # cached
        reg.set_model("scout", "anthropic:claude-opus-4-6")
        second = r.build_pinnace()
        assert second is not first  # rebuilt
        assert r._pinnace_model == "anthropic:claude-opus-4-6"

    def test_set_model_tool_present(self, home):
        from quarterdeck.tools import quarterdeck_tools
        names = [t.name for t in quarterdeck_tools("scout", home=home)]
        assert "quarterdeck_set_model" in names
        assert "quarterdeck_set_agent_model" not in names  # seed only

    def test_seed_gets_set_agent_model_tool(self, home):
        from quarterdeck.tools import quarterdeck_tools
        names = [t.name for t in quarterdeck_tools("seed", home=home)]
        assert "quarterdeck_set_agent_model" in names

    def test_set_model_tool_roundtrip(self, home):
        from quarterdeck.agent_registry import AgentRegistry
        from quarterdeck.tools import quarterdeck_tools
        AgentRegistry(home=home).spawn("scout", system_prompt="x")
        tools = {t.name: t for t in quarterdeck_tools("scout", home=home)}
        out = tools["quarterdeck_set_model"].invoke({"model": "anthropic:claude-haiku-4-5"})
        assert "haiku" in out
        assert AgentRegistry(home=home).get("scout").model == "anthropic:claude-haiku-4-5"


class TestChannelActivity:
    def test_check_activity_returns_new_messages(self, home):
        from quarterdeck import subscriptions
        chat = Chat(home=home)
        subscriptions.subscribe("scout", ["#general"], home=home)
        chat.post("#general", "human", "hello room")
        out = subscriptions.check_activity("scout", chat, home=home)
        assert out["count"] == 1
        assert out["messages"][0]["text"] == "hello room"
        # watermark consumed
        out2 = subscriptions.check_activity("scout", chat, home=home)
        assert out2["count"] == 0

    def test_check_activity_ignores_own_messages(self, home):
        from quarterdeck import subscriptions
        chat = Chat(home=home)
        subscriptions.subscribe("scout", ["#general"], home=home)
        chat.post("#general", "scout", "my own note")
        out = subscriptions.check_activity("scout", chat, home=home)
        assert out["count"] == 0

    def test_obs_ready_batching(self, home):
        import time as _time
        r = AgentRunner("scout", home=home)
        assert r._obs_ready() is False
        now = _time.time()
        r._pending_obs = [{"channel": "#general", "author": "human",
                           "text": f"m{i}", "ts": now} for i in range(4)]
        assert r._obs_ready() is False  # under batch max, fresh
        r._pending_obs.append({"channel": "#general", "author": "human",
                               "text": "m4", "ts": now})
        assert r._obs_ready() is True  # batch max hit
        r._pending_obs = [{"channel": "#general", "author": "human",
                           "text": "old", "ts": now - 200}]
        assert r._obs_ready() is True  # max age hit

    def test_check_activity_buffers(self, home):
        from quarterdeck import subscriptions
        chat = Chat(home=home)
        subscriptions.subscribe("scout", ["#general"], home=home)
        chat.post("#general", "human", "one")
        chat.post("#general", "human", "two")
        r = AgentRunner("scout", home=home)
        assert r._check_activity() == 2
        assert len(r._pending_obs) == 2
        assert r._check_activity() == 0  # watermark consumed


class TestSubEndpoints:
    def test_sub_toggle_roundtrip(self, home):
        from fastapi.testclient import TestClient
        from quarterdeck.agent_registry import AgentRegistry
        from quarterdeck.web import create_app
        AgentRegistry(home=home).spawn("scout", system_prompt="x")
        client = TestClient(create_app(home=home))
        r = client.get("/api/agents/scout/subscriptions")
        assert r.status_code == 200
        assert r.json()["channels"] == {}
        r = client.post("/api/agents/scout/subscriptions",
                        json={"channel": "#general", "subscribed": True})
        assert r.status_code == 200
        assert "#general" in r.json()["subscribed"]
        r = client.post("/api/agents/scout/subscriptions",
                        json={"channel": "#general", "subscribed": False})
        assert r.json()["unsubscribed"] == ["#general"]


class TestUsage:
    def _rec(self, **kw):
        r = {"usage": {"input_tokens": 1000, "output_tokens": 500},
             "cost": {"amount": "0.01", "status": "priced"},
             "model": "claude-haiku-4-5", "model_ref": "anthropic:claude-haiku-4-5"}
        r.update(kw)
        return r

    def test_record_and_summary(self, home):
        from quarterdeck import usage as usage_mod
        n = usage_mod.record_pinnace_usage("scout", [self._rec()], home=home)
        assert n == 1
        s = usage_mod.summary(home=home)
        assert s["input_tokens"] == 1000
        assert s["output_tokens"] == 500
        assert s["cost_usd"] == 0.01
        assert s["by_agent"]["scout"]["calls"] == 1

    def test_fallback_pricing(self, home):
        from quarterdeck import usage as usage_mod
        rec = self._rec(cost={"amount": None, "status": "usage_unavailable"})
        usage_mod.record_pinnace_usage("scout", [rec], home=home)
        s = usage_mod.summary(home=home)
        # haiku fallback: 1000/1e6*0.25 + 500/1e6*1.25 (summary rounds to 4dp)
        assert s["cost_usd"] == round(0.000875, 4)
        assert s["cost_estimated"] is True

    def test_credit_balance_roundtrip(self, home):
        from quarterdeck import usage as usage_mod
        assert usage_mod.get_credit_balance(home=home) is None
        usage_mod.set_credit_balance(25.0, home=home)
        assert usage_mod.get_credit_balance(home=home) == 25.0
        usage_mod.set_credit_balance(None, home=home)
        assert usage_mod.get_credit_balance(home=home) is None

    def test_burn_rate_and_eta(self, home):
        from quarterdeck import usage as usage_mod
        usage_mod.record_pinnace_usage("scout", [self._rec()], home=home)
        usage_mod.set_credit_balance(10.0, home=home)
        b = usage_mod.burn_rate(home=home, window_hours=1.0)
        assert b["tokens_per_hour"] > 0
        eta = usage_mod.credit_eta(home=home)
        assert eta["credit_balance_usd"] == 10.0
        assert eta["eta_hours"] is not None and eta["eta_hours"] > 0

    def test_usage_api(self, home):
        from fastapi.testclient import TestClient
        from quarterdeck.web import create_app
        from quarterdeck import usage as usage_mod
        usage_mod.record_pinnace_usage("scout", [self._rec()], home=home)
        client = TestClient(create_app(home=home))
        r = client.get("/api/usage")
        assert r.status_code == 200
        assert r.json()["summary"]["total_tokens"] == 1500
        r = client.put("/api/usage/balance", json={"balance_usd": 42.5})
        assert r.json()["credit_balance_usd"] == 42.5


class TestDeregister:
    def test_deregister_stops_and_archives(self, home):
        async def go():
            from quarterdeck.daemon import Daemon
            from quarterdeck.agent_registry import AgentRegistry
            reg = AgentRegistry(home=home)
            reg.spawn("tmpagent", system_prompt="x")
            # agent files exist
            (home / "agents" / "tmpagent").mkdir(parents=True, exist_ok=True)
            (home / "agents" / "tmpagent" / "memory.md").write_text("stuff")
            d = Daemon(home=home)
            d._loop = asyncio.get_running_loop()
            await d._start_runner("tmpagent", AgentRunner)
            assert "tmpagent" in d.runners
            out = await d.deregister("tmpagent")
            assert out["status"] == "deregistered"
            assert "tmpagent" not in d.runners
            assert "tmpagent" in d._deregistered
            # retired from registry
            assert not reg.exists("tmpagent")
            # archived, not deleted
            assert out["archived"] is not None
            assert Path(out["archived"]).exists()
            assert not (home / "agents" / "tmpagent").exists()
            assert (Path(out["archived"]) / "memory.md").read_text() == "stuff"
            # supervisor won't restart
            for t in list(d.tasks.values()):
                t.cancel()
        asyncio.run(go())

    def test_deregister_protects_seed(self, home):
        async def go():
            from quarterdeck.daemon import Daemon
            d = Daemon(home=home)
            out = await d.deregister("seed")
            assert out["status"] == "error"
        asyncio.run(go())

    def test_deregister_unknown(self, home):
        async def go():
            from quarterdeck.daemon import Daemon
            d = Daemon(home=home)
            out = await d.deregister("ghost")
            assert out["status"] == "not-running"
        asyncio.run(go())

    def test_seed_has_deregister_tool(self, home):
        from quarterdeck.tools import quarterdeck_tools
        names = [t.name for t in quarterdeck_tools("seed", home=home)]
        assert "quarterdeck_deregister_agent" in names
        names2 = [t.name for t in quarterdeck_tools("scout", home=home)]
        assert "quarterdeck_deregister_agent" not in names2


class TestMessageIds:
    def test_post_assigns_ordered_unique_ids(self, home):
        chat = Chat(home=home)
        m1 = chat.post("#general", "a", "one")
        m2 = chat.post("#general", "a", "two")
        assert m1["id"] != m2["id"]
        assert m1["id"] < m2["id"]  # lexicographic == chronological

    def test_message_id_stable_for_old_records(self, home):
        from quarterdeck.chat import message_id
        m = {"ts": 1234567.0, "author": "a", "text": "old"}
        assert message_id(m) == message_id(m)
        assert message_id(m) < "99999999999999999999-ffffffffffff"

    def test_after_returns_messages_after_cursor(self, home):
        chat = Chat(home=home)
        m1 = chat.post("#general", "a", "one")
        chat.post("#general", "a", "two")
        m3 = chat.post("#general", "a", "three")
        got = chat.after("#general", m1["id"])
        assert [m["id"] for m in got] == [m["id"] for m in chat.history("#general")[1:]]
        assert got[-1]["id"] == m3["id"]
        assert chat.after("#general", None) == chat.history("#general")

    def test_mentions_use_id_cursors(self, home):
        from quarterdeck import subscriptions
        chat = Chat(home=home)
        subscriptions.subscribe("scout", ["#general"], home=home)
        chat.post("#general", "human", "hello @scout")
        out = subscriptions.check_mentions("scout", chat, home=home)
        assert out["count"] == 1
        # second check: cursor advanced, no re-read
        out2 = subscriptions.check_mentions("scout", chat, home=home)
        assert out2["count"] == 0


class TestSelfMaintenance:
    def _tools(self, home, name="scout"):
        r = AgentRunner(name, home=home)
        return {t.name: t for t in r.file_tools()}

    def test_rewrite_prompt_identify(self, home):
        tools = self._tools(home)
        out = tools["quarterdeck_rewrite_prompt"].invoke(
            {"section": "identify", "content": "new identity"})
        assert "archived" in out
        r = AgentRunner("scout", home=home)
        assert r.files.read("scout", "identify.md") == "new identity"
        hist = list((home / "agents" / "scout" / "prompt_history").glob("identify-*.md"))
        assert len(hist) == 1

    def test_rewrite_prompt_bad_section(self, home):
        tools = self._tools(home)
        out = tools["quarterdeck_rewrite_prompt"].invoke(
            {"section": "bogus", "content": "x"})
        assert out.startswith("error:")

    def test_rewrite_memory(self, home):
        tools = self._tools(home)
        out = tools["quarterdeck_rewrite_memory"].invoke({"content": "compacted"})
        assert "archived" in out
        r = AgentRunner("scout", home=home)
        assert r.files.read("scout", "memory.md") == "compacted"

    def test_compact_memory_flow(self, home):
        tools = self._tools(home)
        r = AgentRunner("scout", home=home)
        r.files.write("scout", "memory.md", "# memory\n\nlots of stuff")
        out = tools["quarterdeck_compact_memory"].invoke({})
        assert "quarterdeck_rewrite_memory" in out
        assert "lots of stuff" in out
        # follow through: agent rewrites
        tools["quarterdeck_rewrite_memory"].invoke({"content": "key learning"})
        assert r.files.read("scout", "memory.md") == "key learning"
