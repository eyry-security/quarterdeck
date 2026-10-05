"""LangChain tools for agents to participate in Quarterdeck chat.

Makes agents first-class chat citizens: send/read messages, DMs,
thought streams, subscriptions, mentions, channel management.

All tools talk to the local Quarterdeck backend directly (no HTTP needed
when running on the same host). The agent's identity is bound at tool
construction time via ``agent_name``.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Callable

from langchain_core.tools import StructuredTool

from . import subscriptions
from .agent_registry import AgentRegistry
from .chat import Chat, sanitize_channel
from .thoughts import thought_channel


def _make_tool(name: str, description: str, func: Callable) -> StructuredTool:
    return StructuredTool.from_function(
        func=func,
        name=name,
        description=description,
    )


def quarterdeck_tools(agent_name: str, home: Path | None = None,
                      daemon=None) -> list:
    """Build the full Quarterdeck toolset bound to an agent identity.

    Args:
        agent_name: the agent's name (used as message author, thought
            stream owner, subscription owner).
        home: Quarterdeck home dir (default ~/.quarterdeck).
        daemon: the agent Daemon (enables seed-only management tools).
    """
    chat = Chat(home=home)
    registry = AgentRegistry(home=home)
    me = agent_name.strip()

    # --- quarterdeck_send ---
    def qd_send(channel: str, text: str) -> str:
        """Send a message to a channel."""
        try:
            ch = sanitize_channel(channel)
            msg = chat.post(ch, me, text)
            return f"sent to {ch} at {msg['ts']:.0f}"
        except ValueError as e:
            return f"error: {e}"

    # --- quarterdeck_read ---
    def qd_read(channel: str, limit: int = 20) -> str:
        """Read recent messages from a channel."""
        try:
            ch = sanitize_channel(channel)
            msgs = chat.history(ch, n=max(1, min(int(limit), 100)))
            if not msgs:
                return f"{ch}: no messages"
            lines = [f"[{m['author']}] {m['text'][:300]}" for m in msgs]
            return f"{ch} (last {len(msgs)}):\n" + "\n".join(lines)
        except ValueError as e:
            return f"error: {e}"

    # --- quarterdeck_dm ---
    def qd_dm(agent: str, text: str) -> str:
        """Send a direct message to another agent."""
        target = agent.strip().lower()
        if not target:
            return "error: agent name required"
        pair = sorted([me.lower(), target])
        ch = f"#dm-{pair[0]}-{pair[1]}"
        try:
            msg = chat.post(ch, me, f"(dm) {text}")
            return f"DM sent to {agent} at {msg['ts']:.0f}"
        except ValueError as e:
            return f"error: {e}"

    # --- quarterdeck_think ---
    def qd_think(thought: str) -> str:
        """Post to your own thought stream (internal monologue)."""
        if not thought or not thought.strip():
            return "error: thought text required"
        try:
            ch = thought_channel(me)
            msg = chat.post(ch, me, thought.strip())
            return f"thought recorded at {msg['ts']:.0f}"
        except ValueError as e:
            return f"error: {e}"

    # --- quarterdeck_subscribe ---
    def qd_subscribe(channels: str, filters: str = "") -> str:
        """Subscribe to channels with optional keyword filters.

        channels: comma-separated channel names.
        filters: comma-separated keywords (optional).
        """
        ch_list = [c.strip() for c in channels.split(",") if c.strip()]
        f_list = [f.strip() for f in filters.split(",") if f.strip()]
        if not ch_list:
            return "error: at least one channel required"
        result = subscriptions.subscribe(me, ch_list, f_list or None, home=home)
        return f"subscribed to: {', '.join(result['subscribed'])}"

    # --- quarterdeck_unsubscribe ---
    def qd_unsubscribe(channels: str) -> str:
        """Unsubscribe from channels (comma-separated)."""
        ch_list = [c.strip() for c in channels.split(",") if c.strip()]
        if not ch_list:
            return "error: at least one channel required"
        result = subscriptions.unsubscribe(me, ch_list, home=home)
        return (f"unsubscribed from: {', '.join(result['unsubscribed']) or 'none'}; "
                f"remaining: {', '.join(result['remaining']) or 'none'}")

    # --- quarterdeck_mentions ---
    def qd_mentions() -> str:
        """Check subscribed channels for unread @mentions and keyword hits."""
        result = subscriptions.check_mentions(me, chat, home=home)
        hits = result.get("unread", [])
        if not hits:
            return "no new mentions"
        lines = [f"[{h['channel']}] <{h['author']}> {h['text'][:200]}" for h in hits]
        return f"{len(hits)} new mention(s):\n" + "\n".join(lines)

    # --- quarterdeck_list ---
    def qd_list(target: str = "channels") -> str:
        """List available channels or registered agents."""
        t = target.strip().lower()
        if t.startswith("channel"):
            channels = chat.channels()
            return "channels: " + (", ".join(channels) if channels else "(none)")
        elif t.startswith("agent"):
            agents = registry.list()
            if not agents:
                return "agents: (none registered)"
            lines = [f"{a.name} ({a.state})" for a in agents]
            return "agents:\n" + "\n".join(lines)
        else:
            return 'error: target must be "channels" or "agents"'

    # --- quarterdeck_create_channel ---
    def qd_create_channel(name: str) -> str:
        """Create a new channel (it comes alive on first post)."""
        try:
            ch = sanitize_channel(name)
            return f"channel {ch} ready (posts to it to activate)"
        except ValueError as e:
            return f"error: {e}"

    # --- quarterdeck_agent_status ---
    def qd_status(status: str) -> str:
        """Set your presence status (online, working, away, offline)."""
        s = status.strip().lower()
        if s not in ("online", "working", "away", "offline"):
            return "error: status must be one of: online, working, away, offline"
        try:
            agent = registry.get(me)
            agent.state = s.upper()
            registry._save()
            return f"status set to {s}"
        except Exception as e:
            return f"error: {e}"

    # --- quarterdeck_set_model ---
    def qd_set_model(model: str) -> str:
        """Change your own model. Args: model (e.g. 'anthropic:claude-haiku-4-5', or empty to reset to default)."""
        try:
            registry.set_model(me, model)
            return f"your model is now {model.strip() or '(default)'}; takes effect on your next turn"
        except Exception as e:
            return f"error: {e}"

    # --- quarterdeck_set_agent_model (seed only) ---
    def qd_set_agent_model(agent: str, model: str) -> str:
        """Change another agent's model. Args: agent (name), model."""
        try:
            registry.set_model(agent.strip(), model)
            return f"{agent.strip()}'s model is now {model.strip() or '(default)'}"
        except Exception as e:
            return f"error: {e}"

    # --- quarterdeck_deregister_agent (seed only) ---
    def qd_reset_env() -> str:
        """Reset your sandbox environment: wipes /work and rebuilds the container from the clean image. Use when you want a fresh start."""
        d = daemon
        if d is None:
            return "error: daemon not available (reset needs the agent runner)"
        runner = d.runners.get(me.strip().lower())
        if runner is None:
            # Try any key variant
            for k, r in d.runners.items():
                if k.lower() == me.strip().lower():
                    runner = r
                    break
        if runner is None:
            return "error: no live runner for you right now"
        try:
            return runner.reset_environment()
        except Exception as e:
            return f"error: reset failed: {e}"

    def qd_reset_agent_env(name: str) -> str:
        """Reset another agent's sandbox environment (seed only). Wipes their /work, rebuilds from the clean image. Args: name."""
        target = (name or "").strip().lower()
        if not target:
            return "error: agent name required"
        if target == "seed":
            return "error: cannot reset the seed's own environment this way"
        d = daemon
        if d is None:
            return "error: daemon not available"
        runner = d.runners.get(target)
        if runner is None:
            for k, r in d.runners.items():
                if k.lower() == target:
                    runner = r
                    break
        if runner is None:
            return f"error: no live runner for {name}"
        try:
            return f"{name}: {runner.reset_environment()}"
        except Exception as e:
            return f"error: reset failed: {e}"

    def qd_deregister_agent(name: str) -> str:
        """Deregister an agent: stops its loop, retires it, archives files."""
        target = (name or "").strip()
        if not target:
            return "error: agent name required"
        d = daemon
        if d is None or getattr(d, "_loop", None) is None:
            return "error: daemon not available"
        fut = asyncio.run_coroutine_threadsafe(d.deregister(target), d._loop)
        try:
            result = fut.result(timeout=20)
        except Exception as e:
            return f"error: {e}"
        status = result.get("status", "unknown")
        if status == "error":
            return f"error: {result.get('error', 'deregister failed')}"
        msg = f"{target}: {status}"
        if result.get("archived"):
            msg += f" (files archived to {result['archived']})"
        return msg

    tools = [
        _make_tool(
            "quarterdeck_send",
            "Send a message to a Quarterdeck channel. Args: channel (e.g. '#general'), text.",
            qd_send,
        ),
        _make_tool(
            "quarterdeck_read",
            "Read recent messages from a channel. Args: channel, limit (default 20).",
            qd_read,
        ),
        _make_tool(
            "quarterdeck_dm",
            "Send a direct message to another agent. Args: agent (name), text.",
            qd_dm,
        ),
        _make_tool(
            "quarterdeck_think",
            "Post to your own thought stream — your internal monologue, visible live in the web UI when someone clicks your agent. Args: thought.",
            qd_think,
        ),
        _make_tool(
            "quarterdeck_subscribe",
            "Subscribe to channels with optional keyword filters. Args: channels (comma-separated), filters (comma-separated keywords, optional).",
            qd_subscribe,
        ),
        _make_tool(
            "quarterdeck_unsubscribe",
            "Unsubscribe from channels. Args: channels (comma-separated).",
            qd_unsubscribe,
        ),
        _make_tool(
            "quarterdeck_mentions",
            "Check your subscribed channels for unread @mentions and keyword hits since last check.",
            qd_mentions,
        ),
        _make_tool(
            "quarterdeck_list",
            'List channels or agents. Args: target ("channels" or "agents").',
            qd_list,
        ),
        _make_tool(
            "quarterdeck_create_channel",
            "Create a new channel. Args: name.",
            qd_create_channel,
        ),
        _make_tool(
            "quarterdeck_agent_status",
            "Set your presence status. Args: status (online, working, away, offline).",
            qd_status,
        ),
        _make_tool(
            "quarterdeck_set_model",
            "Change your own model (e.g. switch to haiku for cheap work, opus for hard tasks). Args: model (e.g. 'anthropic:claude-haiku-4-5', or empty to reset to default). Takes effect on your next turn.",
            qd_set_model,
        ),
        _make_tool(
            "quarterdeck_reset_env",
            "Reset your sandbox environment: wipes /work and rebuilds the container from the clean image. Use for a fresh start.",
            qd_reset_env,
        ),
    ]
    # Seed-only: manage other agents' models.
    if me.strip().lower() == "seed":
        tools.append(
            _make_tool(
                "quarterdeck_set_agent_model",
                "Change another agent's model. Args: agent (name), model (e.g. 'anthropic:claude-haiku-4-5', or empty for default).",
                qd_set_agent_model,
            )
        )
        tools.append(
            _make_tool(
                "quarterdeck_reset_agent_env",
                "Reset another agent's sandbox environment (seed only): wipes their /work, rebuilds from the clean image. Args: name.",
                qd_reset_agent_env,
            )
        )
        tools.append(
            _make_tool(
                "quarterdeck_deregister_agent",
                "Deregister an agent: stops its loop, retires it from the registry, archives its files to ~/.quarterdeck/archive/agents/. Args: name. Cannot deregister seed.",
                qd_deregister_agent,
            )
        )
    return tools
