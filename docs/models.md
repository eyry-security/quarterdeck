# Model Switcher

Each agent's model is shown in the sidebar (short name) and editable via a
dropdown in the agent's thought view.

- **API**: `PATCH /api/agents/{name}` with `{"model": "anthropic:claude-haiku-4-5"}`
  (empty/null resets to default). `GET /api/models` lists the catalog.
- **Catalog**: defaults to DeepSeek (deepseek-flash, deepseek-v4-pro) plus
  Anthropic models (opus-4-6, sonnet-4-6, haiku-4-5); override with the
  `QUARTERDECK_MODELS` env var (comma-separated).
- **Default**: `deepseek:deepseek-flash`. Anthropic models remain selectable.
- **No restart needed**: `AgentRunner.build_pinnace()` checks the registry on
  every turn and rebuilds the Pinnace agent when the model changed.
- **Self-service**: `quarterdeck_set_model(model)` — e.g. scout drops to haiku
  for cheap polling, back to opus for hard tasks.
- **Seed control**: `quarterdeck_set_agent_model(agent, model)`.
