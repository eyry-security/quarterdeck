"""Per-agent persistent files: memory.md, runbook.md, identify.md.

Each named agent gets a directory under ~/.quarterdeck/agents/<name>/
holding exactly these three files. Agents read/write them for persistent
memory across wakes; the webchat UI surfaces them.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

from .agent_registry import home_dir

# Exact filenames per Ben's spec — do not rename.
MEMORY_MD = "memory.md"
RUNBOOK_MD = "runbook.md"
IDENTIFY_MD = "identify.md"
AGENT_FILES = (MEMORY_MD, RUNBOOK_MD, IDENTIFY_MD)

_SAFE_NAME = re.compile(r"[^a-zA-Z0-9_.-]")
MAX_FILE_BYTES = 200_000  # 200KB cap per file


def _safe_agent(name: str) -> str:
    clean = _SAFE_NAME.sub("_", name.strip())
    if not clean:
        raise ValueError("agent name must not be empty")
    return clean


class AgentFiles:
    """Read/write per-agent persistent markdown files."""

    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else home_dir()
        self.dir = self.home / "agents"
        self.dir.mkdir(parents=True, exist_ok=True)

    def _agent_dir(self, name: str) -> Path:
        d = self.dir / _safe_agent(name)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _path(self, name: str, filename: str) -> Path:
        if filename not in AGENT_FILES:
            raise ValueError(f"unknown agent file: {filename!r} (want one of {AGENT_FILES})")
        return self._agent_dir(name) / filename

    def read(self, name: str, filename: str) -> str:
        """Return file contents, or empty string if never written."""
        p = self._path(name, filename)
        if not p.exists():
            return ""
        return p.read_text(encoding="utf-8")

    def write(self, name: str, filename: str, content: str) -> dict:
        """Overwrite a file. Returns metadata about the write."""
        if not isinstance(content, str):
            raise ValueError("content must be a string")
        if len(content.encode("utf-8")) > MAX_FILE_BYTES:
            raise ValueError(f"content exceeds {MAX_FILE_BYTES} bytes")
        p = self._path(name, filename)
        p.write_text(content, encoding="utf-8")
        return {
            "agent": name,
            "file": filename,
            "bytes": len(content.encode("utf-8")),
            "updated_at": time.time(),
        }

    def all(self, name: str) -> dict[str, str]:
        """Return all three files as a dict."""
        return {f: self.read(name, f) for f in AGENT_FILES}

    def agents(self) -> list[str]:
        """Names of agents that have a files directory."""
        return sorted(p.name for p in self.dir.iterdir() if p.is_dir())
