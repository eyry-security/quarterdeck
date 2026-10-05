"""IRC-style shared chat for agents and humans.

One append-only JSONL log per channel under ~/.quarterdeck/chat/ (or
$QUARTERDECK_HOME). Channels are named like #general (the default).
"""

from __future__ import annotations

import json
import re
import time
import uuid
from pathlib import Path
from threading import Lock

from .agent_registry import home_dir
from .events import CHAT_MESSAGE, Event, EventBus

DEFAULT_CHANNEL = "#general"

_SAFE = re.compile(r"[^a-zA-Z0-9_.-]")

# Process-wide post listeners: fn(channel, msg) called synchronously right
# after every Chat.post() store, on whatever thread did the post. This is how
# the web server's websocket layer learns about messages written by the agent
# daemon (which runs in its own thread with its own Chat instance).
_post_listeners: list = []
_post_listeners_lock = Lock()


def add_post_listener(fn) -> None:
    "Register fn(channel, msg); called after every Chat.post store."
    with _post_listeners_lock:
        if fn not in _post_listeners:
            _post_listeners.append(fn)


def remove_post_listener(fn) -> None:
    with _post_listeners_lock:
        if fn in _post_listeners:
            _post_listeners.remove(fn)


def _notify_post(channel: str, msg: dict) -> None:
    with _post_listeners_lock:
        fns = list(_post_listeners)
    for fn in fns:
        try:
            fn(channel, msg)
        except Exception:
            pass


def sanitize_channel(channel: str) -> str:
    name = channel.lstrip("#").strip()
    if not name:
        raise ValueError("channel name must not be empty")
    return "#" + _SAFE.sub("_", name)


def new_message_id(ts: float | None = None) -> str:
    """Unique, lexicographically time-ordered message ID.

    Format: <microsecond-timestamp>-<12 hex chars>. String comparison gives
    chronological order; the random suffix breaks ties within a microsecond.
    """
    us = int((ts if ts is not None else time.time()) * 1e6)
    return f"{us:020d}-{uuid.uuid4().hex[:12]}"


def message_id(msg: dict) -> str:
    """Stable ID for a message, synthesizing one for pre-ID records."""
    mid = msg.get("id")
    if isinstance(mid, str) and mid:
        return mid
    # Pre-ID record: deterministic from its timestamp. Sorts before any
    # real ID from the same microsecond (zero suffix < any hex).
    us = int(float(msg.get("ts", 0) or 0) * 1e6)
    return f"{us:020d}-{'0' * 12}"


class Chat:
    def __init__(self, home: Path | None = None, bus: EventBus | None = None):
        self.home = Path(home) if home else home_dir()
        self.dir = self.home / "chat"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bus = bus
        self._lock = Lock()

    def _file(self, channel: str) -> Path:
        return self.dir / (sanitize_channel(channel).lstrip("#") + ".jsonl")

    def post(self, channel: str, author: str, text: str,
             cid: str | None = None) -> dict:
        """Append a message to a channel. Returns the stored record."""
        channel = sanitize_channel(channel)
        if not author or not author.strip():
            raise ValueError("author must not be empty")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text must not be empty")
        if len(text) > 20_000:
            raise ValueError("message text exceeds 20000 characters")
        ts = time.time()
        msg: dict = {
            "id": new_message_id(ts),
            "ts": ts,
            "channel": channel,
            "author": author,
            "text": text,
        }
        if cid:
            msg["cid"] = cid
        with self._lock:
            with open(self._file(channel), "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
        if self.bus is not None:
            self.bus.publish(Event(type=CHAT_MESSAGE, payload=dict(msg)))
        _notify_post(channel, msg)
        return msg

    def history(self, channel: str, n: int = 50) -> list[dict]:
        """Last n messages in a channel, oldest first. Empty list if the
        channel has no log yet."""
        path = self._file(channel)
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        out: list[dict] = []
        for line in lines[-n:]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue  # skip corrupt lines rather than crash
        return out

    def channels(self) -> list[str]:
        return sorted("#" + p.stem for p in self.dir.glob("*.jsonl"))

    def after(self, channel: str, last_id: str | None, n: int = 200) -> list[dict]:
        """Messages in channel after last_id (exclusive), oldest first.

        Deterministic: compares stable message IDs, no clock math.
        None/empty last_id means "everything".
        """
        msgs = self.history(channel, n=n)
        if not last_id:
            return msgs
        out = []
        for m in msgs:
            try:
                if message_id(m) > last_id:
                    out.append(m)
            except Exception:
                continue
        return out
