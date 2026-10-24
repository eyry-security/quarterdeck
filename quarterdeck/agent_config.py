"""One per-agent configuration/sandbox policy for every wake path.

Scheduler runs and daemon runner turns must resolve the same registry values
(model, system prompt, max turns, sandbox) into the same durable Pinnace
session. Sandbox selection is fail-closed: Docker is the default, and a local
sandbox is only used when development mode is explicitly enabled.
"""

from __future__ import annotations

import os
from pathlib import Path

from .agent_registry import Agent, agent_key, home_dir
from .models import resolve_model

ALLOW_LOCAL_ENV = "QUARTERDECK_ALLOW_LOCAL"
TRUE_VALUES = {"1", "true", "yes", "on"}


class SandboxUnavailable(RuntimeError):
    """The configured sandbox cannot be used; the agent must not start."""


def development_local_enabled(flag: bool = False) -> bool:
    """True when local sandboxing was explicitly allowed for development."""
    if flag:
        return True
    return os.environ.get(ALLOW_LOCAL_ENV, "").strip().lower() in TRUE_VALUES


def session_id_for(agent: Agent | str) -> str:
    """The one durable Pinnace session id for an agent, on every wake path."""
    name = agent.name if isinstance(agent, Agent) else str(agent)
    return f"agent-{agent_key(name)}"


def require_sandbox_policy(agent: Agent, *, allow_local: bool = False) -> str:
    """Validate the registry sandbox kind without starting a container."""
    kind = (getattr(agent, "sandbox", "docker") or "docker").strip().lower()
    if kind == "local" and not development_local_enabled(allow_local):
        raise SandboxUnavailable(
            f"agent {agent.name!r} requests a local sandbox; unattended local "
            "execution is disabled. Use --allow-local for development or "
            "respawn the agent with --sandbox docker."
        )
    if kind not in ("docker", "local"):
        raise SandboxUnavailable(
            f"agent {agent.name!r} has unknown sandbox {kind!r}; expected docker or local"
        )
    return kind


def build_sandbox(agent: Agent, *, home: Path | None = None,
                  allow_local: bool = False, persistent: bool = False):
    """Create the registry-selected sandbox, refusing unsafe fallbacks."""
    kind = require_sandbox_policy(agent, allow_local=allow_local)
    dev_local = development_local_enabled(allow_local)

    if kind == "local":
        from pinnace import LocalSandbox
        return LocalSandbox(unsafe_ok=True)

    error: Exception | None = None
    if persistent:
        try:
            from .persistent_sandbox import PersistentDockerSandbox
            return PersistentDockerSandbox(agent.name, home=home)
        except Exception as exc:  # missing package, daemon, or image
            error = exc
    else:
        try:
            from pinnace import DockerSandbox
            return DockerSandbox()
        except Exception as exc:
            error = exc

    if dev_local:
        from pinnace import LocalSandbox
        return LocalSandbox(unsafe_ok=True)

    detail = f"{type(error).__name__}: {error}" if error else "unavailable"
    raise SandboxUnavailable(
        f"docker sandbox unavailable for agent {agent.name!r} ({detail}). "
        "Refusing to start; install/start Docker and the agent image, or pass "
        "--allow-local for development."
    )


def resolve_agent_config(agent: Agent, *, sandbox, home: Path | None = None,
                         tools=None, log=None, system_prompt: str | None = None,
                         max_turns: int | None = None, thinking: bool = False):
    """Resolve one ``AgentConfig`` from the registry for this agent."""
    from pinnace import AgentConfig, SessionStore

    root = Path(home) if home else home_dir()
    return AgentConfig.resolve(
        model=resolve_model(agent.model),
        sandbox=sandbox,
        tools=list(tools or []),
        system_prompt=(
            agent.system_prompt if system_prompt is None else system_prompt
        ),
        max_turns=agent.max_turns if max_turns is None else max_turns,
        session_store=SessionStore(root / "pinnace"),
        session_id=session_id_for(agent),
        log=log or (lambda message: None),
        agent_id=agent.name,
        thinking=thinking,
    )
