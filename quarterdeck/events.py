"""Typed events with durable single-host persistence and deduplication."""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock
from typing import Callable

from .locking import file_lock

TICK = "tick"
CHAT_MESSAGE = "chat_message"
WEBHOOK = "webhook"
RUN_REQUESTED = "agent.run.requested"
RUN_COMPLETED = "agent.run.completed"
APLOMADO_SCAN_COMPLETED = "aplomado.scan.completed"
SUPPORTED_SCHEMA_VERSION = 1

Handler = Callable[["Event"], None]


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = "quarterdeck"
    schema_version: int = SUPPORTED_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(
                f"unsupported event schema version {self.schema_version!r}; "
                f"expected {SUPPORTED_SCHEMA_VERSION}"
            )
        if not isinstance(self.type, str) or not self.type:
            raise ValueError("event type must be non-empty text")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("event id must be non-empty text")
        if not isinstance(self.source, str) or not self.source:
            raise ValueError("event source must be non-empty text")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)):
            raise ValueError("event ts must be numeric")
        if not isinstance(self.payload, dict):
            raise ValueError("event payload must be an object")

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "Event":
        if not isinstance(value, dict):
            raise ValueError("event must be an object")
        schema_version = value.get("schema_version", SUPPORTED_SCHEMA_VERSION)
        if type(schema_version) is not int or schema_version != SUPPORTED_SCHEMA_VERSION:
            raise ValueError(f"unsupported event schema version {schema_version!r}")
        event_type = value.get("type")
        event_id = value.get("id")
        source = value.get("source", "unknown")
        payload = value.get("payload", {})
        timestamp = value.get("ts", time.time())
        return cls(
            type=event_type,
            payload=payload,
            ts=timestamp,
            id=event_id,
            source=source,
            schema_version=schema_version,
        )


class EventStore:
    """Append-only JSONL history with process-safe ID claims on one host."""

    def __init__(self, home: Path):
        self.path = Path(home) / "events.jsonl"
        self.lock_path = Path(home) / ".events.lock"
        self.delivery_path = Path(home) / "event-deliveries.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()

    def _contains_id(self, event_id: str) -> bool:
        if not self.path.exists():
            return False
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict) and value.get("id") == event_id:
                return True
        return False

    def append(self, event: Event) -> bool:
        """Durably claim and append an event ID before delivery."""
        with self._lock, file_lock(self.lock_path):
            if self._contains_id(event.id):
                return False
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True

    def record_delivery(self, event: Event, handler: str,
                        status: str, error: str | None = None) -> None:
        record = {
            "event_id": event.id,
            "handler": handler,
            "status": status,
            "error": error,
            "ts": time.time(),
        }
        with self._lock, file_lock(self.lock_path):
            with open(self.delivery_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())

    def _delivery_records(self, event_id: str) -> list[dict]:
        if not self.delivery_path.exists():
            return []
        records = []
        for line in self.delivery_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event_id") == event_id:
                records.append(record)
        return records

    def claim_delivery(self, event: Event, handler: str,
                       lease_seconds: float = 60.0) -> bool:
        """Atomically claim one handler delivery across local processes."""
        with self._lock, file_lock(self.lock_path):
            records = self._delivery_records(event.id)
            latest = next(
                (record for record in reversed(records) if record.get("handler") == handler),
                None,
            )
            if latest and latest.get("status") == "succeeded":
                return False
            if latest and latest.get("status") == "running":
                age = time.time() - float(latest.get("ts", 0))
                if age < lease_seconds:
                    return False
            record = {
                "event_id": event.id,
                "handler": handler,
                "status": "running",
                "error": None,
                "ts": time.time(),
            }
            with open(self.delivery_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return True

    def delivery_statuses(self, event_id: str) -> dict[str, str]:
        statuses: dict[str, str] = {}
        with self._lock, file_lock(self.lock_path):
            records = self._delivery_records(event_id)
        for record in records:
            statuses[str(record.get("handler"))] = str(record.get("status"))
        return statuses

    def history(self, *, event_type: str | None = None,
                limit: int = 100) -> list[Event]:
        if not self.path.exists() or limit <= 0:
            return []
        events: list[Event] = []
        with self._lock, file_lock(self.lock_path):
            lines = self.path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            try:
                event = Event.from_dict(json.loads(line))
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                continue
            if event_type is None or event.type == event_type:
                events.append(event)
        return events[-limit:]

    def get(self, event_id: str) -> Event | None:
        return next((event for event in self.history(limit=1_000_000)
                     if event.id == event_id), None)


class EventBus:
    """Synchronous pub/sub; events commit before delivery."""

    def __init__(self, store: EventStore | None = None):
        self.store = store
        self._subs: dict[str, list[tuple[str, Handler]]] = {}
        self._lock = Lock()

    def subscribe(self, event_type: str, fn: Handler,
                  subscriber_id: str | None = None) -> Callable[[], None]:
        with self._lock:
            handlers = self._subs.setdefault(event_type, [])
            if self.store is not None and not subscriber_id:
                raise ValueError("durable event subscribers require a stable subscriber_id")
            identity = subscriber_id or (
                f"{getattr(fn, '__module__', '')}."
                f"{getattr(fn, '__qualname__', type(fn).__qualname__)}#{len(handlers)}"
            )
            if any(existing == identity for existing, _fn in handlers):
                raise ValueError(f"duplicate subscriber_id {identity!r} for {event_type!r}")
            handlers.append((identity, fn))

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._subs.get(event_type, [])
                self._subs[event_type] = [
                    item for item in handlers if item != (identity, fn)
                ]

        return unsubscribe

    def _deliver(self, event: Event) -> bool:
        with self._lock:
            handlers = list(self._subs.get(event.type, []))
        succeeded = True
        for handler, fn in handlers:
            if self.store is not None and not self.store.claim_delivery(event, handler):
                continue
            try:
                fn(event)
            except Exception as exc:
                succeeded = False
                if self.store is not None:
                    self.store.record_delivery(event, handler, "failed", str(exc)[:4000])
                continue
            if self.store is not None:
                self.store.record_delivery(event, handler, "succeeded")
        return succeeded

    def publish(self, event: Event) -> bool:
        if self.store is not None and not self.store.append(event):
            return False
        self._deliver(event)
        return True

    def replay(self, event_id: str) -> bool:
        """Retry handlers for an already-persisted event without re-appending it."""
        if self.store is None:
            return False
        event = self.store.get(event_id)
        return self._deliver(event) if event is not None else False
