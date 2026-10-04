"""Typed in-process events with optional append-only persistence and dedup."""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable

TICK = "tick"
CHAT_MESSAGE = "chat_message"
WEBHOOK = "webhook"
RUN_REQUESTED = "agent.run.requested"
RUN_COMPLETED = "agent.run.completed"
APLOMADO_SCAN_COMPLETED = "aplomado.scan.completed"

Handler = Callable[["Event"], None]


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "quarterdeck"
    schema_version: int = 1

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Event":
        return cls(
            type=str(value["type"]),
            payload=dict(value.get("payload") or {}),
            ts=float(value.get("ts", time.time())),
            id=str(value.get("id") or uuid.uuid4()),
            source=str(value.get("source") or "unknown"),
            schema_version=int(value.get("schema_version", 1)),
        )


class EventStore:
    """Single-process append-only JSONL event history."""

    def __init__(self, home: Path):
        self.path = Path(home) / "events.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._ids: set[str] = set()
        if self.path.exists():
            for line in self.path.read_text(encoding="utf-8").splitlines():
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = value.get("id") if isinstance(value, dict) else None
                if event_id:
                    self._ids.add(str(event_id))

    def append(self, event: Event) -> bool:
        """Persist an event. Return false when its ID was already seen."""
        with self._lock:
            if event.id in self._ids:
                return False
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
            self._ids.add(event.id)
            return True

    def history(self, *, event_type: str | None = None,
                limit: int = 100) -> list[Event]:
        if not self.path.exists() or limit <= 0:
            return []
        events: list[Event] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
                event = Event.from_dict(value)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if event_type is None or event.type == event_type:
                events.append(event)
        return events[-limit:]


class EventBus:
    """Synchronous pub/sub; optionally commits events before delivery."""

    def __init__(self, store: EventStore | None = None):
        self.store = store
        self._subs: dict[str, list[Handler]] = {}
        self._lock = Lock()

    def subscribe(self, event_type: str, fn: Handler) -> Callable[[], None]:
        with self._lock:
            self._subs.setdefault(event_type, []).append(fn)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._subs.get(event_type, [])
                if fn in handlers:
                    handlers.remove(fn)

        return unsubscribe

    def publish(self, event: Event) -> bool:
        """Persist then deliver once. Handler failures remain isolated."""
        if self.store is not None and not self.store.append(event):
            return False
        with self._lock:
            handlers = list(self._subs.get(event.type, []))
        for fn in handlers:
            try:
                fn(event)
            except Exception:
                continue
        return True
