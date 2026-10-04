"""Named Pinnace agent configuration and lifecycle state."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .locking import file_lock

IDLE = "idle"
WORKING = "working"
STATES = (IDLE, WORKING)
SANDBOXES = ("docker", "local")


def home_dir() -> Path:
    return Path(os.environ.get("QUARTERDECK_HOME", Path.home() / ".quarterdeck"))


def agent_key(name: str) -> str:
    """Collision-resistant filesystem/session key for any legacy agent name."""
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


@dataclass
class Agent:
    name: str
    system_prompt: str
    model: str | None = None
    sandbox: str = "docker"
    max_turns: int = 30
    state: str = IDLE
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Agent":
        return cls(
            name=value["name"],
            system_prompt=value["system_prompt"],
            model=value.get("model"),
            sandbox=value.get("sandbox", "docker"),
            max_turns=value.get("max_turns", 30),
            state=value.get("state", IDLE),
            created_at=value.get("created_at", 0.0),
            updated_at=value.get("updated_at", 0.0),
        )

    def pinnace_kwargs(self) -> dict:
        return {
            "model": self.model,
            "system_prompt": self.system_prompt,
            "max_turns": self.max_turns,
        }


class RegistryError(Exception):
    pass


class AgentRegistry:
    """Process-safe JSON registry preserving the v0 on-disk shape."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self._file = self.home / "agents.json"
        self._lock_file = self.home / ".agents.lock"
        self._thread_lock = threading.RLock()
        self._agents: dict[str, Agent] = {}
        self._load()

    def _load(self, *, clear: bool = False, strict: bool = False) -> None:
        if clear:
            self._agents.clear()
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            if strict:
                raise RegistryError(f"cannot reload agent registry: {exc}") from exc
            return
        for entry in data.get("agents", []):
            try:
                agent = Agent.from_dict(entry)
            except (KeyError, TypeError):
                continue
            self._agents[agent.name] = agent

    def _save(self) -> None:
        tmp = self._file.with_suffix(f".json.{os.getpid()}.tmp")
        payload = json.dumps(
            {"agents": [agent.to_dict() for agent in self._agents.values()]},
            indent=2, sort_keys=True,
        )
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self._file)

    def _refresh(self) -> None:
        self._load(clear=True, strict=True)

    def spawn(self, name: str, system_prompt: str, *, model: str | None = None,
              sandbox: str = "docker", max_turns: int = 30) -> Agent:
        if not isinstance(name, str) or not name.strip():
            raise RegistryError("agent name must not be empty")
        if sandbox not in SANDBOXES:
            raise RegistryError(f"sandbox must be one of {SANDBOXES}, got {sandbox!r}")
        if max_turns < 1:
            raise RegistryError("max_turns must be >= 1")
        with self._thread_lock, file_lock(self._lock_file):
            self._refresh()
            if name in self._agents:
                raise RegistryError(f"agent {name!r} already exists")
            now = time.time()
            agent = Agent(
                name=name, system_prompt=system_prompt, model=model,
                sandbox=sandbox, max_turns=max_turns, state=IDLE,
                created_at=now, updated_at=now,
            )
            self._agents[name] = agent
            self._save()
            return agent

    def get(self, name: str) -> Agent:
        with self._thread_lock, file_lock(self._lock_file):
            self._refresh()
            try:
                return self._agents[name]
            except KeyError:
                raise RegistryError(f"no agent named {name!r}") from None

    def list(self) -> list[Agent]:
        with self._thread_lock, file_lock(self._lock_file):
            self._refresh()
            return sorted(self._agents.values(), key=lambda agent: agent.name)

    def retire(self, name: str) -> Agent:
        with self._thread_lock, file_lock(self._lock_file):
            self._refresh()
            try:
                agent = self._agents.pop(name)
            except KeyError:
                raise RegistryError(f"no agent named {name!r}") from None
            self._save()
            return agent

    def set_state(self, name: str, state: str) -> Agent:
        if state not in STATES:
            raise RegistryError(f"state must be one of {STATES}, got {state!r}")
        with self._thread_lock, file_lock(self._lock_file):
            self._refresh()
            try:
                agent = self._agents[name]
            except KeyError:
                raise RegistryError(f"no agent named {name!r}") from None
            agent.state = state
            agent.updated_at = time.time()
            self._save()
            return agent

    def exists(self, name: str) -> bool:
        with self._thread_lock, file_lock(self._lock_file):
            self._refresh()
            return name in self._agents
