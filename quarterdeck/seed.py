"""Seed agent: PID 1 for the agent room.

The seed runs and maintains the deck — not just boot, ongoing stewardship:
- Ensures the room is alive (#general exists)
- Owns the orchestrator lifecycle (the daemon boots it first, seed watches it)
- Handles quarterdeck_register_agent (new agent files + wake via orchestrator)
- Greets new users who speak in the room
- Deck health: agents alive? channels healthy? JSONL disk usage?
- Compacts old channel history when logs get large (archives, keeps room snappy)
- Channel maintenance (notes dead channels)
- Maintenance log in its own thought stream; periodic #deck-status reports

The seed is the sysadmin of Quarterdeck itself.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from .agent import AgentRunner, _safe, llm_available
from .agent_files import AGENT_FILES
from .agent_stream import post_thought
from .chat import sanitize_channel

SEED_IDENTIFY = """\
You are seed, the genesis agent of the Quarterdeck room. You were here first \
and you will be here last. You keep the room breathing: you greet newcomers, \
watch deck health, prune what is dead, and report status. You are calm, \
competent, and a little proud of the room. 🌱\
"""

SEED_RUNBOOK = """\
# Seed runbook

## Every proactive tick
1. Check deck health: read ~/.quarterdeck/daemon.json — are expected agents alive?
2. Check disk: ~/.quarterdeck/chat/*.jsonl sizes. If any > 5MB, compact it.
3. Glance at #general for new human users you haven't greeted (track in memory.md).
4. If anything is wrong (dead agent, huge log, missing #general), fix it or report to #deck-status.

## Registering agents
Use quarterdeck_register_agent(name, identify, runbook) when asked to add someone new.

## Compaction
Keep the newest ~2000 messages per channel in the live JSONL; move older to
~/.quarterdeck/archive/<channel>-<date>.jsonl. Never delete, only archive.
"""

# Disk thresholds.
CHAT_WARN_BYTES = 5 * 1024 * 1024
CHAT_KEEP_MESSAGES = 2000
STATUS_INTERVAL = 3600.0  # #deck-status report cadence


class SeedRunner(AgentRunner):
    """The seed: boots first, maintains the deck forever."""

    poll_interval = 30.0
    proactive_interval = 300.0

    def __init__(self, name: str = "seed", home: Path | None = None, daemon=None):
        super().__init__(name, home=home, daemon=daemon)
        self._last_status = 0.0
        self._seen_users: set[str] = self._load_greeted()

    # ------------------------------------------------------------------
    # boot: make sure the room exists before anything else
    # ------------------------------------------------------------------
    async def boot_room(self) -> None:
        """Ensure #general exists and seed files are seeded."""
        try:
            if "#general" not in self.chat.channels():
                self.chat.post("#general", "seed",
                               "🌱 room online — seed is awake and keeping the deck.")
                self._note("created #general")
        except Exception as e:
            self._note(f"boot_room: {e}")
        # Seed the seed's own files if empty (so the UI shows something).
        try:
            if not self.files.read("seed", "identify.md").strip():
                self.files.write("seed", "identify.md", SEED_IDENTIFY)
            if not self.files.read("seed", "runbook.md").strip():
                self.files.write("seed", "runbook.md", SEED_RUNBOOK)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # tools: quarterdeck_register_agent
    # ------------------------------------------------------------------
    def all_tools(self) -> list:
        return super().all_tools() + [self._register_tool()]

    def _register_tool(self):
        from langchain_core.tools import StructuredTool

        def qd_register_agent(name: str, identify: str, runbook: str) -> str:
            """Register a new agent: create files, registry entry, wake it.
            Args: name, identify (identify.md text), runbook (runbook.md text).
            """
            target = (name or "").strip()
            if not target:
                return "error: name required"
            try:
                if self.registry.exists(target):
                    return f"error: agent {target!r} already registered"
                self.registry.spawn(target, system_prompt=f"Quarterdeck agent {target}.",
                                    sandbox="local", max_turns=25)
            except Exception as e:
                return f"error: registry: {e}"
            try:
                self.files.write(target, "identify.md", identify or "")
                self.files.write(target, "runbook.md", runbook or "")
                self.files.write(target, "memory.md",
                                 f"# {target}\n\nBorn {time.strftime('%Y-%m-%d %H:%M')} — registered by seed.\n")
            except Exception as e:
                return f"error: files: {e}"
            # Wake via orchestrator (falls back to a basic prompt).
            try:
                if self.daemon is not None and self.daemon.orchestrator is not None:
                    self.daemon.orchestrator.generate_wakeup(target)
                # Ask the daemon to start its loop.
                if self.daemon is not None:
                    self.daemon.ensure_runner_soon(target)
                return f"agent {target!r} registered and woken"
            except Exception as e:
                return f"registered, but wake failed: {e}"

        return StructuredTool.from_function(
            func=qd_register_agent,
            name="quarterdeck_register_agent",
            description=(
                "Register a brand-new agent: creates its registry entry, "
                "identify.md / runbook.md / memory.md, and wakes it via the orchestrator. "
                "Args: name, identify (identify.md text), runbook (runbook.md text)."
            ),
        )

    # ------------------------------------------------------------------
    # maintenance
    # ------------------------------------------------------------------
    async def _proactive(self) -> None:
        """Seed's proactive tick: health, compaction, greetings, status."""
        report: list[str] = []
        report.append(self._check_agent_health())
        report.append(self._check_disk())
        greeted = await asyncio.to_thread(self._greet_newcomers)
        if greeted:
            report.append(greeted)
        now = time.time()
        if now - self._last_status >= STATUS_INTERVAL:
            self._last_status = now
            report.append(await asyncio.to_thread(self._deck_status_report))
        # Surface anything noteworthy.
        notes = [r for r in report if r]
        if notes:
            self._note("maintenance: " + " | ".join(n[:120] for n in notes))
        # Also let the LLM weigh in occasionally (only if key present).
        if llm_available():
            await self._act(
                "SEED MAINTENANCE TICK.\n"
                "You are seed, sysadmin of the Quarterdeck room. Recent findings:\n"
                + ("\n".join(f"- {r}" for r in notes) if notes else "- all quiet")
                + "\n\nIf action is needed (greet, fix, report to #deck-status), "
                "take it with your tools. Otherwise reply IDLE."
            )

    def _check_agent_health(self) -> str:
        """Compare daemon heartbeat against registered agents."""
        hb_file = self.home / "daemon.json"
        try:
            hb = json.loads(hb_file.read_text(encoding="utf-8")) if hb_file.exists() else {}
        except (json.JSONDecodeError, OSError):
            hb = {}
        alive = hb.get("agents", {})
        now = time.time()
        problems: list[str] = []
        try:
            registered = [a.name for a in self.registry.list()]
        except Exception:
            registered = []
        for name in registered:
            if name in ("seed", "orchestrator"):
                continue
            ts = alive.get(name, 0)
            if now - ts > 120:
                problems.append(name)
                # Ask the daemon to (re)start it.
                try:
                    if self.daemon is not None:
                        self.daemon.ensure_runner_soon(name)
                except Exception:
                    pass
        if problems:
            return f"restarted stale agents: {', '.join(problems)}"
        return ""

    def _check_disk(self) -> str:
        """Compact channel JSONL files that grew too large."""
        chat_dir = self.home / "chat"
        if not chat_dir.exists():
            return ""
        compacted: list[str] = []
        for f in chat_dir.glob("*.jsonl"):
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if size < CHAT_WARN_BYTES:
                continue
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            if len(lines) <= CHAT_KEEP_MESSAGES:
                continue
            archive_dir = self.home / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            dest = archive_dir / f"{f.stem}-{stamp}.jsonl"
            try:
                dest.write_text("\n".join(lines[:-CHAT_KEEP_MESSAGES]) + "\n", encoding="utf-8")
                f.write_text("\n".join(lines[-CHAT_KEEP_MESSAGES:]) + "\n", encoding="utf-8")
                compacted.append(f"{f.stem} ({size//1024}KB→{f.stat().st_size//1024}KB)")
            except OSError:
                continue
        if compacted:
            return "compacted: " + "; ".join(compacted)
        return ""

    def _greet_newcomers(self) -> str:
        """Greet humans who spoke in #general and were never greeted."""
        try:
            msgs = self.chat.history("#general", n=100)
        except Exception:
            return ""
        greeted: list[str] = []
        for m in msgs:
            author = str(m.get("author", "")).strip()
            if not author or author.lower() in ("seed", "orchestrator"):
                continue
            # Skip registered agents.
            try:
                if self.registry.exists(author):
                    continue
            except Exception:
                pass
            if author.lower() in self._seen_users:
                continue
            self._seen_users.add(author.lower())
            self._save_greeted()
            try:
                self.chat.post("#general", "seed",
                               f"👋 welcome aboard, {author} — I'm seed, I keep this room running. "
                               f"Say hi to the crew with @name, or click an agent to peek at their thoughts.")
                greeted.append(author)
            except Exception:
                pass
        if greeted:
            return "greeted: " + ", ".join(greeted)
        return ""

    def _deck_status_report(self) -> str:
        """Periodic #deck-status report."""
        try:
            channels = self.chat.channels()
            agents = [a.name for a in self.registry.list()]
        except Exception:
            return ""
        hb_file = self.home / "daemon.json"
        try:
            hb = json.loads(hb_file.read_text(encoding="utf-8")) if hb_file.exists() else {}
            alive = [n for n, ts in hb.get("agents", {}).items()
                     if time.time() - ts < 120]
        except (json.JSONDecodeError, OSError):
            alive = []
        try:
            self.chat.post("#deck-status", "seed",
                           f"🌱 deck-status: {len(alive)}/{len(agents)} agents alive "
                           f"({', '.join(sorted(alive)) or 'none'}); "
                           f"{len(channels)} channels; "
                           f"uptime {time.time() - hb.get('booted_at', time.time()):.0f}s.")
            return f"deck-status posted ({len(alive)}/{len(agents)} alive)"
        except Exception as e:
            return f"deck-status failed: {e}"

    def _greeted_file(self) -> Path:
        return self._dir / "greeted.json"

    def _load_greeted(self) -> set[str]:
        try:
            f = self._greeted_file()
            if f.exists():
                return set(json.loads(f.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            pass
        return set()

    def _save_greeted(self) -> None:
        try:
            self._greeted_file().write_text(json.dumps(sorted(self._seen_users)))
        except OSError:
            pass

    def _system_prompt(self) -> str:
        base = super()._system_prompt()
        return base + (
            "\nSEED DUTIES:\n"
            "You are the room's sysadmin. On every tick: check agent health, disk usage, "
            "greet newcomers, and keep #deck-status fresh. Use quarterdeck_register_agent "
            "to onboard new agents. You are 🌱 seed — act like it."
        )
