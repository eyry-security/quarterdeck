"""One supervised Quarterdeck runtime.

The runtime is the single owner of the agent daemon, interval scheduler,
durable event-rule engine, ChatOps router, and (when serving) the web app.
Every entrypoint builds this object instead of assembling its own half-stack.

Startup order is fixed: daemon (seed -> agents) -> scheduler -> rules ->
ChatOps -> web. Shutdown mirrors it: web -> ChatOps -> rules -> scheduler ->
daemon.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from .agent import AgentRunner
from .agent_files import AgentFiles
from .agent_registry import AgentRegistry, home_dir
from .chat import Chat
from .chatops import ChatOps
from .daemon import Daemon
from .events import EventBus, EventStore
from .rules import RuleEngine, RuleStore
from .scheduler import Runner, Scheduler
from .seed import SeedRunner


class Runtime:
    """Stable component graph shared by ``quarterdeck run`` and ``serve``."""

    def __init__(
        self,
        home: Path | None = None,
        *,
        allow_local: bool = False,
        runner: Runner | None = None,
        daemon: Daemon | None = None,
        scheduler: Scheduler | None = None,
        rules: RuleEngine | None = None,
        chatops: ChatOps | None = None,
        runner_cls=AgentRunner,
        seed_cls=SeedRunner,
    ):
        self.home = Path(home) if home else home_dir()
        self.home.mkdir(parents=True, exist_ok=True)
        self.allow_local = allow_local
        self.registry = AgentRegistry(home=self.home)
        self.files = AgentFiles(home=self.home)
        self.bus = EventBus(store=EventStore(self.home))
        self.chat = Chat(home=self.home, bus=self.bus)
        self.scheduler = scheduler or Scheduler(
            self.registry,
            self.chat,
            bus=self.bus,
            runner=runner,
            home=self.home,
            allow_local=allow_local,
        )
        self.rules = rules or RuleEngine(
            RuleStore(self.home), self.registry, self.scheduler, self.chat, self.bus
        )
        self.chatops = chatops or ChatOps(
            self.registry, self.scheduler, self.chat, self.bus
        )
        self.daemon = daemon or Daemon(
            home=self.home,
            allow_local=allow_local,
            runner_cls=runner_cls,
            seed_cls=seed_cls,
        )
        self._daemon_thread: threading.Thread | None = None
        self._started = False

    def start(self, *, wait_ready: float = 10.0) -> None:
        """Bring components up in dependency order."""
        if self._started:
            return
        # 1. Daemon: seed -> registered agents. Wait for boot so wake routing
        #    below never races the runners it is about to signal.
        self._daemon_thread = threading.Thread(
            target=self._run_daemon, name="qd-daemon", daemon=True
        )
        self._daemon_thread.start()
        if wait_ready:
            self.daemon.wait_ready(timeout=wait_ready)
        # 2-4. Scheduler feeds rules and ChatOps; both subscribe to the shared
        #      event bus only after the agent room exists.
        self.scheduler.start()
        self.rules.start()
        self.chatops.start()
        self._started = True

    def stop(self, *, timeout: float = 10.0) -> None:
        """Tear components down in reverse startup order."""
        if not self._started:
            return
        self.chatops.stop()
        self.rules.stop()
        self.scheduler.stop(timeout=timeout)
        self.daemon.stop()
        if self._daemon_thread is not None:
            self._daemon_thread.join(timeout=timeout)
            self._daemon_thread = None
        self._started = False

    def wait(self, poll: float = 1.0) -> None:
        """Block until interrupted. Caller stops the runtime in ``finally``."""
        try:
            while True:
                time.sleep(poll)
        except KeyboardInterrupt:
            pass

    def _run_daemon(self) -> None:
        import asyncio

        try:
            asyncio.run(self.daemon.run())
        except Exception as exc:  # surfaced in the service log, not swallowed
            print(f"[daemon] fatal: {exc}", flush=True)
