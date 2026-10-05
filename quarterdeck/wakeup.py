"""Wakeup prompts: mechanical template, no LLM.

The seed calls build_wakeup_prompt() when spawning a new agent. The prompt
is written to the agent's wakeup.json; the agent's loop consumes it on
first boot, then runs on its own. Wakeup prompts are spawn-time only —
existing agents don't get them (they have memory.md and sessions already).
"""
from __future__ import annotations

import time
from pathlib import Path


def build_wakeup_prompt(name: str, identify: str = "", runbook: str = "",
                        channels: list[str] | None = None) -> str:
    """Assemble a wakeup prompt for a brand-new agent. Deterministic."""
    name = (name or "").strip()
    L = [
        f"You are {name}, a persistent agent in the Quarterdeck chat room.",
        f"Current time: {time.strftime('%A %Y-%m-%d %H:%M %Z', time.localtime())}.",
        "You were just born. This prompt is your first briefing — read it, "
        "then start your loop.",
    ]
    if identify.strip():
        L.append(f"Who you are: {identify.strip()[:600]}")
    if runbook.strip():
        # First lines of the runbook = the essentials.
        essentials = "\n".join(runbook.strip().splitlines()[:8])
        L.append(f"Your runbook (essentials):\n{essentials[:800]}")
    if channels:
        L.append(f"Room channels: {', '.join(channels[:10])}.")
    L.append(
        "First actions: 1) Read your memory.md (it's nearly empty — that's fine). "
        "2) quarterdeck_subscribe to the channels you care about. "
        "3) Skim #general for context. "
        "4) Follow your runbook; if nothing needs doing, stay quiet and useful."
    )
    return "\n".join(L)


def write_wakeup(home: Path, name: str, prompt: str) -> Path:
    """Persist a wakeup prompt for the agent's first boot. Returns the path."""
    import json
    from .agent import _safe
    d = Path(home) / "agents" / _safe(name)
    d.mkdir(parents=True, exist_ok=True)
    f = d / "wakeup.json"
    f.write_text(json.dumps({"prompt": prompt, "at": time.time()}),
                 encoding="utf-8")
    return f
