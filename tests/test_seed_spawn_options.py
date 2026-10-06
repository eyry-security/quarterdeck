"""Seed spawn options: sandbox/model/max_turns pass-through + persistence modes."""
from __future__ import annotations

import pytest

from quarterdeck.seed import SeedRunner, MEMORY_SYSTEM_SECTION


@pytest.fixture
def home(tmp_path):
    return tmp_path / "qd"


@pytest.fixture
def seed(home):
    return SeedRunner("seed", home=home)


@pytest.fixture
def register_tool(seed):
    return {t.name: t for t in seed.all_tools()}["quarterdeck_register_agent"]


def _base(seed, register_tool, name, **kw):
    args = {"name": name, "identify": f"I am {name}", "runbook": f"# {name} runbook"}
    args.update(kw)
    return register_tool.invoke(args)


def test_defaults_are_docker_and_full_persistence(home, seed, register_tool):
    res = _base(seed, register_tool, "a1")
    assert "registered and woken" in res
    agent = seed.registry.get("a1")
    assert agent.sandbox == "docker"
    assert agent.model is None
    assert agent.max_turns == 25
    assert "six layers" in seed.files.read("a1", "runbook.md")
    assert "Born" in seed.files.read("a1", "memory.md")


def test_local_sandbox_passthrough(home, seed, register_tool):
    res = _base(seed, register_tool, "a2", sandbox="local")
    assert "registered and woken" in res
    assert seed.registry.get("a2").sandbox == "local"


def test_invalid_sandbox_rejected(home, seed, register_tool):
    res = _base(seed, register_tool, "a3", sandbox="gvisor")
    assert "error: registry:" in res
    assert not seed.registry.exists("a3")


def test_model_passthrough(home, seed, register_tool):
    res = _base(seed, register_tool, "a4", model="anthropic:claude-haiku-4-5")
    assert "registered and woken" in res
    assert seed.registry.get("a4").model == "anthropic:claude-haiku-4-5"


def test_max_turns_passthrough(home, seed, register_tool):
    res = _base(seed, register_tool, "a5", max_turns=50)
    assert "registered and woken" in res
    assert seed.registry.get("a5").max_turns == 50


def test_persistence_simple(home, seed, register_tool):
    res = _base(seed, register_tool, "a6", persistence="simple")
    assert "registered and woken" in res
    assert "six layers" not in seed.files.read("a6", "runbook.md")
    assert "Born" in seed.files.read("a6", "memory.md")


def test_persistence_none(home, seed, register_tool):
    res = _base(seed, register_tool, "a7", persistence="none")
    assert "registered and woken" in res
    assert "six layers" not in seed.files.read("a7", "runbook.md")
    assert seed.files.read("a7", "memory.md") == ""


def test_persistence_invalid_rejected_before_spawn(home, seed, register_tool):
    res = _base(seed, register_tool, "a8", persistence="quantum")
    assert "error: persistence" in res
    assert not seed.registry.exists("a8")


def test_full_persistence_appends_memory_section(home, seed, register_tool):
    res = _base(seed, register_tool, "a9", persistence="full")
    assert "registered and woken" in res
    runbook = seed.files.read("a9", "runbook.md")
    assert runbook.startswith("# a9 runbook")
    assert MEMORY_SYSTEM_SECTION in runbook
    assert "Born" in seed.files.read("a9", "memory.md")
