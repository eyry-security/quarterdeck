# quarterdeck

> The command deck of the Eyry suite: persistent agent identity, event-driven orchestration, and ChatOps.

Quarterdeck turns one-off [Pinnace](https://github.com/eyry-security/pinnace) runs into named agents that wake on schedules, events, or explicit chat mentions. It keeps local state inspectable, routes Aplomado findings into alerts or agent work, and puts results back into IRC-style channels.

## Status

**Single-node v0.** Agent registry, interval scheduler, durable event history, exact-type rules, local ChatOps, and Aplomado event ingestion work. Authenticated Slack/Discord connectors, cron, SQLite/Postgres migration, and multi-node workers are later work; Quarterdeck does not ship an insecure webhook stub.

## Install

```bash
pip install -e ../pinnace
pip install -e '.[pinnace]'
```

Pinnace resolves the model. The default is `anthropic:claude-opus-4-6`; set `PINNACE_MODEL` or pass `--model provider:model` when spawning an agent.

## Start a crew

```bash
quarterdeck spawn --name scout \
  --system "Watch the supplied scope and report evidence, not guesses."

# One scheduled wake per hour
quarterdeck spawn --name reporter \
  --system "Summarize supplied security events." \
  --interval 3600 --prompt "Review the latest status, {agent_name}."

quarterdeck list
quarterdeck run
```

`quarterdeck run` stays active even when no schedules exist so in-process chat/event routing remains available. Ctrl-C stops new work, cancels queued wakes, and waits up to the shutdown grace period for active work.

## Run and chat

```bash
# One explicit run
quarterdeck wake --agent scout --prompt "Summarize current work"

# Ordinary chat is stored but does not wake anything
quarterdeck chat --say "morning crew"

# An explicit human mention wakes exactly one named agent
quarterdeck chat --channel '#ops' --say '@scout give me a status update'
quarterdeck chat --channel '#ops' -n 50
```

Only messages authored as the local human (`you`) are routed. Agent and Quarterdeck-authored messages never trigger another agent, preventing self-echo and agent-to-agent chat loops. Routed text is base64-encoded and labeled as untrusted data before it reaches Pinnace, so message text cannot forge prompt boundaries.

Local-sandbox agents execute model-generated commands on the host. Unattended and one-shot runs refuse them unless `--allow-local` is explicit. Docker is the normal runtime.

## Scheduling and sessions

Quarterdeck uses a bounded worker pool and a lock per agent, so two schedules, events, or chat messages can queue concurrently without running the same Pinnace session at the same time. Each run emits:

- `agent.run.requested` with run, agent, schedule, and channel IDs.
- `agent.run.completed` with the same run ID and succeeded/failed status.

Pinnace sessions live below `$QUARTERDECK_HOME/pinnace/sessions` (default `~/.quarterdeck/pinnace/sessions`) and use collision-resistant keys derived from the full legacy-compatible agent name. In-process locks and host file locks serialize the same session across independent Quarterdeck commands. Sandboxes close on success, model failure, and agent-construction failure.

## Durable events and rules

Events are committed to `events.jsonl` before in-process delivery:

```json
{
  "type": "host.probed",
  "payload": {"host": "api.example.com", "status": 200},
  "ts": 1791158400.0,
  "id": "producer-event-id",
  "source": "vedette",
  "schema_version": 1
}
```

IDs are claimed under a host file lock, fsynced before delivery, and deduplicated across process restarts and concurrent local commands. Handler outcomes are recorded in `event-deliveries.jsonl`; failed handlers can be replayed through `EventBus.replay()`. Only schema version 1 is accepted. The service remains intentionally single-host; distributed leases are not claimed.

```bash
quarterdeck event --type host.probed \
  --id probe-123 --source vedette \
  --payload '{"host":"api.example.com","status":200}'
quarterdeck events --type host.probed --json

quarterdeck rule-add --name review-new-host \
  --event host.probed --action agent --agent scout \
  --prompt 'Review this event: {event_json}' --channel '#ops'
quarterdeck rule-add --name finding-alerts \
  --event aplomado.scan.completed --action alert --channel '#alerts'
quarterdeck rule-list
quarterdeck rule-remove --name review-new-host
```

Rule payloads are base64-encoded and labeled as untrusted model data. Causation IDs and a four-hop ceiling prevent event→agent→completion rule cycles from running forever. Filters are exact top-level payload equality in the Python API; richer policy comes later.

## Aplomado events

Quarterdeck consumes Aplomado’s producer-owned envelope without importing the Aplomado package:

```json
{
  "type": "aplomado.scan.completed",
  "id": "<uuid4>",
  "producer": "aplomado",
  "timestamp": "<ISO-8601 UTC>",
  "data": {
    "target": "https://api.example.com",
    "summary": "One confirmed issue.",
    "scanned_at": "<ISO-8601>",
    "findings": [],
    "ok": true,
    "error": null,
    "metadata": {}
  }
}
```

The adapter validates the required outer fields, UUID4 identity, and minimum report shape; preserves the original producer document plus unknown report/finding fields; and uses the producer UUID4 for deduplication. Alert rules summarize severity counts and include high/critical titles without rewriting the source report.

```bash
# File or live JSONL pipeline
quarterdeck ingest-aplomado --file findings-events.jsonl
aplomado scan --target https://api.example.com --event-sink - \
  | quarterdeck ingest-aplomado --file -
```

Malformed lines are reported and later events continue. Duplicate producer IDs are retained once.

## State

`$QUARTERDECK_HOME` defaults to `~/.quarterdeck`:

- `agents.json` — named Pinnace configuration and lifecycle state.
- `schedules.json` — interval schedules.
- `rules.json` — exact event rules.
- `events.jsonl` — typed event history and run lifecycle.
- `event-deliveries.jsonl` — handler success/failure attempts for replay.
- `chat/<channel>.jsonl` — local channel history.
- `pinnace/sessions/` — resumable agent transcripts.

Agent names remain backward-compatible with v0. Quarterdeck derives a collision-resistant hash for filesystem locks and Pinnace session IDs, so legacy names such as `team/scout` and `team_scout` cannot collapse onto one transcript.

## CLI

```text
quarterdeck spawn|list|forget
quarterdeck wake
quarterdeck chat
quarterdeck run
quarterdeck event|events
quarterdeck rule-add|rule-list|rule-remove
quarterdeck ingest-aplomado
```

Existing v0 `spawn`, `list`, `chat`, `run`, and `forget` state files and command shapes remain supported.

## Python API

```python
from quarterdeck import AgentRegistry, Chat, EventBus, EventStore, Scheduler

home = "/srv/quarterdeck"
bus = EventBus(store=EventStore(home))
registry = AgentRegistry(home=home)
chat = Chat(home=home, bus=bus)
scheduler = Scheduler(registry, chat, bus=bus, home=home)

registry.spawn("scout", "Review supplied events and report evidence.")
scheduler.wake("scout", "Check status", "#ops").result()
scheduler.stop()
```

The scheduler accepts an injectable `runner(agent, prompt) -> summary`, so all orchestration tests run without Docker, API keys, or network.

## Tests

```bash
pytest
```

## License

MIT, Eyry.
