"""Quarterdeck: durable local orchestration and ChatOps for Pinnace agents."""

from __future__ import annotations

from .agent_registry import Agent, AgentRegistry
from .chat import Chat
from .chatops import ChatOps
from .events import (
    APLOMADO_SCAN_COMPLETED,
    CHAT_MESSAGE,
    RUN_COMPLETED,
    RUN_REQUESTED,
    TICK,
    WEBHOOK,
    Event,
    EventBus,
    EventStore,
)
from .integrations import format_aplomado_alert, parse_aplomado_event
from .rules import Rule, RuleEngine, RuleStore
from . import subscriptions
from .thoughts import thought_channel

def __getattr__(name: str):
    # Lazy: langchain_core is heavy; only import on explicit use.
    if name == "quarterdeck_tools":
        from .tools import quarterdeck_tools
        return quarterdeck_tools
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
from .scheduler import Scheduler

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "APLOMADO_SCAN_COMPLETED",
    "Agent",
    "AgentRegistry",
    "CHAT_MESSAGE",
    "Chat",
    "ChatOps",
    "Event",
    "EventBus",
    "EventStore",
    "RUN_COMPLETED",
    "RUN_REQUESTED",
    "Rule",
    "RuleEngine",
    "RuleStore",
    "Scheduler",
    "TICK",
    "WEBHOOK",
    "format_aplomado_alert",
    "parse_aplomado_event",
    "quarterdeck_tools",
    "subscriptions",
    "thought_channel",
]
