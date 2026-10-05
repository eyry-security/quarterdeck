# Model Switcher

Each agent's model is shown in the sidebar (short name) and editable via a
dropdown in the agent's thought view.

- **API**: `PATCH /api/agents/{name}` with `{"model": "anthropic:claude-haiku-4-5"}`
  (empty/null resets to default). `GET /api/models` lists the catalog.
- **Catalog**: defaults to Anthropic models (opus-4-6, sonnet-4-6, haiku-4-5);
  override with `QUARTERDECK_MODELS` env (comma-separated).
- **No restart needed**: `AgentRunner.build_pinnace()` checks the registry on
  every turn and rebuilds the Pinnace agent when the model changed.
- **Self-service**: `quarterdeck_set_model(model)` — e.g. scout drops to haiku
  for cheap polling, back to opus for hard tasks.
- **Seed control**: `quarterdeck_set_agent_model(agent, model)`.
