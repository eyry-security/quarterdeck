"""In-process pub/sub event bus. Threads use it to coordinate; everything else
just subscribes to event types.

Event types: "tick" (scheduler heartbeat), "chat_message" (someone posted in
chat), "webhook" (inbound external event; v0 is a stub hook for later HTTP
receivers).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Callable

TICK = "tick"
CHAT_MESSAGE = "chat_message"
WEBHOOK = "webhook"

Handler = Callable[["Event"], None]


@dataclass
class Event:
    type: str
    payload: dict = field(default_factory=dict)
    ts: float = field(default_factory=time.time)


class EventBus:
    """Tiny synchronous pub/sub. Handlers run in the publisher's thread."""

    def __init__(self):
        self._subs: dict[str, list[Handler]] = {}
        self._lock = Lock()

    def subscribe(self, event_type: str, fn: Handler) -> Callable[[], None]:
        """Subscribe fn to an event type. Returns an unsubscribe callable."""
        with self._lock:
            self._subs.setdefault(event_type, []).append(fn)

        def unsubscribe() -> None:
            with self._lock:
                handlers = self._subs.get(event_type, [])
                if fn in handlers:
                    handlers.remove(fn)

        return unsubscribe

    def publish(self, event: Event) -> None:
        """Deliver event to all subscribers of its type. Exceptions in one
        handler do not stop the others."""
        with self._lock:
            handlers = list(self._subs.get(event.type, []))
        for fn in handlers:
            try:
                fn(event)
            except Exception:
                continue  # a bad handler must not kill the bus
