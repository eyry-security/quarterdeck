"""Registry: spawn/get/list/retire, state, JSON persistence."""

from __future__ import annotations

import json

import pytest

from quarterdeck.agent_registry import AgentRegistry, RegistryError


@pytest.fixture()
def reg(tmp_path):
    return AgentRegistry(home=tmp_path / "qd")


def test_spawn_and_get(reg):
    a = reg.spawn("scout", "you watch the horizon", model="anthropic:claude-sonnet-4-5",
                  sandbox="local", max_turns=5)
    assert a.name == "scout"
    assert a.state == "idle"
    got = reg.get("scout")
    assert got.system_prompt == "you watch the horizon"
    assert got.model == "anthropic:claude-sonnet-4-5"
    assert got.sandbox == "local"
    assert got.max_turns == 5


def test_spawn_duplicate_rejected(reg):
    reg.spawn("scout", "x")
    with pytest.raises(RegistryError):
        reg.spawn("scout", "y")


def test_spawn_validates(reg):
    with pytest.raises(RegistryError):
        reg.spawn("", "x")
    with pytest.raises(RegistryError):
        reg.spawn("a", "x", sandbox="gvisor")
    with pytest.raises(RegistryError):
        reg.spawn("a", "x", max_turns=0)


def test_persistence_across_instances(tmp_path):
    reg1 = AgentRegistry(home=tmp_path / "qd")
    reg1.spawn("scout", "watch things", model="openai:gpt-4o")
    reg1.set_state("scout", "working")
    reg2 = AgentRegistry(home=tmp_path / "qd")
    a = reg2.get("scout")
    assert a.system_prompt == "watch things"
    assert a.model == "openai:gpt-4o"
    assert a.state == "working"
    agents_file = tmp_path / "qd" / "agents.json"
    assert json.loads(agents_file.read_text())["agents"][0]["name"] == "scout"


def test_list_sorted(reg):
    reg.spawn("zeta", "x")
    reg.spawn("alpha", "x")
    assert [a.name for a in reg.list()] == ["alpha", "zeta"]


def test_set_state_roundtrip(reg):
    reg.spawn("scout", "x")
    reg.set_state("scout", "working")
    assert reg.get("scout").state == "working"
    with pytest.raises(RegistryError):
        reg.set_state("scout", "sleeping")


def test_retire(reg):
    reg.spawn("scout", "x")
    reg.retire("scout")
    assert not reg.exists("scout")
    with pytest.raises(RegistryError):
        reg.get("scout")
    with pytest.raises(RegistryError):
        reg.retire("scout")


def test_pinnace_kwargs(reg):
    a = reg.spawn("scout", "watch", model=None, max_turns=7)
    kw = a.pinnace_kwargs()
    assert kw["model"] is None  # PinnaceAgent falls back to $PINNACE_MODEL
    assert kw["system_prompt"] == "watch"
    assert kw["max_turns"] == 7
