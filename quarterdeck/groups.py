"""Group-chat metadata: membership, archive state, and read cursors.

Quarterdeck channels are append-only message logs. A ``GroupStore`` layers
the conversation-list concerns on top of them without changing the log
format: which agents belong to a group, which conversations the operator has
archived, and how far they have read.

State lives in ``$QUARTERDECK_HOME/groups.json``::

    {
      "groups": {"#ops": {"channel": "#ops", "name": "ops",
                           "members": ["self", "scout"],
                           "created_at": 0.0, "created_by": "operator",
                           "archived": false}},
      "archived": ["#old-chat"],
      "reads": {"#ops": "<last-read-message-id>"}
    }
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from .agent_registry import home_dir
from .chat import sanitize_channel

CAPTAIN = "self"
GENERAL = "#general"


def _clean_members(members: list[str] | None) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in members or []:
        name = str(value or "").strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


class GroupStore:
    """Process-safe group membership/archive/read state."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "groups.json"
        self._lock = threading.RLock()
        self._data: dict | None = None

    # -- persistence ---------------------------------------------------
    def _load(self) -> dict:
        if self._data is not None:
            return self._data
        data: dict = {"groups": {}, "archived": [], "reads": {}}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data["groups"] = raw.get("groups") or {}
                    data["archived"] = raw.get("archived") or []
                    data["reads"] = raw.get("reads") or {}
            except (json.JSONDecodeError, OSError):
                pass
        self._data = data
        return data

    def _save(self, data: dict) -> None:
        tmp = self.path.with_suffix(f".json.{os.getpid()}.tmp")
        payload = json.dumps(data, indent=2, sort_keys=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        tmp.replace(self.path)

    def _reload(self) -> dict:
        self._data = None
        return self._load()

    # -- groups --------------------------------------------------------
    def list(self) -> dict[str, dict]:
        with self._lock:
            data = self._reload()
            return {k: dict(v) for k, v in data["groups"].items()}

    def channels(self) -> list[str]:
        return sorted(self.list().keys())

    def get(self, channel: str) -> dict | None:
        channel = sanitize_channel(channel)
        with self._lock:
            group = self._load()["groups"].get(channel)
            return dict(group) if group else None

    def is_group(self, channel: str) -> bool:
        return self.get(channel) is not None

    def members(self, channel: str) -> list[str]:
        group = self.get(channel)
        return list(group.get("members", [])) if group else []

    def create(self, name: str, members: list[str] | None = None, *,
               created_by: str = "operator", taken: set[str] | None = None) -> dict:
        display = str(name or "").strip().lstrip("#").strip()
        if not display:
            raise ValueError("group name must not be empty")
        with self._lock:
            data = self._reload()
            base = sanitize_channel(display)
            taken_names = {c.lower() for c in (taken or set())}
            taken_names.update(c.lower() for c in data["groups"])
            channel = base
            suffix = 2
            while channel.lower() in taken_names:
                channel = f"{base}-{suffix}"
                suffix += 1
            group = {
                "channel": channel,
                "name": display,
                "members": _clean_members(members),
                "created_at": time.time(),
                "created_by": created_by,
                "archived": False,
            }
            data["groups"][channel] = group
            self._save(data)
            return dict(group)

    def update(self, channel: str, *, name: str | None = None,
               members: list[str] | None = None,
               archived: bool | None = None) -> dict:
        channel = sanitize_channel(channel)
        with self._lock:
            data = self._reload()
            group = data["groups"].get(channel)
            if group is None:
                raise KeyError(f"no group at {channel}")
            if name is not None:
                display = str(name).strip().lstrip("#").strip()
                if not display:
                    raise ValueError("group name must not be empty")
                group["name"] = display
            if members is not None:
                group["members"] = _clean_members(members)
            if archived is not None:
                group["archived"] = bool(archived)
            self._save(data)
            return dict(group)

    def add_members(self, channel: str, members: list[str]) -> dict:
        group = self.get(channel)
        current = list(group.get("members", [])) if group else []
        return self.update(channel, members=current + list(members))

    def remove_member(self, channel: str, member: str) -> dict:
        group = self.get(channel)
        if group is None:
            raise KeyError(f"no group at {channel}")
        key = str(member or "").strip().lower()
        kept = [m for m in group.get("members", []) if m.lower() != key]
        return self.update(channel, members=kept)

    # -- archive -------------------------------------------------------
    def is_archived(self, channel: str) -> bool:
        channel = sanitize_channel(channel)
        with self._lock:
            return any(c.lower() == channel.lower()
                       for c in self._load().get("archived", []))

    def archive(self, channel: str, archived: bool = True) -> None:
        channel = sanitize_channel(channel)
        with self._lock:
            data = self._reload()
            kept = [c for c in data.get("archived", [])
                    if c.lower() != channel.lower()]
            if archived:
                kept.append(channel)
            data["archived"] = kept
            self._save(data)

    # -- read cursors --------------------------------------------------
    def last_read(self, channel: str) -> str | None:
        channel = sanitize_channel(channel)
        with self._lock:
            return self._load().get("reads", {}).get(channel)

    def mark_read(self, channel: str, last_id: str | None) -> None:
        channel = sanitize_channel(channel)
        with self._lock:
            data = self._reload()
            reads = data.setdefault("reads", {})
            if last_id:
                current = reads.get(channel)
                if current is None or last_id > current:
                    reads[channel] = last_id
            else:
                reads.pop(channel, None)
            self._save(data)


def agent_color(name: str) -> str:
    """Stable gradient for an agent's initial avatar."""
    import hashlib

    digest = hashlib.sha1((name or "?").encode("utf-8")).hexdigest()
    hue = int(digest[:6], 16) % 360
    hue2 = (hue + 42) % 360
    return f"linear-gradient(135deg, hsl({hue}, 72%, 58%), hsl({hue2}, 68%, 46%))"


def agent_initial(name: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", " ", str(name or "")).strip()
    if not text:
        return "?"
    parts = text.split()
    if len(parts) > 1:
        return (parts[0][0] + parts[1][0]).upper()
    return parts[0][0].upper()


def is_captain_name(name: str | None) -> bool:
    return str(name or "").strip().lower() == CAPTAIN


def _is_operator(name: str | None) -> bool:
    return str(name or "").strip().lower() in {"operator", "human"}
