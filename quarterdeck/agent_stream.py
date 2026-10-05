"""Stream agent thinking output to Quarterdeck thought channels.

Usage:
    from quarterdeck.agent_stream import thought_stream_log

    # Wrap your existing log function; thinking lines go to the
    # agent's thought stream, everything else passes through.
    agent = PinnaceAgent(
        ...,
        log=thought_stream_log("my-agent", base_log=my_existing_log),
    )

Or post thoughts directly:
    from quarterdeck.agent_stream import post_thought
    post_thought("my-agent", "Hmm, this endpoint looks interesting…")
"""
from __future__ import annotations

from pathlib import Path

from .chat import Chat
from .thoughts import thought_channel


def post_thought(agent: str, text: str, home: Path | None = None) -> bool:
    """Post a single thought to an agent's thought stream. Returns success."""
    if not text or not text.strip():
        return False
    try:
        chat = Chat(home=home)
        chat.post(thought_channel(agent), agent.strip(), text.strip()[:2000])
        return True
    except Exception:
        return False


TOOL_PREFIX = "[pinnace] tool:"
TOOL_RESULT_PREFIX = "[pinnace] tool-result:"


def _format_tool_call(text: str) -> str | None:
    """Format a '[pinnace] tool: name(args)' line for the thought stream."""
    try:
        payload = text.split(TOOL_PREFIX, 1)[1].strip()
        # payload looks like: name({'key': 'val', ...})
        if "(" in payload:
            name, rest = payload.split("(", 1)
            args = rest.rstrip(")")
        else:
            name, args = payload, ""
        name = name.strip()
        args = args.strip()
        if len(args) > 300:
            args = args[:300] + "\u2026"
        return f"\U0001f527 {name}({args})"
    except Exception:
        return None


def _format_tool_result(text: str) -> str | None:
    """Format a '[pinnace] tool-result: name -> output' line."""
    try:
        payload = text.split(TOOL_RESULT_PREFIX, 1)[1].strip()
        # payload looks like: name -> output
        if " -> " in payload:
            _, out = payload.split(" -> ", 1)
        else:
            out = payload
        out = out.strip()
        if len(out) > 500:
            out = out[:500] + "\u2026"
        return f"\u2192 {out}"
    except Exception:
        return None


def thought_stream_log(agent: str, base_log=None, home: Path | None = None,
                       prefix: str = "[pinnace] thinking:"):
    """Wrap a log callable: thinking + tool calls stream to Quarterdeck.

    Args:
        agent: agent name owning the thought stream.
        base_log: existing log callable (or None to drop non-thinking lines).
        home: Quarterdeck home dir.
        prefix: log-line prefix identifying thinking output.

    Returns a log(line) callable for PinnaceAgent(log=...).
    """
    chat = Chat(home=home)
    me = agent.strip()

    def _post(text: str, limit: int = 2000) -> None:
        try:
            if text and text.strip():
                chat.post(thought_channel(me), me, text.strip()[:limit])
        except Exception:
            pass

    def log(line: str) -> None:
        # Always pass through to the base log first.
        if base_log is not None:
            try:
                base_log(line)
            except Exception:
                pass
        # Thinking lines, tool calls, and tool results → thought stream
        # (best-effort, never breaks the run).
        try:
            text = str(line)
            if TOOL_RESULT_PREFIX in text:
                formatted = _format_tool_result(text)
                if formatted:
                    _post(formatted)
            elif TOOL_PREFIX in text:
                formatted = _format_tool_call(text)
                if formatted:
                    _post(formatted)
            elif prefix in text:
                thought = text.split(prefix, 1)[1].strip()
                if thought:
                    _post(thought)
        except Exception:
            pass

    return log
