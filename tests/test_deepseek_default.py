"""DeepSeek default resolution, catalog, and pricing without network access."""

from __future__ import annotations

import socket

import pytest

from quarterdeck.agent_registry import AgentRegistry
from quarterdeck.models import DEFAULT_MODEL


def _block_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(socket, "socket", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)


def test_runner_resolves_deepseek_default_without_network(tmp_path, monkeypatch):
    _block_network(monkeypatch)
    AgentRegistry(home=tmp_path).spawn("scout", "watch")

    from quarterdeck.agent import AgentRunner

    runner = AgentRunner("scout", home=tmp_path)
    assert DEFAULT_MODEL == "deepseek:deepseek-flash"
    assert runner._current_model() == DEFAULT_MODEL


def test_cli_spawn_defaults_to_deepseek_without_network(tmp_path, monkeypatch):
    _block_network(monkeypatch)
    from quarterdeck.cli import main

    home = tmp_path / "qd"
    assert main(["--home", str(home), "spawn", "--name", "scout",
                 "--system", "watch"]) == 0
    assert AgentRegistry(home=home).get("scout").model == DEFAULT_MODEL


def test_llm_available_accepts_deepseek_key(monkeypatch):
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    from quarterdeck.agent import llm_available

    assert llm_available()


def test_catalog_and_pricing_include_deepseek(monkeypatch):
    monkeypatch.delenv("QUARTERDECK_MODELS", raising=False)
    from quarterdeck import usage as u
    from quarterdeck.web import available_models

    models = available_models()
    assert models[0] == DEFAULT_MODEL
    assert "deepseek:deepseek-v4-pro" in models
    assert "anthropic:claude-opus-4-6" in models

    assert u._fallback_cost("deepseek-flash", 1_000_000, 0) == pytest.approx(0.15)
    assert u._fallback_cost("deepseek-flash", 0, 1_000_000) == pytest.approx(0.60)
    assert u._fallback_cost(
        "deepseek-flash", 1_000_000, 0, cache_read=1_000_000
    ) == pytest.approx(0.003)
    assert u._fallback_cost(
        "deepseek-v4-pro", 1_000_000, 1_000_000
    ) == pytest.approx(0.66 + 1.98)
