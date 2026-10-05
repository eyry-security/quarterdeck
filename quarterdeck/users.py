"""User profiles: stable human identity for Quarterdeck.

When someone first joins, they pick a unique username. That username becomes
their inherent identity: it's the `author` on every message they send, and
agents can remember users by name and distinguish one person from another.

Profiles live at ~/.quarterdeck/users/<username>/profile.json (or
$QUARTERDECK_HOME). No passwords — just unique usernames. This is the
foundation for multi-user support later.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from .agent_registry import home_dir
from .locking import file_lock

# Usernames: 2-32 chars, letters/digits plus _ . - (same spirit as channels).
_VALID = re.compile(r"^[A-Za-z0-9_.-]{2,32}$")
# Reserved: can't collide with agent names or system authors.
_RESERVED = {"seed", "orchestrator", "system", "admin", "quarterdeck"}


class UserError(ValueError):
    pass


def normalize_username(name: str) -> str:
    """Validate and canonicalize a username. Raises UserError."""
    name = (name or "").strip()
    if not _VALID.match(name):
        raise UserError(
            "username must be 2-32 chars: letters, digits, _ . -"
        )
    return name


@dataclass
class UserProfile:
    username: str
    created_at: float = 0.0
    # Optional display color for the UI (hex). Assigned on registration.
    color: str = ""
    # Freeform; agents may read this to know who they're talking to.
    note: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict) -> "UserProfile":
        return cls(
            username=value["username"],
            created_at=value.get("created_at", 0.0),
            color=value.get("color", ""),
            note=value.get("note", ""),
        )


# A small palette so different users get distinct colors in the UI.
_PALETTE = [
    "#7aa2f7", "#bb9af7", "#7dcfff", "#9ece6a", "#e0af68",
    "#f7768e", "#73daca", "#ff9e64", "#c0caf5", "#9aa5ce",
]


def _color_for(username: str) -> str:
    import hashlib

    h = int(hashlib.sha256(username.lower().encode()).hexdigest(), 16)
    return _PALETTE[h % len(_PALETTE)]


class UserRegistry:
    """Process-safe user profile registry."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else home_dir()
        self._dir = self.home / "users"
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, username: str) -> Path:
        key = username.lower()
        return self._dir / key / "profile.json"

    def _agent_names(self) -> set[str]:
        """Agent names are reserved so authors are unambiguous."""
        try:
            from .agent_registry import AgentRegistry

            return {a.name.lower() for a in AgentRegistry(home=self.home).list()}
        except Exception:
            return set()

    def exists(self, username: str) -> bool:
        try:
            name = normalize_username(username)
        except UserError:
            return False
        return self._path(name).exists()

    def get(self, username: str) -> UserProfile | None:
        try:
            name = normalize_username(username)
        except UserError:
            return None
        p = self._path(name)
        if not p.exists():
            return None
        try:
            return UserProfile.from_dict(
                json.loads(p.read_text(encoding="utf-8"))
            )
        except (json.JSONDecodeError, OSError, KeyError, TypeError):
            return None

    def list(self) -> list[UserProfile]:
        out = []
        for d in sorted(self._dir.iterdir()):
            if not d.is_dir():
                continue
            p = d / "profile.json"
            if not p.exists():
                continue
            try:
                out.append(
                    UserProfile.from_dict(
                        json.loads(p.read_text(encoding="utf-8"))
                    )
                )
            except (json.JSONDecodeError, OSError, KeyError, TypeError):
                continue
        return out

    def register(self, username: str, note: str = "") -> UserProfile:
        """Create a profile. Raises UserError if invalid or taken."""
        name = normalize_username(username)
        key = name.lower()
        if key in _RESERVED or key in self._agent_names():
            raise UserError(f"username {name!r} is reserved")
        with self._lock:
            p = self._path(name)
            if p.exists():
                raise UserError(f"username {name!r} is already taken")
            profile = UserProfile(
                username=name,
                created_at=time.time(),
                color=_color_for(name),
                note=note or "",
            )
            lock_path = self._dir / ".users.lock"
            with file_lock(lock_path):
                if p.exists():
                    raise UserError(f"username {name!r} is already taken")
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(profile.to_dict(), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                tmp.replace(p)
            return profile

    def ensure(self, username: str) -> UserProfile:
        """Get the profile, auto-registering if it doesn't exist.

        Used on message post so CLI/API clients don't need an explicit
        registration step. Raises UserError for invalid/reserved names.
        """
        existing = self.get(username)
        if existing is not None:
            return existing
        return self.register(username)

    def update_note(self, username: str, note: str) -> UserProfile:
        """Update a user's note. Raises UserError if missing."""
        name = normalize_username(username)
        with self._lock:
            profile = self.get(name)
            if profile is None:
                raise UserError(f"unknown user {name!r}")
            profile.note = note or ""
            p = self._path(name)
            lock_path = self._dir / ".users.lock"
            with file_lock(lock_path):
                tmp = p.with_suffix(".json.tmp")
                tmp.write_text(
                    json.dumps(profile.to_dict(), indent=2, sort_keys=True),
                    encoding="utf-8",
                )
                tmp.replace(p)
            return profile
