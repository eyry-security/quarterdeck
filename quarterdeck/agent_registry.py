"""Agent registry: named agents with persistent identity and lifecycle state.

Each agent holds the config needed to build a PinnaceAgent (model ref, system
prompt, sandbox kind, max turns) plus a lifecycle state: idle or working.
Persisted as JSON under ~/.quarterdeck (or $QUARTERDECK_HOME).

Pinnace is never imported here; building a runnable agent from a config is the
scheduler's job.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path

IDLE = "idle"
WORKING = "working"
STATES = (IDLE, WORKING)

SANDBOXES = ("docker", "local")


def home_dir() -> Path:
    return Path(os.environ.get("QUARTERDECK_HOME", Path.home() / ".quarterdeck"))


@dataclass
class Agent:
    """One named agent's identity: its Pinnace config + lifecycle state."""

    name: str
    system_prompt: str
    model: str | None = None  # "provider:model"; None -> PinnaceAgent defaults ($PINNACE_MODEL)
    sandbox: str = "docker"  # "docker" | "local"
    max_turns: int = 30
    state: str = IDLE  # "idle" | "working"
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Agent":
        return cls(**{f: d.get(f) for f in _FIELDS})

    def pinnace_kwargs(self) -> dict:
        """Kwargs for PinnaceAgent(model=..., system_prompt=..., max_turns=...)."""
        return {
            "model": self.model,
            "system_prompt": self.system_prompt,
            "max_turns": self.max_turns,
        }


_FIELDS = ("name", "system_prompt", "model", "sandbox", "max_turns", "state",
           "created_at", "updated_at")


class RegistryError(Exception):
    pass


class AgentRegistry:
    """Persistent registry of named agents. JSON file, atomic rewrites."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self._file = self.home / "agents.json"
        self._agents: dict[str, Agent] = {}
        self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
        except (json.JSONDecodeError, OSError):
            return  # corrupt or unreadable: start empty rather than crash
        for entry in data.get("agents", []):
            try:
                agent = Agent.from_dict(entry)
            except TypeError:
                continue
            self._agents[agent.name] = agent

    def _save(self) -> None:
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"agents": [a.to_dict() for a in self._agents.values()]},
                                  indent=2, sort_keys=True))
        tmp.replace(self._file)

    # -- lifecycle ---------------------------------------------------------

    def spawn(self, name: str, system_prompt: str, *, model: str | None = None,
              sandbox: str = "docker", max_turns: int = 30) -> Agent:
        if not name or not name.strip():
            raise RegistryError("agent name must not be empty")
        if name in self._agents:
            raise RegistryError(f"agent {name!r} already exists")
        if sandbox not in SANDBOXES:
            raise RegistryError(f"sandbox must be one of {SANDBOXES}, got {sandbox!r}")
        if max_turns < 1:
            raise RegistryError("max_turns must be >= 1")
        now = time.time()
        agent = Agent(name=name, system_prompt=system_prompt, model=model,
                      sandbox=sandbox, max_turns=max_turns,
                      state=IDLE, created_at=now, updated_at=now)
        self._agents[name] = agent
        self._save()
        return agent

    def get(self, name: str) -> Agent:
        try:
            return self._agents[name]
        except KeyError:
            raise RegistryError(f"no agent named {name!r}") from None

    def list(self) -> list[Agent]:
        return sorted(self._agents.values(), key=lambda a: a.name)

    def retire(self, name: str) -> Agent:
        agent = self.get(name)
        del self._agents[name]
        self._save()
        return agent

    def set_state(self, name: str, state: str) -> Agent:
        if state not in STATES:
            raise RegistryError(f"state must be one of {STATES}, got {state!r}")
        agent = self.get(name)
        agent.state = state
        agent.updated_at = time.time()
        self._save()
        return agent

    def exists(self, name: str) -> bool:
        return name in self._agents
