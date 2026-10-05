"""Orchestrator agent: generates tailored wakeup prompts.

The orchestrator is itself a Pinnace agent with a system prompt describing
its role. On boot — and on demand via ``quarterdeck_wake(agent)`` — it reads
the target agent's identify.md / runbook.md / memory.md, the current channel
list, recent mentions/DMs, and the time of day, then writes a wakeup prompt:
who they are, what to care about right now, what's pending, first actions.

Wakeup prompts are logged to the agent's thought stream so they're visible.
"""
from __future__ import annotations

import inspect
import json
import time
from pathlib import Path

from .agent import _safe, llm_available
from .agent_files import AgentFiles
from .agent_registry import AgentRegistry, home_dir
from .agent_stream import post_thought
from .chat import Chat
from .thoughts import thought_channel

ORCHESTRATOR_SYSTEM = """\
You are the orchestrator of the Quarterdeck agent room. Your job: write wakeup \
prompts for agents.

A wakeup prompt tells an agent who they are, what is happening in the room \
right now, what is pending for them specifically, and what their first actions \
should be. It is the first thing they read on boot (or re-wake), so make it \
concrete and actionable — not generic.

You are given: the agent's identify.md (who they are), runbook.md (procedures), \
memory.md (what they remember), the channel list, their recent unread mentions \
and DMs, and the current time.

Write the wakeup prompt in second person ("You are scout…"). Keep it under \
400 words. End with 2-4 explicit first actions. If there is genuinely nothing \
pending, say so and suggest one useful proactive check from their runbook.
"""


class Orchestrator:
    """Generates wakeup prompts. Cheap to construct; LLM runs on demand."""

    def __init__(self, home: Path | None = None, daemon=None):
        self.home = Path(home) if home else home_dir()
        self.daemon = daemon
        self.files = AgentFiles(home=self.home)
        self.registry = AgentRegistry(home=self.home)
        self.chat = Chat(home=self.home)
        self._pinnace = None

    # ------------------------------------------------------------------
    # wakeup generation
    # ------------------------------------------------------------------
    def generate_wakeup(self, agent_name: str) -> str:
        """Build a tailored wakeup prompt for an agent (blocking LLM call)."""
        name = agent_name.strip()
        context = self._gather_context(name)
        if not llm_available():
            prompt = self._fallback_wakeup(name, context)
        else:
            prompt = self._llm_wakeup(name, context)
        # Log it visibly.
        try:
            post_thought(name,
                         f"☀️ wakeup prompt:\n{prompt[:1500]}",
                         home=self.home)
        except Exception:
            pass
        # Persist for the agent loop to consume.
        try:
            d = self.home / "agents" / _safe(name)
            d.mkdir(parents=True, exist_ok=True)
            (d / "wakeup.json").write_text(
                json.dumps({"prompt": prompt, "at": time.time()}), encoding="utf-8")
        except OSError:
            pass
        # Nudge an in-process runner, if any.
        if self.daemon is not None:
            try:
                self.daemon.nudge(name)
            except Exception:
                pass
        return prompt

    def _gather_context(self, name: str) -> dict:
        files = self.files.all(name)
        try:
            channels = self.chat.channels()
        except Exception:
            channels = []
        # Recent mentions (don't consume the watermark — just peek).
        mentions: list[str] = []
        try:
            from . import subscriptions
            data = subscriptions._load(self.home, name)
            since = data.get("last_check", 0.0)
            for ch, sub in data.get("channels", {}).items():
                try:
                    msgs = self.chat.history(ch, n=30)
                except Exception:
                    continue
                for m in msgs:
                    if m.get("ts", 0) <= since:
                        continue
                    if str(m.get("author", "")).strip().lower() == name.lower():
                        continue
                    text = str(m.get("text", ""))
                    if f"@{name.lower()}" in text.lower():
                        mentions.append(f"[{ch}] <{m.get('author')}> {text[:200]}")
        except Exception:
            pass
        return {
            "identify": files.get("identify.md", "")[:2000],
            "runbook": files.get("runbook.md", "")[:2000],
            "memory": files.get("memory.md", "")[:2000],
            "channels": channels,
            "mentions": mentions[:10],
            "time": time.strftime("%A %Y-%m-%d %H:%M %Z", time.localtime()),
        }

    def _llm_wakeup(self, name: str, ctx: dict) -> str:
        from pinnace.agent import PinnaceAgent

        kwargs: dict = {
            "system_prompt": ORCHESTRATOR_SYSTEM,
            "session_id": f"qd-orchestrator-{_safe(name)}",
            "agent_id": "orchestrator",
            "max_turns": 6,
        }
        try:
            if "thinking" in inspect.signature(PinnaceAgent.__init__).parameters:
                kwargs["thinking"] = False
        except (TypeError, ValueError):
            pass
        agent = PinnaceAgent(**kwargs)
        prompt = (
            f"Write a wakeup prompt for agent {name!r}.\n\n"
            f"IDENTIFY.md:\n{ctx['identify'] or '(empty)'}\n\n"
            f"RUNBOOK.md:\n{ctx['runbook'] or '(empty)'}\n\n"
            f"MEMORY.md:\n{ctx['memory'] or '(empty)'}\n\n"
            f"CHANNELS: {', '.join(ctx['channels']) or '(none)'}\n"
            f"CURRENT TIME: {ctx['time']}\n"
            f"UNREAD MENTIONS:\n" + ("\n".join(ctx["mentions"]) if ctx["mentions"] else "(none)")
        )
        try:
            result = agent.run(prompt + "\n\nReply with ONLY the wakeup prompt, no preamble.")
            return (result.final or "").strip() or self._fallback_wakeup(name, ctx)
        except Exception as e:
            return self._fallback_wakeup(name, ctx) + f"\n\n(orchestrator LLM failed: {e})"

    def _fallback_wakeup(self, name: str, ctx: dict) -> str:
        lines = [
            f"You are {name}, a persistent agent in the Quarterdeck chat room.",
            f"Current time: {ctx['time']}.",
        ]
        if ctx["identify"]:
            lines.append(f"Your identity: {ctx['identify'][:500]}")
        if ctx["channels"]:
            lines.append(f"Channels: {', '.join(ctx['channels'])}")
        if ctx["mentions"]:
            lines.append("Unread mentions:\n" + "\n".join(ctx["mentions"]))
        else:
            lines.append("No unread mentions.")
        lines.append(
            "First actions: 1) quarterdeck_mentions to check for anything new. "
            "2) Skim #general for context. 3) Consult your runbook for standing work."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # quarterdeck_wake tool (orchestrator-only by convention)
    # ------------------------------------------------------------------
    def wake_tool(self):
        from langchain_core.tools import StructuredTool

        def qd_wake(agent: str) -> str:
            """Wake an agent with a fresh tailored prompt. Args: agent (name)."""
            target = (agent or "").strip()
            if not target:
                return "error: agent name required"
            try:
                self.generate_wakeup(target)
                return f"wakeup prompt delivered to {target}"
            except Exception as e:
                return f"error: {type(e).__name__}: {e}"

        return StructuredTool.from_function(
            func=qd_wake,
            name="quarterdeck_wake",
            description=(
                "Wake (or re-wake) an agent with a freshly generated tailored prompt "
                "built from their files, mentions, and current room state. "
                "Args: agent (name)."
            ),
        )
