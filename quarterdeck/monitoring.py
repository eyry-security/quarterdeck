"""Per-agent channel monitoring, wake policy, batching, and wake history.

The monitor is a service-level consumer of the event bus. It keeps a
monitored-channel set per agent, decides which messages and system events
should wake which agent, rate-limits wakes per channel, and records every
wake so operators can debug routing from the API.

Defaults:
- the captain ``self`` monitors every channel it belongs to, plus #general;
- every other agent monitors its joined channels (group membership and
  explicit subscriptions).

Wake policy:
- the captain wakes on an @mention, a message from the operator in a
  monitored channel, or a subscribed system event;
- other agents wake on an @mention by name.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .agent_registry import AgentRegistry, home_dir
from .chat import DEFAULT_CHANNEL, Chat, message_id, sanitize_channel
from .events import (
    APLOMADO_SCAN_COMPLETED,
    CHAT_MESSAGE,
    Event,
    EventBus,
)
from .groups import GroupStore
from .locking import file_lock

DEFAULT_COOLDOWN = 30.0
DEFAULT_BATCH_WINDOW = 2.0
SYSTEM_EVENT_TYPES = (
    APLOMADO_SCAN_COMPLETED,
    "aplomado.scan.failed",
    "scan.completed",
    "scan.failed",
)
_OPERATOR_NAMES = {"operator", "human", "user"}
_MENTION = re.compile(r"@([A-Za-z0-9_.-]+)")


def _safe(name: str) -> str:
    return "".join(
        c if c.isalnum() or c in "-_" else "_" for c in (name or "").strip().lower()
    )


def _agent_dir(home: Path | None, agent: str) -> Path:
    base = Path(home) if home else home_dir()
    d = base / "agents" / _safe(agent)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _eligible_channel(channel: str) -> bool:
    channel = sanitize_channel(channel)
    return not (channel.startswith("#dm-") or channel.startswith("#thoughts"))


def _mentions(text: str, name: str) -> bool:
    target = (name or "").strip().lower()
    if not target:
        return False
    return any(
        found.strip().lower() == target
        for found in _MENTION.findall(text or "")
    )


def _is_question(text: str) -> bool:
    stripped = (text or "").strip()
    if not stripped:
        return False
    return "?" in stripped or stripped.lower().startswith((
        "what ", "why ", "how ", "when ", "where ", "who ", "can ",
        "could ", "would ", "should ", "is ", "are ", "do ", "does ",
        "did ", "will ", "any ",
    ))


def write_wake_context(home: Path | None, agent: str, item: dict) -> None:
    """Append one monitor context item for an agent's next LLM turn."""
    path = _agent_dir(home, agent) / "monitor_wake.json"
    lock_path = (Path(home) if home else home_dir()) / ".monitor-wake.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path):
        data = {"items": [], "updated_at": time.time()}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict) and isinstance(loaded.get("items"), list):
                    data = loaded
            except (json.JSONDecodeError, OSError):
                pass
        data.setdefault("items", []).append(dict(item))
        data["items"] = data["items"][-50:]
        data["updated_at"] = time.time()
        tmp = path.with_suffix(f".json.{int(time.time() * 1e6)}.tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)


def take_wake_context(home: Path | None, agent: str) -> list[dict]:
    """Consume and return any monitor context queued for an agent."""
    path = _agent_dir(home, agent) / "monitor_wake.json"
    lock_path = (Path(home) if home else home_dir()) / ".monitor-wake.lock"
    if not path.exists():
        return []
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with file_lock(lock_path):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            data = {}
        try:
            path.unlink()
        except OSError:
            pass
    items = data.get("items") if isinstance(data, dict) else None
    return [item for item in (items or []) if isinstance(item, dict)]


def clear_wake_context(home: Path | None, agent: str) -> None:
    path = _agent_dir(home, agent) / "monitor_wake.json"
    try:
        path.unlink()
    except OSError:
        pass


@dataclass
class WakeRecord:
    agent: str
    channel: str
    trigger: str
    message_id: str
    at: float = field(default_factory=time.time)
    message_ids: list[str] = field(default_factory=list)
    author: str = ""
    reason: str = ""
    status: str = "woken"
    id: str = ""

    def to_dict(self) -> dict:
        value = asdict(self)
        value["id"] = self.id or f"{int(self.at * 1e6)}-{self.agent}-{self.channel}"
        value["message_ids"] = list(self.message_ids or [self.message_id])
        return value


class Monitor:
    """Service-level channel monitor and wake router."""

    def __init__(
        self,
        home: Path | None = None,
        chat: Chat | None = None,
        registry: AgentRegistry | None = None,
        daemon=None,
        groups: GroupStore | None = None,
        bus: EventBus | None = None,
        wake=None,
        *,
        clock=None,
        cooldown: float = DEFAULT_COOLDOWN,
        batch_window: float = DEFAULT_BATCH_WINDOW,
    ):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.chat = chat or Chat(home=self.home)
        self.registry = registry or AgentRegistry(home=self.home)
        self.groups = groups or GroupStore(home=self.home)
        self.daemon = daemon
        self.bus = bus
        self.wake_fn = wake or self._default_wake
        self.clock = clock or time.time
        self.cooldown = max(0.0, float(cooldown))
        self.batch_window = max(0.0, float(batch_window))
        self._state_file = self.home / "monitoring.json"
        self._log_file = self.home / "wake-log.jsonl"
        self._lock_path = self.home / ".monitoring.lock"
        self._lock = threading.RLock()
        self._state = self._load_state()
        self._last_wake: dict[tuple[str, str], float] = {}
        self._pending: dict[tuple[str, str], list[dict]] = {}
        self._unsubscribers: list = []
        self._event_types: set[str] = set()
        self._started = False

    # -- persistence ---------------------------------------------------
    def _load_state(self) -> dict:
        data: dict = {"agents": {}, "events": {}}
        if self._state_file.exists():
            try:
                raw = json.loads(self._state_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data["agents"] = raw.get("agents") or {}
                    data["events"] = raw.get("events") or {}
            except (json.JSONDecodeError, OSError):
                pass
        return data

    def _save_state(self) -> None:
        with self._lock, file_lock(self._lock_path):
            tmp = self._state_file.with_suffix(f".json.{int(time.time() * 1e6)}.tmp")
            tmp.write_text(json.dumps(self._state, indent=2, sort_keys=True),
                           encoding="utf-8")
            tmp.replace(self._state_file)

    # -- monitored channels -------------------------------------------
    def _subscriptions(self, agent: str) -> set[str]:
        try:
            from . import subscriptions
            data = subscriptions.get_subscriptions(agent, home=self.home)
            return set((data.get("channels") or {}).keys())
        except Exception:
            return set()

    def _group_channels(self, agent: str) -> set[str]:
        target = (agent or "").strip().lower()
        channels: set[str] = set()
        try:
            groups = self.groups.list()
        except Exception:
            groups = {}
        for group in groups.values():
            members = {
                str(member).strip().lower()
                for member in (group.get("members") or [])
            }
            if target in members:
                channel = group.get("channel")
                if channel:
                    channels.add(sanitize_channel(channel))
        return channels

    def monitored_channels(self, agent: str) -> list[str]:
        """The agent's effective monitored-channel set, computed live."""
        name = (agent or "").strip()
        if not name:
            return []
        key = name.lower()
        with self._lock:
            explicit = self._state.get("agents", {}).get(key)
        if explicit is not None:
            channels = set()
            for channel in explicit or []:
                try:
                    channel = sanitize_channel(channel)
                except ValueError:
                    continue
                if _eligible_channel(channel):
                    channels.add(channel)
        else:
            channels = self._group_channels(name) | self._subscriptions(name)
            if self._is_captain(name):
                channels.add(DEFAULT_CHANNEL)
        if self._is_captain(name):
            channels.add(DEFAULT_CHANNEL)
        return sorted(channels)

    def add_monitored(self, agent: str, channels: list[str]) -> list[str]:
        key = (agent or "").strip().lower()
        current = set(self.monitored_channels(agent))
        for channel in channels or []:
            try:
                channel = sanitize_channel(channel)
            except ValueError:
                continue
            if _eligible_channel(channel):
                current.add(channel)
        with self._lock:
            self._state.setdefault("agents", {})[key] = sorted(current)
        self._save_state()
        return self.monitored_channels(agent)

    def remove_monitored(self, agent: str, channels: list[str]) -> list[str]:
        key = (agent or "").strip().lower()
        current = set(self.monitored_channels(agent))
        for channel in channels or []:
            try:
                current.discard(sanitize_channel(channel))
            except ValueError:
                pass
        with self._lock:
            self._state.setdefault("agents", {})[key] = sorted(current)
        self._save_state()
        return self.monitored_channels(agent)

    def set_monitored(self, agent: str, channels: list[str]) -> list[str]:
        key = (agent or "").strip().lower()
        current: set[str] = set()
        for channel in channels or []:
            try:
                channel = sanitize_channel(channel)
            except ValueError:
                continue
            if _eligible_channel(channel):
                current.add(channel)
        with self._lock:
            self._state.setdefault("agents", {})[key] = sorted(current)
        self._save_state()
        return self.monitored_channels(agent)

    # -- event subscriptions ------------------------------------------
    def _is_captain(self, agent: str) -> bool:
        from .captain import is_captain
        return is_captain(agent)

    def event_subscriptions(self, agent: str) -> list[str]:
        key = (agent or "").strip().lower()
        with self._lock:
            explicit = self._state.get("events", {}).get(key)
        if explicit is not None:
            return sorted(str(item) for item in explicit or [] if str(item).strip())
        return sorted(SYSTEM_EVENT_TYPES) if self._is_captain(agent) else []

    def subscribe_events(self, agent: str, event_types: list[str]) -> list[str]:
        key = (agent or "").strip().lower()
        current = set(self.event_subscriptions(agent))
        added = {
            str(item).strip() for item in event_types or [] if str(item).strip()
        }
        current.update(added)
        with self._lock:
            self._state.setdefault("events", {})[key] = sorted(current)
        self._save_state()
        # A subscription configured after startup still needs a bus handler.
        for event_type in sorted(added):
            self._subscribe_event_type(event_type)
        return self.event_subscriptions(agent)

    def unsubscribe_events(self, agent: str, event_types: list[str]) -> list[str]:
        key = (agent or "").strip().lower()
        current = set(self.event_subscriptions(agent))
        current.difference_update(
            str(item).strip() for item in event_types or [] if str(item).strip()
        )
        with self._lock:
            self._state.setdefault("events", {})[key] = sorted(current)
        self._save_state()
        return self.event_subscriptions(agent)

    def config(self) -> dict:
        names = self._agent_names()
        return {
            "agents": {
                name: {
                    "channels": self.monitored_channels(name),
                    "events": self.event_subscriptions(name),
                }
                for name in names
            },
            "cooldown_seconds": self.cooldown,
            "batch_window_seconds": self.batch_window,
        }

    # -- lifecycle -----------------------------------------------------
    def _subscribe_event_type(self, event_type: str) -> None:
        if not self._started or self.bus is None or not event_type:
            return
        if event_type in self._event_types or event_type == CHAT_MESSAGE:
            return
        self._unsubscribers.append(self.bus.subscribe(
            event_type, self.handle_event,
            subscriber_id=f"quarterdeck.monitor:{event_type}",
        ))
        self._event_types.add(event_type)

    def start(self) -> None:
        if self.bus is None or self._started:
            return
        self._started = True
        self._unsubscribers.append(self.bus.subscribe(
            CHAT_MESSAGE, self.handle_event,
            subscriber_id="quarterdeck.monitor:chat",
        ))
        types = set(SYSTEM_EVENT_TYPES)
        for name in self._agent_names():
            types.update(self.event_subscriptions(name))
        for event_type in sorted(types):
            self._subscribe_event_type(event_type)

    def stop(self) -> None:
        for unsubscribe in self._unsubscribers:
            try:
                unsubscribe()
            except Exception:
                pass
        self._unsubscribers = []
        self._event_types.clear()
        self._started = False

    # -- routing -------------------------------------------------------
    def _agent_names(self) -> list[str]:
        names: set[str] = set()
        try:
            names.update(agent.name for agent in self.registry.list())
        except Exception:
            pass
        try:
            names.update(getattr(self.daemon, "runners", {}).keys())
        except Exception:
            pass
        with self._lock:
            names.update(self._state.get("agents", {}).keys())
        return sorted(name for name in names if name and name.lower() != "seed")

    def _is_agent(self, author: str) -> bool:
        target = (author or "").strip().lower()
        if not target:
            return False
        try:
            if self.registry.exists(author):
                return True
        except Exception:
            pass
        try:
            return any(str(name).lower() == target
                       for name in getattr(self.daemon, "runners", {}))
        except Exception:
            return False

    def _is_operator(self, author: str) -> bool:
        target = (author or "").strip().lower()
        if target in _OPERATOR_NAMES:
            return True
        return bool(target) and not self._is_agent(author)

    def _default_wake(self, agent: str, reason: str) -> bool:
        if self.daemon is None:
            return False
        try:
            return bool(self.daemon.wake_agent(agent, reason))
        except Exception:
            return False

    def handle_event(self, event) -> list[dict]:
        if isinstance(event, Event):
            if event.type == CHAT_MESSAGE:
                return self.observe(event.payload)
            return self._handle_system_event(event)
        if isinstance(event, dict):
            if event.get("type") and isinstance(event.get("payload"), dict):
                return self.handle_event(Event.from_dict(event))
            return self.observe(event)
        return []

    def observe_message(self, msg: dict) -> list[dict]:
        return self.observe(msg)

    def handle_message(self, msg: dict) -> list[dict]:
        return self.observe(msg)

    def process_message(self, msg: dict) -> list[dict]:
        return self.observe(msg)

    def observe(self, msg: dict) -> list[dict]:
        if not isinstance(msg, dict):
            return []
        try:
            channel = sanitize_channel(msg.get("channel") or "")
        except ValueError:
            return []
        if not _eligible_channel(channel):
            return []
        text = str(msg.get("text") or "")
        author = str(msg.get("author") or "").strip()
        if not text.strip() or not author:
            return []
        mid = message_id(msg)
        operator = self._is_operator(author)
        records: list[dict] = []
        for name in self._agent_names():
            if name.strip().lower() == author.lower():
                continue
            mentioned = _mentions(text, name)
            captain = self._is_captain(name)
            if captain:
                if mentioned:
                    trigger = "mention"
                elif operator:
                    trigger = "operator-question" if _is_question(text) else "operator-message"
                else:
                    continue
            else:
                if not mentioned:
                    continue
                trigger = "mention"
            if not mentioned and channel not in self.monitored_channels(name):
                continue
            record = self._dispatch(
                name, channel, trigger, mid,
                message_ids=[mid], author=author, text=text,
            )
            if record:
                records.append(record)
        return records

    def _handle_system_event(self, event: Event) -> list[dict]:
        channel = ""
        payload = event.payload or {}
        for value in (
            payload.get("channel"),
            (payload.get("data") or {}).get("channel") if isinstance(
                payload.get("data"), dict) else None,
        ):
            if value:
                try:
                    channel = sanitize_channel(value)
                except ValueError:
                    channel = ""
                break
        if not channel:
            channel = DEFAULT_CHANNEL
        text = json.dumps(payload, sort_keys=True)[:2000]
        records: list[dict] = []
        for name in self._agent_names():
            if event.type not in self.event_subscriptions(name):
                continue
            record = self._dispatch(
                name, channel, f"event:{event.type}", event.id,
                message_ids=[event.id], author=event.source, text=text,
            )
            if record:
                records.append(record)
        return records

    def _dispatch(self, agent: str, channel: str, trigger: str,
                  mid: str, *, message_ids: list[str], author: str,
                  text: str) -> dict | None:
        now = self.clock()
        key = (agent.strip().lower(), channel)
        with self._lock:
            last = self._last_wake.get(key)
            if last is not None and now - last < self.cooldown:
                item = {
                    "message_id": mid,
                    "author": author,
                    "text": text[:500],
                    "trigger": trigger,
                    "at": now,
                }
                self._pending.setdefault(key, []).append(item)
                write_wake_context(self.home, agent, {
                    "channel": channel,
                    "trigger": trigger,
                    "message_id": mid,
                    "message_ids": [mid],
                    "author": author,
                    "text": text[:2000],
                    "at": now,
                    "batched": True,
                })
                return None
            queued = self._pending.pop(key, [])
            self._last_wake[key] = now
            all_ids = list(message_ids)
            for item in queued:
                item_id = item.get("message_id")
                if item_id and item_id not in all_ids:
                    all_ids.append(item_id)

        reason = f"monitor:{trigger}"
        item = {
            "channel": channel,
            "trigger": trigger,
            "message_id": mid,
            "message_ids": all_ids,
            "author": author,
            "text": text[:2000],
            "at": now,
        }
        write_wake_context(self.home, agent, item)
        try:
            woke = self.wake_fn(agent, reason)
        except Exception:
            woke = False
        if woke is False:
            clear_wake_context(self.home, agent)

        record = WakeRecord(
            agent=agent,
            channel=channel,
            trigger=trigger,
            message_id=mid,
            at=now,
            message_ids=all_ids,
            author=author,
            reason=reason,
            status="woken" if woke is not False else "wake-failed",
        )
        value = record.to_dict()
        with self._lock, file_lock(self._lock_path):
            with open(self._log_file, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(value, sort_keys=True) + "\n")
                handle.flush()
        return value

    # -- wake history --------------------------------------------------
    def wakes(self, agent: str | None = None, channel: str | None = None,
              limit: int = 100) -> list[dict]:
        if not self._log_file.exists() or limit <= 0:
            return []
        target_agent = (agent or "").strip().lower()
        target_channel = None
        if channel:
            try:
                target_channel = sanitize_channel(channel)
            except ValueError:
                target_channel = channel
        with self._lock:
            lines = self._log_file.read_text(encoding="utf-8").splitlines()
        out: list[dict] = []
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(record, dict):
                continue
            if target_agent and str(record.get("agent", "")).lower() != target_agent:
                continue
            if target_channel and record.get("channel") != target_channel:
                continue
            out.append(record)
        return out[-limit:]

    def wake_log(self, *args, **kwargs) -> list[dict]:
        return self.wakes(*args, **kwargs)

    @property
    def log(self) -> list[dict]:
        return self.wakes()

    def clear_wakes(self) -> None:
        with self._lock, file_lock(self._lock_path):
            try:
                self._log_file.unlink()
            except OSError:
                pass


ChannelMonitor = Monitor

Monitor.handle = Monitor.handle_event
Monitor.wake_history = Monitor.wakes
