"""quarterdeck: agent control plane — scheduler, events, and IRC-style chat.

The command deck of the Eyry suite. Quarterdeck wakes and sleeps Pinnace
agents, keeps their identity and lifecycle state, and gives agents and humans
a shared chat to coordinate in.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .agent_registry import Agent, AgentRegistry
from .chat import Chat
from .events import TICK, CHAT_MESSAGE, WEBHOOK, Event, EventBus
from .scheduler import Scheduler

__all__ = [
    "__version__",
    "Agent",
    "AgentRegistry",
    "Chat",
    "Event",
    "EventBus",
    "Scheduler",
    "TICK",
    "CHAT_MESSAGE",
    "WEBHOOK",
]
