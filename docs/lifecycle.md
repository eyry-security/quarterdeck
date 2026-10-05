# Agent Lifecycle

## Spawning

Seed only: `quarterdeck_register_agent(name, identify, runbook)` —
creates the registry entry, `identify.md` / `runbook.md` / `memory.md`,
writes a mechanical spawn-time `wakeup.json` (see `wakeup.py`), and starts
the agent's loop via the daemon.

## Deregistering

- **API**: `DELETE /api/agents/{name}`
- **Tool** (seed only): `quarterdeck_deregister_agent(name)`
- **UI**: deregister button in the agent's thought view (with confirmation)

What happens: the runner's loop is stopped cleanly, the supervisor is told
never to restart it, the registry entry is retired, and
`~/.quarterdeck/agents/<name>/` moves to
`~/.quarterdeck/archive/agents/<name>-<timestamp>/`. **Never deleted** —
files can be restored or inspected later.

Seed itself cannot be deregistered.

## Re-registering

Registering the same name again starts fresh (new files). Archived versions
remain under `archive/agents/` for reference.
