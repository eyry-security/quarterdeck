"""Quarterdeck agent daemon: supervises per-agent runtime loops.

Boot order: seed -> orchestrator -> per-agent wakeup prompts -> agents run.
The daemon does NOT think for agents; each AgentRunner owns its loop.
The daemon boots them in order, restarts crashed ones (with backoff),
and writes a heartbeat file the web UI reads for presence.

Run: ``quarterdeck serve --agents``  (or ``python -m quarterdeck.daemon``)
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from .agent import AgentRunner
from .agent_registry import AgentRegistry, home_dir
from .orchestrator import Orchestrator
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
        self.orchestrator = Orchestrator(home=self.home, daemon=self)
        self.runners: dict[str, AgentRunner] = {}
        self.tasks: dict[str, asyncio.Task] = {}
        self._restarts: dict[str, int] = {}
        self._last_crash: dict[str, float] = {}
        self._stop = asyncio.Event()
        self._pending: set[str] = set()  # runners requested via ensure_runner_soon
        self.booted_at = time.time()

    # ------------------------------------------------------------------
    # public API (also used by seed / tools)
    # ------------------------------------------------------------------
    def nudge(self, name: str) -> None:
        """Wakeup.json was written; the runner picks it up on next poll."""
        # In-process runners poll frequently enough; nothing else needed.
        self._pending.add(name)

    def ensure_runner_soon(self, name: str) -> None:
        """Start this agent's runner soon (immediately if the loop is running)."""
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
        print("[daemon] booting: seed -> orchestrator -> agents", flush=True)
        # 0. Specials visible in the registry (seed gets its 🌱 badge in the UI).
        for special, prompt in (
            ("seed", "Genesis agent: keeps the Quarterdeck room alive."),
            ("orchestrator", "Writes tailored wakeup prompts for agents."),
        ):
            try:
                if not self.registry.exists(special):
                    self.registry.spawn(special, system_prompt=prompt,
                                        sandbox="local", max_turns=10)
            except Exception as e:
                print(f"[daemon] registry ensure {special}: {e}", flush=True)
        # 1. Seed first, always. It ensures the room exists.
        seed = await self._start_runner("seed", SeedRunner)
        try:
            await seed.boot_room()
        except Exception as e:
            print(f"[daemon] seed boot_room failed: {e}", flush=True)
        self._heartbeat()

        # 2. Orchestrator next.
        await self._start_runner("orchestrator", _OrchestratorRunner)
        self._heartbeat()

        # 3. Wake every registered agent (except the specials).
        for agent in self._safe_list():
            if agent.name in ("seed", "orchestrator"):
                continue
            # Orchestrator generates the wakeup prompt; the runner consumes it.
            try:
                await asyncio.to_thread(self.orchestrator.generate_wakeup, agent.name)
            except Exception as e:
                print(f"[daemon] wakeup for {agent.name} failed: {e}", flush=True)
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
            elif name == "orchestrator":
                await self._start_runner(name, _OrchestratorRunner)
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
                if not task.done():
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
                cls = SeedRunner if name == "seed" else (
                    _OrchestratorRunner if name == "orchestrator" else AgentRunner)
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


class _OrchestratorRunner(AgentRunner):
    """The orchestrator as a supervised runner.

    It mostly idles — wakeups are generated on demand via generate_wakeup().
    Its loop handles re-wake requests and periodic re-wakes of long-idle agents.
    """

    poll_interval = 60.0
    proactive_interval = 3600.0

    def _system_prompt(self) -> str:
        return (
            "You are the orchestrator of the Quarterdeck agent room.\n"
            "Your job: write wakeup prompts for agents (via quarterdeck_wake), "
            "keep them purposeful, and re-wake agents that have been idle too long "
            "or when major context changes (new channel, many unread mentions).\n"
            "You act rarely but decisively."
        )

    def all_tools(self) -> list:
        tools = super().all_tools()
        if self.daemon is not None:
            tools.append(self.daemon.orchestrator.wake_tool())
        return tools

    async def _proactive(self) -> None:
        # Re-wake agents idle > 2h with fresh context.
        if self.daemon is None:
            return
        for name, task in self.daemon.tasks.items():
            if name in ("seed", "orchestrator") or task.done():
                continue
            runner = self.daemon.runners.get(name)
            if runner is None:
                continue
            idle_for = time.time() - getattr(runner, "_last_proactive", 0)
            if idle_for > 7200:
                try:
                    await asyncio.to_thread(
                        self.daemon.orchestrator.generate_wakeup, name)
                    self._note(f"re-woke idle agent {name}")
                except Exception as e:
                    self._note(f"re-wake {name} failed: {e}")


def main() -> int:
    """Entry point: python -m quarterdeck.daemon"""
    daemon = Daemon()
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
