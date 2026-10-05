"""Persistent per-agent channel subscriptions with mention tracking."""
from __future__ import annotations

import json
import re
import time
from pathlib import Path


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
    """Check subscribed channels for new @mentions and keyword hits.

    Returns unread mentions since last check, then updates the watermark.
    """
    data = _load(home, agent)
    since = data.get("last_check", 0.0)
    now = time.time()
    agent_lc = agent.strip().lower()
    mention_pat = re.compile(rf"@{re.escape(agent_lc)}\b", re.IGNORECASE)

    hits = []
    for channel, sub in data["channels"].items():
        try:
            msgs = chat.history(channel, n=200)
        except Exception:
            continue
        for m in msgs:
            if m.get("ts", 0) <= since:
                continue
            # Don't notify about own messages
            if str(m.get("author", "")).strip().lower() == agent_lc:
                continue
            text = str(m.get("text", ""))
            matched = mention_pat.search(text) is not None
            # Keyword filters
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
                })

    data["last_check"] = now
    _save(home, agent, data)
    hits.sort(key=lambda h: h["ts"])
    return {"agent": agent, "unread": hits, "count": len(hits)}
