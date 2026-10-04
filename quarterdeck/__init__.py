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
from .integrations import aplomado_event, format_aplomado_alert
from .rules import Rule, RuleEngine, RuleStore
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
    "aplomado_event",
    "format_aplomado_alert",
]
