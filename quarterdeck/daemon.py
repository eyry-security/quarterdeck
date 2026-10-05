"""Quarterdeck agent daemon: supervises per-agent runtime loops.

Boot order: seed -> agents. Wakeup prompts are spawn-time only (seed writes them).
The daemon does NOT think for agents; each AgentRunner owns its loop.
The daemon boots them in order, restarts crashed ones (with backoff),
and writes a heartbeat file the web UI reads for presence.

Run: ``quarterdeck serve --agents``  (or ``python -m quarterdeck.daemon``)
"""
from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from .agent import AgentRunner
from .agent_registry import AgentRegistry, home_dir
from .seed import SeedRunner

HEARTBEAT_INTERVAL = 30.0
SUPERVISE_INTERVAL = 15.0
MAX_RESTARTS = 5          # per agent before giving up for a while
RESTART_COOLDOWN = 300.0  # seconds before retrying a repeatedly-crashing agent


class Daemon:
    def __init__(self, home: Path | None = None):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.registry = AgentRegistry(home=self.home)
        self.runners: dict[str, AgentRunner] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._restarts: dict[str, int] = {}
        self._last_crash: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._pending: set[str] = set()  # runners requested via ensure_runner_soon
        self._deregistered: set[str] = set()  # never auto-restart these
        self.booted_at = time.time()
        self._loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------------
    # public API (also used by seed / tools)
    # ------------------------------------------------------------------
    def nudge(self, name: str) -> None:
        """Wakeup.json was written; the runner picks it up on next poll."""
        # In-process runners poll frequently enough; nothing else needed.
        self._pending.add(name)

    def wake_agent(self, name: str, reason: str = "dm") -> bool:
        """Wake an agent's runner immediately (thread-safe; e.g. from the web server).

        Returns True if a live runner was signaled.
        """
        key = (name or "").strip()
        runner = self.runners.get(key)
        if runner is None:
            lowered = key.lower()
            for k, r in self.runners.items():
                if k.lower() == lowered:
                    runner = r
                    break
        loop = self._loop
        if runner is None or loop is None or loop.is_closed():
            return False
        try:
            loop.call_soon_threadsafe(runner.wake_now, reason)
        except RuntimeError:
            return False
        return True

    async def deregister(self, name: str) -> dict:
        """Stop an agent's loop, retire it from the registry, archive its files.

        The supervisor will not restart a deregistered agent. Files move to
        ~/.quarterdeck/archive/agents/<name>-<timestamp>/ (never deleted).
        """
        key = (name or "").strip()
        if key.lower() == "seed":
            return {"agent": key, "status": "error",
                    "error": "seed cannot be deregistered"}
        target = None
        for k in self.runners:
            if k == key or k.lower() == key.lower():
                target = k
                break
        if target is None:
            return {"agent": key, "status": "not-running"}
        self._deregistered.add(target)
        runner = self.runners.pop(target, None)
        task = self.tasks.pop(target, None)
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            self.registry.retire(target)
        except Exception:
            pass
        archived = self._archive_agent_files(target)
        self._heartbeat()
        return {"agent": target, "status": "deregistered", "archived": archived}

    def _archive_agent_files(self, name: str) -> str | None:
        from .agent import _safe
        src_dir = self.home / "agents" / _safe(name)
        if not src_dir.exists():
            return None
        dest_base = self.home / "archive" / "agents"
        dest_base.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = dest_base / f"{_safe(name)}-{stamp}"
        try:
            shutil.move(str(src_dir), str(dest))
            return str(dest)
        except Exception:
            return None

    def ensure_runner_soon(self, name: str) -> None:
        """Start this agent's runner soon (immediately if the loop is running)."""
        self._deregistered.discard(name)
        self._pending.add(name)
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._drain_pending()))

    def stop(self) -> None:
        self._stop.set()
        for r in self.runners.values():
            r.stop()

    # ------------------------------------------------------------------
    # boot
    # ------------------------------------------------------------------
    async def run(self) -> None:
        """Boot in order, then supervise forever."""
        self._loop = asyncio.get_running_loop()
        print("[daemon] booting: seed -> agents", flush=True)
        # 0. Seed is special (gets its 🌱 badge in the UI).
        try:
            if not self.registry.exists("seed"):
                self.registry.spawn("seed", system_prompt="Genesis agent.",
                                    sandbox="local", max_turns=10)
        except Exception as e:
            print(f"[daemon] registry ensure seed: {e}", flush=True)
        # Migration: the orchestrator is gone (wakeup is a seed-time function now).
        try:
            if self.registry.exists("orchestrator"):
                self.registry.retire("orchestrator")
                print("[daemon] retired legacy orchestrator agent", flush=True)
        except Exception as e:
            print(f"[daemon] orchestrator retire: {e}", flush=True)
        # 1. Seed first, always. It ensures the room exists.
        seed = await self._start_runner("seed", SeedRunner)
        try:
            await seed.boot_room()
        except Exception as e:
            print(f"[daemon] seed boot_room failed: {e}", flush=True)
        self._heartbeat()

        # 2. Start every registered agent (except seed). Agents read any
        #    pending wakeup.json from their spawn, then run their own loops.
        for agent in self._safe_list():
            if agent.name == "seed":
                continue
            await self._start_runner(agent.name, AgentRunner)
            self._heartbeat()

        print(f"[daemon] room live with {len(self.tasks)} runners", flush=True)
        await self._supervise()

    def _safe_list(self):
        try:
            return self.registry.list()
        except Exception as e:
            print(f"[daemon] registry list failed: {e}", flush=True)
            return []

    async def _start_runner(self, name: str, cls) -> AgentRunner:
        existing = self.tasks.get(name)
        if existing is not None and not existing.done():
            return self.runners[name]
        runner = cls(name, home=self.home, daemon=self)
        # SeedRunner takes no name arg default; AgentRunner does. Both accept (name, home, daemon).
        self.runners[name] = runner
        self.tasks[name] = asyncio.create_task(self._wrap(name, runner), name=f"qd-{name}")
        print(f"[daemon] started runner: {name}", flush=True)
        return runner

    async def _wrap(self, name: str, runner: AgentRunner) -> None:
        """Run a runner; record crashes for the supervisor."""
        try:
            await runner.run()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self._last_crash[name] = time.time()
            self._restarts[name] = self._restarts.get(name, 0) + 1
            print(f"[daemon] runner {name} crashed: {type(e).__name__}: {e}", flush=True)

    async def _drain_pending(self) -> None:
        """Start any runners requested via ensure_runner_soon."""
        for name in sorted(self._pending):
            self._pending.discard(name)
            task = self.tasks.get(name)
            if task is not None and not task.done():
                continue
            if name == "seed":
                await self._start_runner(name, SeedRunner)
            else:
                try:
                    self.registry.get(name)
                except Exception:
                    continue
                await self._start_runner(name, AgentRunner)

    # ------------------------------------------------------------------
    # supervision
    # ------------------------------------------------------------------
    async def _supervise(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=SUPERVISE_INTERVAL)
                break
            except asyncio.TimeoutError:
                pass
            # Pending starts (e.g. newly registered agents).
            await self._drain_pending()
            # Restart crashed runners (with cooldown after repeated crashes).
            for name, task in list(self.tasks.items()):
                if not task.done() or name in self._deregistered:
                    continue
                # Retrieve exception to avoid "never retrieved" warnings.
                try:
                    task.exception()
                except asyncio.CancelledError:
                    continue
                except Exception:
                    pass
                restarts = self._restarts.get(name, 0)
                last = self._last_crash.get(name, 0)
                if restarts >= MAX_RESTARTS and time.time() - last < RESTART_COOLDOWN:
                    continue
                print(f"[daemon] restarting crashed runner: {name} (#{restarts + 1})", flush=True)
                cls = SeedRunner if name == "seed" else AgentRunner
                await self._start_runner(name, cls)
            self._heartbeat()
        # Shutdown: cancel everything.
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)
        print("[daemon] stopped", flush=True)

    # ------------------------------------------------------------------
    # heartbeat (web UI presence)
    # ------------------------------------------------------------------
    def _heartbeat(self) -> None:
        data = {
            "booted_at": self.booted_at,
            "at": time.time(),
            "agents": {
                name: time.time()
                for name, task in self.tasks.items()
                if not task.done()
            },
        }
        try:
            (self.home / "daemon.json").write_text(json.dumps(data), encoding="utf-8")
        except OSError:
            pass

