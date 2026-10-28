"""Build the iMessage-style conversation list from chat logs and groups."""

from __future__ import annotations

from . import groups as groups_mod
from .chat import message_id


def _is_operator(name: str | None) -> bool:
    return str(name or "").strip().lower() in {"operator", "human"}


def _is_system(name: str | None) -> bool:
    return str(name or "").strip().lower() in {"", "system"}


def _preview(last: dict | None) -> str:
    if not last:
        return ""
    text = str(last.get("text", "") or "")
    if text.startswith("(dm) "):
        text = text[5:]
    text = " ".join(text.split())
    if len(text) > 140:
        text = text[:139] + "\u2026"
    author = str(last.get("author", "") or "")
    if _is_operator(author) or _is_system(author):
        return text
    return f"{author}: {text}" if text else author


def _member_payload(name: str, agent_map: dict[str, dict]) -> dict:
    info = agent_map.get(name.lower(), {})
    presence = info.get("presence", "offline")
    return {
        "name": name,
        "initial": groups_mod.agent_initial(name),
        "color": groups_mod.agent_color(name),
        "presence": presence,
        "thinking": bool(info.get("thinking", False)),
        "captain": groups_mod.is_captain_name(name),
        "model": info.get("model"),
    }


def build_conversations(*, chat, groups, agents: list[dict] | None = None,
                        include_archived: bool = False) -> list[dict]:
    """Return conversation rows, pinned captain chat first.

    Each row is a channel/group with its member avatars, last-message preview,
    relative-time timestamp, and unread count (tracked per channel in
    ``GroupStore``). Thought streams are never conversations.
    """
    agent_map = {
        str(a.get("name", "")).strip().lower(): a
        for a in (agents or [])
        if a.get("name")
    }
    channels = set(chat.channels())
    channels.update(groups.channels())
    channels.add(groups_mod.GENERAL)

    rows: list[dict] = []
    for channel in channels:
        if channel.startswith("#thoughts-"):
            continue
        if not include_archived and groups.is_archived(channel):
            continue
        group = groups.get(channel)
        members = list(group.get("members", [])) if group else []
        if group is None and channel.startswith("#dm-"):
            # A direct chat's participants are encoded in the channel name.
            members = [p for p in channel[4:].split("-")
                       if p and not _is_operator(p)]
        if channel == groups_mod.GENERAL and not any(
                m.lower() == groups_mod.CAPTAIN for m in members):
            members.insert(0, groups_mod.CAPTAIN)
        # Deduplicate case-insensitively while preserving order.
        seen: set[str] = set()
        clean_members: list[str] = []
        for name in members:
            key = str(name).strip().lower()
            if key and key not in seen:
                seen.add(key)
                clean_members.append(str(name).strip())

        history = chat.history(channel, n=1)
        last = history[-1] if history else None
        last_read = groups.last_read(channel)
        unread = 0
        for msg in chat.after(channel, last_read, n=200):
            author = msg.get("author")
            if _is_operator(author) or _is_system(author):
                continue
            unread += 1

        pinned = channel == groups_mod.GENERAL
        title = group.get("name") if group else channel.lstrip("#")
        if group is None and channel.startswith("#dm-") and len(clean_members) == 1:
            title = clean_members[0]
        if pinned and not group:
            title = "self \u00b7 Captain"
        rows.append({
            "channel": channel,
            "name": channel.lstrip("#"),
            "title": title,
            "pinned": pinned,
            "captain": pinned,
            "group": group is not None,
            "archived": groups.is_archived(channel),
            "members": [_member_payload(name, agent_map)
                        for name in clean_members],
            "last_message": ({
                "id": message_id(last),
                "ts": last.get("ts"),
                "author": last.get("author"),
                "text": str(last.get("text", "") or "")[5:]
                        if str(last.get("text", "")).startswith("(dm) ")
                        else last.get("text"),
                "agent": not _is_operator(last.get("author")),
            } if last else None),
            "preview": _preview(last),
            "last_ts": (last.get("ts") if last else None)
                       or (group.get("created_at") if group else 0),
            "unread": unread,
        })

    rows.sort(key=lambda row: (
        0 if row["pinned"] else 1,
        -(float(row.get("last_ts") or 0)),
        row["channel"],
    ))
    return rows
