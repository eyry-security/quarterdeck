"""Persisted event rules that wake agents or post alerts."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Lock

from .agent_registry import AgentRegistry
from .chat import DEFAULT_CHANNEL, Chat
from .events import APLOMADO_SCAN_COMPLETED, Event, EventBus
from .integrations import format_aplomado_alert
from .security import encode_untrusted
from .scheduler import Scheduler

MAX_EVENT_HOPS = 4


@dataclass
class Rule:
    name: str
    event_type: str
    action: str
    agent_name: str | None = None
    prompt: str = "Review this untrusted event:\n{event_json}"
    channel: str = DEFAULT_CHANNEL
    filters: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class RuleError(ValueError):
    pass


class RuleStore:
    def __init__(self, home: Path):
        self.path = Path(home) / "rules.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = Lock()
        self._rules: dict[str, Rule] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            values = json.loads(self.path.read_text(encoding="utf-8")).get("rules", [])
        except (json.JSONDecodeError, OSError, AttributeError):
            return
        for value in values:
            try:
                rule = Rule(**value)
            except (TypeError, ValueError):
                continue
            self._rules[rule.name] = rule

    def _save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(
            {"rules": [rule.to_dict() for rule in self.list()]},
            indent=2, sort_keys=True,
        ))
        tmp.replace(self.path)

    def add(self, rule: Rule) -> Rule:
        if not rule.name.strip() or rule.name in self._rules:
            raise RuleError(f"rule {rule.name!r} is empty or already exists")
        if rule.action not in {"agent", "alert"}:
            raise RuleError("rule action must be 'agent' or 'alert'")
        if rule.action == "agent" and not rule.agent_name:
            raise RuleError("agent rules require agent_name")
        with self._lock:
            self._rules[rule.name] = rule
            self._save()
        return rule

    def remove(self, name: str) -> None:
        with self._lock:
            if name not in self._rules:
                raise RuleError(f"no rule named {name!r}")
            del self._rules[name]
            self._save()

    def list(self) -> list[Rule]:
        return sorted(self._rules.values(), key=lambda rule: rule.name)


class RuleEngine:
    """Routes persisted rules from EventBus into scheduler/chat actions."""

    def __init__(self, store: RuleStore, registry: AgentRegistry,
                 scheduler: Scheduler, chat: Chat, bus: EventBus):
        self.store = store
        self.registry = registry
        self.scheduler = scheduler
        self.chat = chat
        self.bus = bus
        self._unsubscribers = []

    def start(self) -> None:
        event_types = {rule.event_type for rule in self.store.list()}
        self._unsubscribers = [
            self.bus.subscribe(
                event_type, self.handle, subscriber_id=f"quarterdeck.rules:{event_type}"
            )
            for event_type in event_types
        ]

    def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            unsubscribe()
        self._unsubscribers = []

    def handle(self, event: Event) -> None:
        for rule in self.store.list():
            if rule.event_type != event.type:
                continue
            if any(event.payload.get(key) != value for key, value in rule.filters.items()):
                continue
            if rule.action == "alert":
                if event.type == APLOMADO_SCAN_COMPLETED:
                    text = format_aplomado_alert(event.payload.get("data") or {})
                else:
                    text = json.dumps(event.payload, sort_keys=True)[:4000]
                self.chat.post(rule.channel, "quarterdeck", text)
                continue
            if not self.registry.exists(rule.agent_name or ""):
                continue
            hop_count = int(event.payload.get("hop_count", 0))
            if hop_count >= MAX_EVENT_HOPS:
                continue
            payload = json.dumps(event.payload, sort_keys=True)[:20_000]
            rendered = rule.prompt.replace("{event_type}", event.type).replace(
                "{event_json}", payload
            )
            prompt = encode_untrusted("EVENT", rendered)
            self.scheduler.wake(
                rule.agent_name or "", prompt, rule.channel,
                cause={"event_id": event.id, "hop_count": hop_count + 1},
            )
