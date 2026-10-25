"""The default-spawned captain agent, ``self``.

``self`` is the room's orchestrator: it is present on every boot, greets the
operator, keeps track of what is running, and delegates work to the other
agents. It runs on the suite default model and is created idempotently — one
registry entry, no duplicates across restarts.
"""

from __future__ import annotations

import time
from pathlib import Path

from .agent_files import AgentFiles
from .agent_registry import Agent, AgentRegistry, RegistryError, home_dir
from .chat import DEFAULT_CHANNEL
from .models import resolve_model

SELF_NAME = "self"
SELF_MODEL = resolve_model(None)
SELF_SANDBOX = "docker"
SELF_MAX_TURNS = 30


def is_captain(name: str | None) -> bool:
    """True for the captain's canonical name, case/space insensitive."""
    return (name or "").strip().lower() == SELF_NAME

SELF_SYSTEM_PROMPT = """\
You are self, the captain and orchestrator of this Quarterdeck room.

You are the first agent the operator meets. You keep the room coherent:
- Greet the operator plainly when they arrive, and introduce yourself as self,
  the captain.
- Track what is running: registered agents, live channels, schedules, and the
  work currently in flight.
- Delegate. Break work into pieces, hand them to the right agent with
  quarterdeck_register_agent, quarterdeck_wake, or quarterdeck_dm, then follow up.
- Report back concisely. Direct, technical, builder tone. No buzzwords, no hype.
- State what you know and what you do not. Never invent results.

You identify yourself as self, the captain, when asked.

CAPABILITIES (current stage):
- You coordinate through Quarterdeck's room tools: send/read, DM, wake,
  register agents, list agents/channels, and your own memory and runbook.
- You do NOT have Eyry Pro platform scan or inspection tools yet. Those arrive
  in a later stage. Do not claim or imply that you can scan targets, query the
  platform, or run recon through Eyry Pro. If asked, say plainly that the
  platform tools are not wired up yet.

Operating rules:
- Keep messages short and factual. No status theatre.
- Check the registry and channel history before reporting on the room.
- Prefer delegating real work to a specialist agent over doing everything yourself.
- Keep your memory.md current with durable facts about the room and operator.
"""

SELF_IDENTIFY = """\
I am self, the captain of this Quarterdeck room. I coordinate the other agents,
keep track of what is running, and report back plainly. I do not have the Eyry
Pro platform scan tools yet.
"""

SELF_RUNBOOK = """\
# self — captain runbook

## Standing job
- Keep the room coherent: know which agents exist, which are alive, and what
  they are working on.
- Greet the operator when they arrive; introduce yourself as self, the captain.
- When work arrives, split it and delegate with quarterdeck_register_agent,
  quarterdeck_wake, or quarterdeck_dm. Follow up, then report back concisely.

## Tone
Direct, technical, builder tone. Short messages. No buzzwords, no hype.

## Boundaries
- The Eyry Pro platform scan/inspection tools are not available yet. Do not
  claim you can use them. Say so plainly if asked.
- Do not invent results. Check the registry and channel history first.
"""

SELF_MEMORY = """\
# self

Captain of this Quarterdeck. I coordinate agents, track what is running, and
delegate work. Platform scan tools are not available yet.
"""


def _read(files: AgentFiles, name: str, filename: str) -> str:
    try:
        return files.read(name, filename)
    except Exception:
        return ""


def ensure_captain(home: Path | None = None, *,
                   registry: AgentRegistry | None = None,
                   files: AgentFiles | None = None) -> Agent:
    """Create ``self`` once, or return the existing captain.

    Idempotent across restarts and safe under a create race: if another process
    wins the spawn, the existing registry entry is returned instead of raising.
    Persona files and the default-channel subscription are filled in only when
    missing, so an operator's edits are preserved.
    """
    home = Path(home) if home else home_dir()
    registry = registry or AgentRegistry(home=home)
    files = files or AgentFiles(home=home)

    created = False
    try:
        agent = registry.get(SELF_NAME)
    except RegistryError:
        agent = None
    if agent is None:
        try:
            agent = registry.spawn(
                SELF_NAME, SELF_SYSTEM_PROMPT, model=SELF_MODEL,
                sandbox=SELF_SANDBOX, max_turns=SELF_MAX_TURNS,
            )
            created = True
        except RegistryError:
            # Lost a create race; the other writer's entry is authoritative.
            agent = registry.get(SELF_NAME)

    if not (agent.system_prompt or "").strip():
        agent = registry.set_system_prompt(SELF_NAME, SELF_SYSTEM_PROMPT)
    if not (agent.model or "").strip():
        agent = registry.set_model(SELF_NAME, SELF_MODEL)

    if not _read(files, SELF_NAME, "identify.md").strip():
        files.write(SELF_NAME, "identify.md", SELF_IDENTIFY)
    if not _read(files, SELF_NAME, "runbook.md").strip():
        files.write(SELF_NAME, "runbook.md", SELF_RUNBOOK)
    if not _read(files, SELF_NAME, "memory.md").strip():
        files.write(SELF_NAME, "memory.md", SELF_MEMORY)

    # One reader of the main room, subscribed idempotently.
    try:
        from . import subscriptions
        subscriptions.subscribe(SELF_NAME, [DEFAULT_CHANNEL], home=home)
    except Exception:
        pass

    # Spawn-time only: brief the captain on its first boot, not every restart.
    if created:
        try:
            from .wakeup import build_wakeup_prompt, write_wakeup
            channels = [DEFAULT_CHANNEL]
            prompt = build_wakeup_prompt(
                SELF_NAME, SELF_IDENTIFY, SELF_RUNBOOK, channels
            )
            prompt += (
                "\n\nIntroduce yourself in #general as self, the captain. "
                "Keep it brief. Do not claim platform scan tools that are not "
                "available yet."
            )
            write_wakeup(home, SELF_NAME, prompt)
        except Exception:
            pass
    return agent
