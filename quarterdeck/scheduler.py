"""Interval scheduler: wakes agents on a timer, runs them, posts to chat.

Each entry names an agent, an interval in seconds (v0 — no cron parsing yet),
and a prompt template. On each tick, due entries fire: the agent's lifecycle
state goes to "working", its PinnaceAgent runs the prompt in a worker thread,
and a summary of the result is posted to #general. Then the agent sleeps
(state back to "idle").

The runner is injectable: tests pass a fake callable; production uses the
default runner that builds a PinnaceAgent (imported lazily, so quarterdeck
works without pinnace installed until you actually run an agent).
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from .agent_registry import IDLE, WORKING, Agent, AgentRegistry, home_dir
from .chat import DEFAULT_CHANNEL, Chat
from .events import TICK, Event, EventBus

Runner = Callable[[Agent, str], str]
"""A runner wakes an agent with a prompt and returns a summary string."""

PINNACE_INSTALL_HINT = (
    "pinnace is not installed; quarterdeck can't run agents without it: "
    "pip install pinnace (or pip install -e ../pinnace for local dev)"
)


def default_runner(agent: Agent, prompt: str) -> str:
    """Run the agent's PinnaceAgent once and return a summary string.

    Pinnace is imported here, lazily, so the rest of quarterdeck works
    without it installed. Clear error instead of a traceback when missing.
    """
    try:
        from pinnace import PinnaceAgent
        from pinnace import DockerSandbox, LocalSandbox
    except ImportError as e:
        raise RuntimeError(PINNACE_INSTALL_HINT) from e

    sandbox = DockerSandbox() if agent.sandbox == "docker" else LocalSandbox(unsafe_ok=True)
    runner = PinnaceAgent(
        model=agent.model,
        system_prompt=agent.system_prompt,
        max_turns=agent.max_turns,
        sandbox=sandbox,
        session_id=f"quarterdeck-{agent.name}",
        log=lambda m: None,  # keep scheduler output clean; transcript is summarized
    )
    try:
        result = runner.run(prompt)
    finally:
        sandbox.close()
    final = (result.final or "").strip()
    if len(final) > 2000:
        final = final[:2000] + "…"
    structured = ""
    if result.structured:
        try:
            structured = json.dumps(result.structured)[:1000]
        except (TypeError, ValueError):
            structured = str(result.structured)[:1000]
    summary = f"woke up and worked for {result.turns} turn(s)."
    if final:
        summary += f"\n{final}"
    if structured:
        summary += f"\nstructured: {structured}"
    return summary


@dataclass
class ScheduleEntry:
    name: str
    agent_name: str
    interval: float  # seconds between wakes
    prompt: str
    channel: str = DEFAULT_CHANNEL
    last_run: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleEntry":
        return cls(**{f: d.get(f) for f in
                      ("name", "agent_name", "interval", "prompt", "channel", "last_run")})


class SchedulerError(Exception):
    pass


class Scheduler:
    """Interval scheduler. `start()` runs the loop in a background thread;
    `stop()` shuts it down cleanly. `tick_once()` fires due entries
    synchronously — useful for tests and one-shot runs."""

    def __init__(self, registry: AgentRegistry, chat: Chat,
                 bus: EventBus | None = None,
                 runner: Runner | None = None,
                 home: Path | None = None):
        self.registry = registry
        self.chat = chat
        self.bus = bus or EventBus()
        self.runner = runner or default_runner
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self._file = self.home / "schedules.json"
        self._entries: dict[str, ScheduleEntry] = {}
        self._load()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- persistence ------------------------------------------------------

    def _load(self) -> None:
        if not self._file.exists():
            return
        try:
            data = json.loads(self._file.read_text())
        except (json.JSONDecodeError, OSError):
            return
        for entry in data.get("schedules", []):
            try:
                e = ScheduleEntry.from_dict(entry)
            except TypeError:
                continue
            self._entries[e.name] = e

    def _save(self) -> None:
        tmp = self._file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"schedules": [e.to_dict() for e in self._entries.values()]},
            indent=2, sort_keys=True))
        tmp.replace(self._file)

    # -- schedule management -----------------------------------------------

    def add(self, name: str, agent_name: str, interval: float, prompt: str,
            channel: str = DEFAULT_CHANNEL) -> ScheduleEntry:
        if name in self._entries:
            raise SchedulerError(f"schedule {name!r} already exists")
        if not self.registry.exists(agent_name):
            raise SchedulerError(f"no agent named {agent_name!r}")
        if interval <= 0:
            raise SchedulerError("interval must be > 0 seconds")
        entry = ScheduleEntry(name=name, agent_name=agent_name, interval=interval,
                              prompt=prompt, channel=channel,
                              last_run=time.time())  # don't fire immediately
        self._entries[name] = entry
        self._save()
        return entry

    def remove(self, name: str) -> None:
        if name not in self._entries:
            raise SchedulerError(f"no schedule named {name!r}")
        del self._entries[name]
        self._save()

    def remove_for_agent(self, agent_name: str) -> int:
        """Drop all schedules for a retired agent. Returns count removed."""
        doomed = [n for n, e in self._entries.items() if e.agent_name == agent_name]
        for n in doomed:
            del self._entries[n]
        if doomed:
            self._save()
        return len(doomed)

    def list(self) -> list[ScheduleEntry]:
        return sorted(self._entries.values(), key=lambda e: e.name)

    # -- running -----------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="quarterdeck-scheduler",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10.0) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=timeout)
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.bus.publish(Event(type=TICK, payload={"ts": time.time()}))
            self.tick_once()
            self._stop.wait(1.0)  # 1s resolution; v0 is seconds-granular

    def tick_once(self) -> list[str]:
        """Fire all due entries. Returns names of schedules that fired."""
        now = time.time()
        fired: list[str] = []
        with self._lock:
            entries = sorted(self._entries.values(), key=lambda e: e.name)
        for entry in entries:
            if now - entry.last_run >= entry.interval:
                entry.last_run = now
                self._fire(entry)
                fired.append(entry.name)
        if fired:
            self._save()
        return fired

    def _fire(self, entry: ScheduleEntry) -> None:
        t = threading.Thread(target=self._wake, args=(entry,),
                             name=f"quarterdeck-wake-{entry.agent_name}", daemon=True)
        t.start()

    def _wake(self, entry: ScheduleEntry) -> None:
        agent = self.registry.get(entry.agent_name)
        self.registry.set_state(agent.name, WORKING)
        prompt = entry.prompt.format(agent_name=agent.name)
        try:
            summary = self.runner(agent, prompt)
            self.chat.post(entry.channel, agent.name, f"☀️ woke up: {summary}")
        except Exception as e:  # agent errors go to chat, not to the void
            self.chat.post(entry.channel, agent.name,
                           f"⚠️ run failed: {type(e).__name__}: {e}")
        finally:
            self.registry.set_state(agent.name, IDLE)
