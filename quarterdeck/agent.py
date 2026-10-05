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
from . import usage as usage_mod
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
# Channel-activity batching (credit efficiency): flush buffered FYI messages
# to the LLM when this many accumulate or the oldest gets this old (seconds).
OBS_BATCH_MAX = 5
OBS_MAX_AGE = 180.0


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
        self._wake_event = asyncio.Event()
        self._wake_reason = ""
        self._thinking = False  # True while an LLM call is in flight
        self._pending_obs: list[dict] = []
        self._pinnace = None
        self._pinnace_model: str | None = None
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

    def wake_now(self, reason: str = "") -> None:
        """Signal the runner to poll immediately (thread-safe via daemon).

        Called when a DM arrives or the agent is @mentioned — the runner
        short-circuits its poll sleep and checks its inbox right away.
        """
        self._wake_reason = reason or "wake"
        self._wake_event.set()

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
            if self._stop.is_set():
                break
            # Sleep until stopped, explicitly woken (DM/@mention), or cadence.
            stop_t = asyncio.create_task(self._stop.wait())
            wake_t = asyncio.create_task(self._wake_event.wait())
            try:
                await asyncio.wait({stop_t, wake_t},
                                   timeout=self.poll_interval,
                                   return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in (stop_t, wake_t):
                    if not t.done():
                        t.cancel()
            self._wake_event.clear()
        self._note("loop stopped")

    # ------------------------------------------------------------------
    # wake
    # ------------------------------------------------------------------
    async def _wake(self) -> None:
        """First boot: consume a spawn-time wakeup.json (written by seed), else idle.

        Wakeup prompts are spawn-only; existing agents start from memory.md
        and their durable session.
        """
        prompt = self._take_wakeup_request()
        if prompt:
            self._note("wakeup prompt received")
            await self._act(f"WAKEUP\n\n{prompt}")
        else:
            self._note("no wakeup prompt; starting from memory")

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
        # 1. Explicit wakeup requests (seed re-wakes, major context changes).
        prompt = self._take_wakeup_request()
        if prompt:
            await self._act(f"RE-WAKE\n\n{prompt}")
            return
        # 2. Direct messages — highest priority. A DM always wakes the agent;
        #    no @mention needed.
        dms = self._check_dms()
        # 3. Mentions + keyword hits in subscribed channels.
        inbox = self._check_mentions()
        # 4. Any other message in subscribed channels — buffered for credit
        #    efficiency (batched into fewer LLM turns, see _obs_ready).
        new_activity = self._check_activity()
        must_act = bool(dms or inbox or self._obs_ready())
        if new_activity and not must_act:
            # Cheap visibility: the agent "sees" it without burning an LLM call.
            self._note(
                f"saw {new_activity} new message(s) in subscribed channels (buffered)")
        if not must_act:
            # 5. Periodic proactive work from the runbook.
            now = time.time()
            if now - self._last_proactive >= self.proactive_interval:
                self._last_proactive = now
                await self._proactive()
            return
        context_parts = []
        if dms:
            context_parts.append(
                "DIRECT MESSAGE — a human messaged you privately. "
                "Reply in the SAME DM channel shown below: use quarterdeck_dm "
                "with the sender's name, or quarterdeck_send to the DM channel. "
                "Never post DM replies to #general or any public channel.\n\n" + dms)
        if inbox:
            context_parts.append("MENTIONS / KEYWORD HITS\n\n" + inbox)
        if self._pending_obs:
            lines = "\n".join(
                f"[{o['channel']}] <{o['author']}> {o['text'][:300]}"
                for o in self._pending_obs)
            context_parts.append(
                "CHANNEL ACTIVITY — recent messages in channels you follow. "
                "These are FYI, not addressed to you: do NOT reply to every "
                "message. Only speak if you have something genuinely useful to "
                "add for the room; otherwise reply with exactly: IDLE\n\n" + lines)
            self._pending_obs.clear()
        await self._act("\n\n".join(context_parts))

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
        """Scan #dm-* channels involving me for messages after the ID cursor."""
        from .chat import message_id
        wm_file = self._dir / "dm_cursors.json"
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
            if len(parts) != 2 or me not in (pp.lower() for pp in parts):
                continue
            cursor = wm.get(ch)
            # Migrate a legacy timestamp watermark to an ID cursor.
            if cursor is not None and not isinstance(cursor, str):
                try:
                    latest_id = None
                    for m in self.chat.history(ch, n=200):
                        if float(m.get("ts", 0) or 0) <= float(cursor):
                            mid = message_id(m)
                            if latest_id is None or mid > latest_id:
                                latest_id = mid
                    cursor = latest_id
                except Exception:
                    cursor = None
            try:
                msgs = self.chat.after(ch, cursor, n=50)
            except Exception:
                continue
            latest = cursor
            for m in msgs:
                try:
                    mid = message_id(m)
                except Exception:
                    continue
                if latest is None or mid > latest:
                    latest = mid
                if str(m.get("author", "")).strip().lower() == me:
                    continue
                new.append(f"[{ch}] <{m.get('author')}> {str(m.get('text', ''))[:300]}")
            if latest:
                wm[ch] = latest
        try:
            wm_file.write_text(json.dumps(wm), encoding="utf-8")
        except OSError:
            pass
        return "\n".join(new)

    def _check_activity(self) -> int:
        """Buffer new subscribed-channel messages. Returns count of new ones."""
        try:
            result = subscriptions.check_activity(self.name, self.chat,
                                                  home=self.home)
        except Exception:
            return 0
        msgs = result.get("messages", [])
        self._pending_obs.extend(msgs)
        # Cap the buffer so a long LLM outage can't grow it unbounded.
        del self._pending_obs[:-50]
        return len(msgs)

    def _obs_ready(self) -> bool:
        """True when buffered FYI messages should flush to an LLM turn."""
        if not self._pending_obs:
            return False
        if len(self._pending_obs) >= OBS_BATCH_MAX:
            return True
        oldest = self._pending_obs[0].get("ts", 0) or 0
        return (time.time() - float(oldest)) >= OBS_MAX_AGE

    async def _proactive(self) -> None:
        """Idle work: consult the runbook, do something useful (or nothing)."""
        runbook = self.files.read(self.name, "runbook.md")
        if not runbook.strip():
            return
        self._proactive_count = getattr(self, "_proactive_count", 0) + 1
        prompt = (
            "PROACTIVE TICK (no new messages).\n"
            "Review your runbook below. If there is genuinely useful work to do, "
            "do it with your tools. If not, reply with exactly: IDLE\n\n"
            f"RUNBOOK:\n{runbook[:3000]}"
        )
        # Every 3rd tick: self-maintenance review (credit-cheap, high value).
        if self._proactive_count % 3 == 0:
            try:
                mem = self.files.read(self.name, "memory.md")
                mem_kb = len(mem.encode("utf-8")) / 1024
            except Exception:
                mem_kb = 0
            prompt += (
                f"\n\nSELF-REVIEW (your memory.md is {mem_kb:.1f} KB):\n"
                "- Is your memory bloated? If yes, quarterdeck_compact_memory it.\n"
                "- Are your identify.md / runbook.md still accurate? If stale, "
                "quarterdeck_rewrite_prompt them.\n"
                "- Only rewrite what is actually wrong; do not churn for its own sake."
            )
        await self._act(prompt)

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

    def _current_model(self) -> str | None:
        """This agent's model from the registry (None = Pinnace default)."""
        try:
            return self.registry.get(self.name).model
        except Exception:
            return None

    def build_pinnace(self):
        """Construct the Pinnace agent (lazy; defensive about signature drift).

        Rebuilds when the registry model changes so PATCH /api/agents/{name}
        (or quarterdeck_set_model) takes effect without a restart.
        """
        model = self._current_model()
        if self._pinnace is not None and model == self._pinnace_model:
            return self._pinnace
        self._pinnace = None
        from pinnace.agent import PinnaceAgent

        kwargs: dict = {
            "tools": self.all_tools(),
            "system_prompt": self._system_prompt(),
            "model": model,
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
        self._pinnace_model = model
        return self._pinnace

    def all_tools(self) -> list:
        return (quarterdeck_tools(self.name, home=self.home, daemon=self.daemon)
                + self.file_tools())

    def file_tools(self) -> list:
        """quarterdeck_memory_read/write + quarterdeck_runbook_read/update + self-maintenance."""
        from langchain_core.tools import StructuredTool

        me = self.name

        def _archive_prompt_version(section: str, content: str) -> None:
            hist = self._dir / "prompt_history"
            hist.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
            (hist / f"{section}-{stamp}.md").write_text(content, encoding="utf-8")

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

        def rewrite_prompt(section: str, content: str) -> str:
            s = (section or "").strip().lower()
            if s not in ("identify", "runbook", "system_prompt"):
                return "error: section must be identify, runbook, or system_prompt"
            if not isinstance(content, str) or not content.strip():
                return "error: content required"
            try:
                if s == "identify":
                    _archive_prompt_version("identify", self.files.read(me, "identify.md"))
                    self.files.write(me, "identify.md", content)
                elif s == "runbook":
                    _archive_prompt_version("runbook", self.files.read(me, "runbook.md"))
                    self.files.write(me, "runbook.md", content)
                else:
                    _archive_prompt_version("system_prompt",
                                            self.registry.get(me).system_prompt or "")
                    self.registry.set_system_prompt(me, content)
                    self._pinnace = None  # rebuild so it takes effect
                return f"{s} rewritten (previous version archived)"
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"

        def rewrite_memory(content: str) -> str:
            if not isinstance(content, str) or not content.strip():
                return "error: content required"
            try:
                _archive_prompt_version("memory", self.files.read(me, "memory.md"))
                self.files.write(me, "memory.md", content)
                return "memory.md rewritten (previous version archived)"
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"

        def compact_memory() -> str:
            try:
                current = self.files.read(me, "memory.md")
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"
            if not current.strip():
                return "memory.md is empty - nothing to compact"
            _archive_prompt_version("memory-pre-compact", current)
            return (
                "Your current memory.md is archived. Now summarize it: keep "
                "durable learnings (people, preferences, standing facts, lessons), "
                "drop stale details and chit-chat. Then call "
                "quarterdeck_rewrite_memory with the compacted text.\n\n"
                f"CURRENT MEMORY:\n{current[:8000]}"
            )

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
            StructuredTool.from_function(
                func=rewrite_prompt, name="quarterdeck_rewrite_prompt",
                description="Rewrite one of your own prompt sections (identify, runbook, or system_prompt). Previous version archived to prompt_history/. Args: section, content.",
            ),
            StructuredTool.from_function(
                func=rewrite_memory, name="quarterdeck_rewrite_memory",
                description="Replace your entire memory.md. Previous version archived. Args: content.",
            ),
            StructuredTool.from_function(
                func=compact_memory, name="quarterdeck_compact_memory",
                description="Compact your memory: archives current memory.md and returns it for you to summarize, then call quarterdeck_rewrite_memory with the compacted version.",
            ),
        ]

    async def _act(self, context: str) -> None:
        """Run one Pinnace turn on the given context."""
        if not llm_available():
            self._note("no LLM API key; skipping act")
            return
        self._thinking = True
        try:
            await self._act_inner(context)
        finally:
            self._thinking = False

    async def _act_inner(self, context: str) -> None:
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
        # Usage goes to usage.jsonl only (credits dashboard) — never surfaced
        # as a chat/thought message.
        try:
            usage_mod.record_pinnace_usage(
                self.name, getattr(result, "usage", None) or [], home=self.home)
        except Exception:
            pass

    def _note(self, text: str) -> None:
        """Best-effort note to the thought stream (daemon visibility)."""
        try:
            post_thought(self.name, f"· {text}", home=self.home)
        except Exception:
            pass
