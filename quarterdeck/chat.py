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


def sanitize_channel(channel: str) -> str:
    name = channel.lstrip("#").strip()
    if not name:
        raise ValueError("channel name must not be empty")
    return "#" + _SAFE.sub("_", name)


class Chat:
    def __init__(self, home: Path | None = None, bus: EventBus | None = None):
        self.home = Path(home) if home else home_dir()
        self.dir = self.home / "chat"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.bus = bus
        self._lock = Lock()

    def _file(self, channel: str) -> Path:
        return self.dir / (sanitize_channel(channel).lstrip("#") + ".jsonl")

    def post(self, channel: str, author: str, text: str) -> dict:
        """Append a message to a channel. Returns the stored record."""
        channel = sanitize_channel(channel)
        if not author or not author.strip():
            raise ValueError("author must not be empty")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("message text must not be empty")
        if len(text) > 20_000:
            raise ValueError("message text exceeds 20000 characters")
        msg = {
            "id": str(uuid.uuid4()),
            "ts": time.time(),
            "channel": channel,
            "author": author,
            "text": text,
        }
        with self._lock:
            with open(self._file(channel), "a", encoding="utf-8") as f:
                f.write(json.dumps(msg) + "\n")
        if self.bus is not None:
            self.bus.publish(Event(type=CHAT_MESSAGE, payload=dict(msg)))
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
