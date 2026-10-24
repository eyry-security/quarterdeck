"""Model catalog and default resolution for Quarterdeck agents."""

from __future__ import annotations

DEFAULT_MODEL = "deepseek:deepseek-flash"

DEFAULT_MODELS = [
    "deepseek:deepseek-flash",
    "deepseek:deepseek-v4-pro",
    "anthropic:claude-opus-4-6",
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-haiku-4-5",
]


def resolve_model(model: str | None) -> str:
    """Return an explicit model reference or Quarterdeck's default model."""
    return (model or "").strip() or DEFAULT_MODEL
