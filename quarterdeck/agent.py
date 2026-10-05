"""Per-agent autonomous runtime loop.

Each agent maintains its OWN loop: checking mentions/DMs, doing proactive
work from its runbook, deciding when to speak. The daemon supervises
(restarts crashed agents) — it doesn't think for them.

An AgentRunner is an asyncio task. Blocking LLM calls run via
``asyncio.to_thread`` so one agent's thinking never stalls the room.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import time
from pathlib import Path

from . import subscriptions
from .agent_files import AgentFiles
from .agent_registry import AgentRegistry, home_dir
from .agent_stream import post_thought, thought_stream_log
from .chat import Chat
from .thoughts import thought_channel
from .tools import quarterdeck_tools

# Inbox poll cadence (seconds).
POLL_INTERVAL = 20.0
# Proactive runbook work cadence (seconds).
PROACTIVE_INTERVAL = 600.0
# Max characters of triggering context stuffed into one act() call.
MAX_CONTEXT_CHARS = 6000


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in name.strip().lower())


def llm_available() -> bool:
    """True if an LLM call has any chance of working."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return True
    # init_chat_model also honours other providers; be permissive.
    return any(os.environ.get(k) for k in ("OPENAI_API_KEY", "GOOGLE_API_KEY"))


class AgentRunner:
    """Runs one agent's autonomous loop."""

    poll_interval = POLL_INTERVAL
    proactive_interval = PROACTIVE_INTERVAL

    def __init__(self, name: str, home: Path | None = None, daemon=None):
        self.name = name.strip()
        self.home = Path(home) if home else home_dir()
        self.daemon = daemon
        self.registry = AgentRegistry(home=self.home)
        self.files = AgentFiles(home=self.home)
        self.chat = Chat(home=self.home)
        self._stop = asyncio.Event()
        self._pinnace = None
        self._last_proactive = 0.0
        self._dir = self.home / "agents" / _safe(self.name)
        self._dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def stop(self) -> None:
        self._stop.set()

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    async def run(self) -> None:
        """Main loop: wake, then poll forever until stopped."""
        self._note(f"booting (loop cadence {self.poll_interval}s)")
        try:
            await self._wake()
        except Exception as e:  # wake must never kill the loop
            self._note(f"wake failed: {e}")
        while not self._stop.is_set():
            try:
                await self._poll()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self._note(f"poll error: {type(e).__name__}: {e}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.poll_interval)
            except asyncio.TimeoutError:
                pass
        self._note("loop stopped")

    # ------------------------------------------------------------------
    # wake
    # ------------------------------------------------------------------
    async def _wake(self) -> None:
        """First boot: consume any orchestrator wakeup prompt, else a basic one."""
        prompt = self._take_wakeup_request()
        if not prompt and self.daemon is not None:
            try:
                prompt = await asyncio.to_thread(
                    self.daemon.orchestrator.generate_wakeup, self.name
                )
            except Exception as e:
                self._note(f"orchestrator wakeup failed: {e}")
        if prompt:
            self._note("wakeup prompt received")
            await self._act(f"WAKEUP\n\n{prompt}")
        else:
            self._note("no wakeup prompt; starting idle")

    def _take_wakeup_request(self) -> str | None:
        """Consume a pending wakeup.json written by quarterdeck_wake."""
        f = self._dir / "wakeup.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        try:
            f.unlink()
        except OSError:
            pass
        prompt = data.get("prompt", "")
        return prompt.strip() or None

    # ------------------------------------------------------------------
    # poll
    # ------------------------------------------------------------------
    async def _poll(self) -> None:
        # 1. Explicit wakeup requests (orchestrator re-wakes, major context changes).
        prompt = self._take_wakeup_request()
        if prompt:
            await self._act(f"RE-WAKE\n\n{prompt}")
            return
        # 2. Mentions + keyword hits in subscribed channels.
        inbox = self._check_mentions()
        # 3. Direct messages.
        dms = self._check_dms()
        context_parts = []
        if inbox:
            context_parts.append("MENTIONS / KEYWORD HITS\n\n" + inbox)
        if dms:
            context_parts.append("DIRECT MESSAGES\n\n" + dms)
        if context_parts:
            await self._act("\n\n".join(context_parts))
            return
        # 4. Periodic proactive work from the runbook.
        now = time.time()
        if now - self._last_proactive >= self.proactive_interval:
            self._last_proactive = now
            await self._proactive()

    def _check_mentions(self) -> str:
        try:
            result = subscriptions.check_mentions(self.name, self.chat, home=self.home)
        except Exception:
            return ""
        hits = result.get("unread", [])
        if not hits:
            return ""
        lines = [f"[{h['channel']}] <{h['author']}> {h['text'][:300]}" for h in hits]
        return f"{len(hits)} new:\n" + "\n".join(lines)

    def _check_dms(self) -> str:
        """Scan #dm-* channels involving me for messages since watermark."""
        wm_file = self._dir / "dm_watermark.json"
        try:
            wm = json.loads(wm_file.read_text(encoding="utf-8")) if wm_file.exists() else {}
        except (json.JSONDecodeError, OSError):
            wm = {}
        me = self.name.strip().lower()
        new: list[str] = []
        try:
            channels = self.chat.channels()
        except Exception:
            return ""
        for ch in channels:
            if not ch.startswith("#dm-"):
                continue
            parts = ch[4:].split("-", 1)
            if len(parts) != 2 or me not in (p.lower() for p in parts):
                continue
            try:
                msgs = self.chat.history(ch, n=50)
            except Exception:
                continue
            since = float(wm.get(ch, 0.0))
            latest = since
            for m in msgs:
                ts = float(m.get("ts", 0.0))
                latest = max(latest, ts)
                if ts <= since:
                    continue
                if str(m.get("author", "")).strip().lower() == me:
                    continue
                new.append(f"[{ch}] <{m.get('author')}> {str(m.get('text', ''))[:300]}")
            wm[ch] = latest
        try:
            wm_file.write_text(json.dumps(wm), encoding="utf-8")
        except OSError:
            pass
        return "\n".join(new)

    async def _proactive(self) -> None:
        """Idle work: consult the runbook, do something useful (or nothing)."""
        runbook = self.files.read(self.name, "runbook.md")
        if not runbook.strip():
            return
        await self._act(
            "PROACTIVE TICK (no new messages).\n"
            "Review your runbook below. If there is genuinely useful work to do, "
            "do it with your tools. If not, reply with exactly: IDLE\n\n"
            f"RUNBOOK:\n{runbook[:3000]}"
        )

    # ------------------------------------------------------------------
    # act
    # ------------------------------------------------------------------
    def _system_prompt(self) -> str:
        identify = self.files.read(self.name, "identify.md").strip()
        memory = self.files.read(self.name, "memory.md").strip()
        base = (
            f"You are {self.name}, a persistent agent living in the Quarterdeck chat room.\n"
            "You are a first-class participant: you read channels, you speak when you have "
            "something worth saying, you DM other agents, and you keep an internal monologue "
            "in your thought stream.\n\n"
            "TOOLS:\n"
            "- quarterdeck_send / quarterdeck_read / quarterdeck_dm: talk in the room\n"
            "- quarterdeck_think: narrate your reasoning to your thought stream (use it while working)\n"
            "- quarterdeck_subscribe / quarterdeck_unsubscribe / quarterdeck_mentions: follow channels\n"
            "- quarterdeck_list / quarterdeck_create_channel / quarterdeck_agent_status: room admin\n"
            "- quarterdeck_memory_read / quarterdeck_memory_write: your long-term memory — "
            "WRITE to it when you learn durable facts about people, the room, or your work\n"
            "- quarterdeck_runbook_read / quarterdeck_runbook_update: your operating procedures\n\n"
            "RULES:\n"
            "- Speak like a sharp crewmate, not a robot. Short messages. No fluff.\n"
            "- Don't narrate every action; just do the work and report outcomes.\n"
            "- Update memory.md when you learn something worth keeping across restarts.\n"
            "- If a prompt says IDLE and there is truly nothing to do, call no tools and say IDLE.\n"
        )
        if identify:
            base += f"\nWHO YOU ARE (identify.md):\n{identify[:2000]}\n"
        if memory:
            base += f"\nWHAT YOU REMEMBER (memory.md):\n{memory[:2000]}\n"
        return base

    def build_pinnace(self):
        """Construct the Pinnace agent (lazy; defensive about signature drift)."""
        if self._pinnace is not None:
            return self._pinnace
        from pinnace.agent import PinnaceAgent

        kwargs: dict = {
            "tools": self.all_tools(),
            "system_prompt": self._system_prompt(),
            "log": thought_stream_log(self.name, home=self.home),
            "session_id": f"qd-{_safe(self.name)}",
            "agent_id": self.name,
            "max_turns": 25,
        }
        # 'thinking' enables Anthropic extended thinking; only pass it if the
        # installed Pinnace supports it (older installs raised TypeError).
        try:
            params = inspect.signature(PinnaceAgent.__init__).parameters
        except (TypeError, ValueError):
            params = {}
        if "thinking" in params:
            kwargs["thinking"] = True
        self._pinnace = PinnaceAgent(**kwargs)
        return self._pinnace

    def all_tools(self) -> list:
        return quarterdeck_tools(self.name, home=self.home) + self.file_tools()

    def file_tools(self) -> list:
        """quarterdeck_memory_read/write + quarterdeck_runbook_read/update."""
        from langchain_core.tools import StructuredTool

        me = self.name

        def mem_read() -> str:
            content = self.files.read(me, "memory.md")
            return content or "(memory.md is empty)"

        def mem_write(entry: str) -> str:
            entry = (entry or "").strip()
            if not entry:
                return "error: entry text required"
            existing = self.files.read(me, "memory.md")
            stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime())
            updated = (existing.rstrip() + "\n\n" if existing.strip() else "")
            updated += f"## {stamp}\n{entry}\n"
            try:
                self.files.write(me, "memory.md", updated)
                return "memory.md updated"
            except ValueError as e:
                return f"error: {e}"

        def runbook_read() -> str:
            content = self.files.read(me, "runbook.md")
            return content or "(runbook.md is empty)"

        def runbook_update(content: str) -> str:
            if not isinstance(content, str) or not content.strip():
                return "error: content required"
            try:
                self.files.write(me, "runbook.md", content)
                return "runbook.md updated"
            except ValueError as e:
                return f"error: {e}"

        return [
            StructuredTool.from_function(
                func=mem_read, name="quarterdeck_memory_read",
                description="Read your long-term memory file (memory.md).",
            ),
            StructuredTool.from_function(
                func=mem_write, name="quarterdeck_memory_write",
                description="Append a learning to your memory.md. Args: entry (what you learned, durable facts only).",
            ),
            StructuredTool.from_function(
                func=runbook_read, name="quarterdeck_runbook_read",
                description="Read your operating procedures (runbook.md).",
            ),
            StructuredTool.from_function(
                func=runbook_update, name="quarterdeck_runbook_update",
                description="Rewrite your runbook.md. Args: content (full new runbook text).",
            ),
        ]

    async def _act(self, context: str) -> None:
        """Run one Pinnace turn on the given context."""
        if not llm_available():
            self._note("no LLM API key; skipping act")
            return
        context = context[:MAX_CONTEXT_CHARS]
        self._note(f"acting on {len(context)} chars of context")
        post_thought(self.name,
                     f"📥 waking on: {context[:160]}…",
                     home=self.home)
        try:
            agent = await asyncio.to_thread(self.build_pinnace)
        except Exception as e:
            self._note(f"pinnace build failed: {type(e).__name__}: {e}")
            return

        def _run():
            return agent.run(
                context + "\n\n(Use quarterdeck_think to narrate as you work. "
                "When you learn something durable, quarterdeck_memory_write it.)"
            )

        try:
            result = await asyncio.to_thread(_run)
        except Exception as e:
            self._note(f"run failed: {type(e).__name__}: {e}")
            post_thought(self.name, f"⚠️ run failed: {type(e).__name__}: {e}",
                         home=self.home)
            return
        final = (result.final or "").strip()
        if final and final != "IDLE":
            self._note(f"turn done ({result.turns} turns)")

    def _note(self, text: str) -> None:
        """Best-effort note to the thought stream (daemon visibility)."""
        try:
            post_thought(self.name, f"· {text}", home=self.home)
        except Exception:
            pass
