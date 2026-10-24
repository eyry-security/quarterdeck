"""Persistent per-agent channel subscriptions with mention tracking."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

from .chat import message_id


def _agent_dir(home: Path | None, agent: str) -> Path:
    base = Path(home) if home else Path.home() / ".quarterdeck"
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in agent.strip().lower())
    d = base / "agents" / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _subs_file(home: Path | None, agent: str) -> Path:
    return _agent_dir(home, agent) / "subscriptions.json"


def _load(home: Path | None, agent: str) -> dict:
    f = _subs_file(home, agent)
    if f.exists():
        try:
            return json.loads(f.read_text())
        except (json.JSONDecodeError, OSError):
            pass
    return {"channels": {}, "last_check": 0.0}


def _save(home: Path | None, agent: str, data: dict) -> None:
    _subs_file(home, agent).write_text(json.dumps(data, indent=2))


def _cursor_for(data: dict, channel: str, chat, old_ts_key: str,
                cursor_key: str) -> str | None:
    """ID cursor for one independent reader, migrating legacy watermarks.

    Mentions and channel activity each own a cursor map. On first run after
    upgrade, the old shared ``read_cursors`` map (or an even older timestamp
    watermark) is copied into the new map so nothing is re-read.
    """
    cursors = data.setdefault(cursor_key, {})
    if channel in cursors:
        return cursors[channel]
    legacy = data.get("read_cursors", {})
    if channel in legacy:
        cursors[channel] = legacy[channel]
        return legacy[channel]
    old_ts = data.get(old_ts_key, 0.0) or 0.0
    if old_ts:
        latest_id = None
        try:
            for m in chat.history(channel, n=500):
                if float(m.get("ts", 0) or 0) <= float(old_ts):
                    mid = message_id(m)
                    if latest_id is None or mid > latest_id:
                        latest_id = mid
        except Exception:
            pass
        if latest_id:
            cursors[channel] = latest_id
            return latest_id
    return None


def _advance_cursor(data: dict, channel: str, messages: list[dict],
                    cursor_key: str) -> None:
    cursors = data.setdefault(cursor_key, {})
    latest = cursors.get(channel)
    for m in messages:
        try:
            mid = message_id(m)
        except Exception:
            continue
        if latest is None or mid > latest:
            latest = mid
    if latest:
        cursors[channel] = latest


def subscribe(agent: str, channels: list[str], filters: list[str] | None = None,
              home: Path | None = None) -> dict:
    """Subscribe an agent to channels with optional keyword filters."""
    data = _load(home, agent)
    for ch in channels:
        ch = ch.strip()
        if not ch:
            continue
        if not ch.startswith("#"):
            ch = "#" + ch
        data["channels"][ch] = {"filters": filters or []}
    _save(home, agent, data)
    return {"subscribed": sorted(data["channels"].keys())}


def unsubscribe(agent: str, channels: list[str],
                home: Path | None = None) -> dict:
    """Remove channel subscriptions for an agent."""
    data = _load(home, agent)
    removed = []
    for ch in channels:
        ch = ch.strip()
        if not ch.startswith("#"):
            ch = "#" + ch
        if ch in data["channels"]:
            del data["channels"][ch]
            removed.append(ch)
    _save(home, agent, data)
    return {"unsubscribed": removed, "remaining": sorted(data["channels"].keys())}


def get_subscriptions(agent: str, home: Path | None = None) -> dict:
    """Return an agent's current subscriptions."""
    data = _load(home, agent)
    return {"agent": agent, "channels": data["channels"]}


def check_mentions(agent: str, chat, home: Path | None = None) -> dict:
    """Check for new @mentions (any channel) and keyword hits (subscriptions).

    An @mention is addressed to the agent explicitly, so it is delivered even
    when the agent is not subscribed to that channel. Keyword filters still
    apply only to subscribed channels. The mention cursor is independent of
    the activity cursor, so neither reader can consume the other's messages.
    """
    data = _load(home, agent)
    agent_lc = agent.strip().lower()
    mention_pat = re.compile(rf"@{re.escape(agent_lc)}\b", re.IGNORECASE)
    subscribed = data["channels"]

    try:
        channels = set(chat.channels())
    except Exception:
        channels = set()
    # Mentions are explicit and channel-independent; keywords require a sub.
    channels.update(subscribed)

    hits = []
    for channel in sorted(channels):
        sub = subscribed.get(channel, {})
        cursor = _cursor_for(data, channel, chat, "last_check",
                             "mention_cursors")
        try:
            msgs = chat.after(channel, cursor, n=200)
        except Exception:
            continue
        for m in msgs:
            # Don't notify about own messages
            if str(m.get("author", "")).strip().lower() == agent_lc:
                continue
            text = str(m.get("text", ""))
            matched = mention_pat.search(text) is not None
            # Keyword filters apply only where the agent subscribed.
            if channel in subscribed:
                for kw in sub.get("filters", []):
                    if kw.lower() in text.lower():
                        matched = True
                        break
            if matched:
                hits.append({
                    "channel": channel,
                    "author": m.get("author"),
                    "text": text[:500],
                    "ts": m.get("ts"),
                    "id": message_id(m),
                })
        _advance_cursor(data, channel, msgs, "mention_cursors")

    _save(home, agent, data)
    hits.sort(key=lambda h: (h["ts"], h["id"]))
    return {"agent": agent, "unread": hits, "count": len(hits)}


def check_activity(agent: str, chat, home: Path | None = None) -> dict:
    """Return ALL new messages in subscribed channels (not just mentions).

    Separate watermark (last_activity_check) from check_mentions so the two
    can run independently. Own messages are excluded.
    """
    data = _load(home, agent)
    agent_lc = agent.strip().lower()

    msgs_out = []
    for channel in data["channels"].keys():
        cursor = _cursor_for(data, channel, chat, "last_activity_check",
                             "activity_cursors")
        try:
            msgs = chat.after(channel, cursor, n=200)
        except Exception:
            continue
        for m in msgs:
            if str(m.get("author", "")).strip().lower() == agent_lc:
                continue
            text = str(m.get("text", ""))
            if not text.strip():
                continue
            msgs_out.append({
                "channel": channel,
                "author": m.get("author"),
                "text": text[:500],
                "ts": m.get("ts"),
                "id": message_id(m),
            })
        _advance_cursor(data, channel, msgs, "activity_cursors")

    _save(home, agent, data)
    msgs_out.sort(key=lambda h: (h["ts"], h["id"]))
    return {"agent": agent, "messages": msgs_out, "count": len(msgs_out)}
