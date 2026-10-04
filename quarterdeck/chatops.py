"""Explicit local ChatOps routing, independent of external connectors."""

from __future__ import annotations

import re
from concurrent.futures import Future

from .agent_registry import AgentRegistry
from .chat import Chat
from .events import CHAT_MESSAGE, Event, EventBus
from .scheduler import Scheduler

_MENTION = re.compile(r"^@([A-Za-z0-9][A-Za-z0-9_-]{0,63})\s+(.+)$", re.DOTALL)


class ChatOps:
    """Route explicit human mentions to named agents and prevent bot loops."""

    def __init__(self, registry: AgentRegistry, scheduler: Scheduler,
                 chat: Chat, bus: EventBus):
        self.registry = registry
        self.scheduler = scheduler
        self.chat = chat
        self.bus = bus
        self._unsubscribe = None
        self.futures: list[Future] = []

    def route(self, message: dict) -> Future | None:
        if message.get("author") != "you":
            return None
        match = _MENTION.fullmatch(str(message.get("text", "")).strip())
        if not match:
            return None
        agent_name, prompt = match.groups()
        if not self.registry.exists(agent_name):
            self.chat.post(
                message.get("channel", "#general"),
                "quarterdeck",
                f"unknown agent @{agent_name}",
            )
            return None
        future = self.scheduler.wake(
            agent_name,
            "UNTRUSTED CHAT MESSAGE BEGIN\n"
            f"{prompt}\n"
            "UNTRUSTED CHAT MESSAGE END",
            message.get("channel", "#general"),
        )
        self.futures.append(future)
        return future

    def _on_event(self, event: Event) -> None:
        self.route(event.payload)

    def start(self) -> None:
        if self._unsubscribe is None:
            self._unsubscribe = self.bus.subscribe(CHAT_MESSAGE, self._on_event)

    def stop(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
