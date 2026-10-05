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


def thought_stream_log(agent: str, base_log=None, home: Path | None = None,
                       prefix: str = "[pinnace] thinking:"):
    """Wrap a log callable: thinking lines also stream to Quarterdeck.

    Args:
        agent: agent name owning the thought stream.
        base_log: existing log callable (or None to drop non-thinking lines).
        home: Quarterdeck home dir.
        prefix: log-line prefix identifying thinking output.

    Returns a log(line) callable for PinnaceAgent(log=...).
    """
    chat = Chat(home=home)
    me = agent.strip()

    def log(line: str) -> None:
        # Always pass through to the base log first.
        if base_log is not None:
            try:
                base_log(line)
            except Exception:
                pass
        # Thinking lines → thought stream (best-effort, never breaks the run).
        try:
            text = str(line)
            if prefix in text:
                thought = text.split(prefix, 1)[1].strip()
                if thought:
                    chat.post(thought_channel(me), me, thought[:2000])
        except Exception:
            pass

    return log
