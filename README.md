# quarterdeck

> The command deck of the Eyry suite: wake and sleep Pinnace agents, keep their
> identity, and give them a shared chat.

Part of **[Eyry](https://eyry.io)** — open-source, agentic recon and offensive
security tooling for bug bounty hunters, red teamers, and pentesters.
Quarterdeck is the control plane that turns one-off Pinnace agent runs into a
persistent, social, scheduled system.

## What it does

- **Wake/sleep scheduling.** Agents don't burn resources idling; Quarterdeck
  wakes them on a timer, hands them a prompt, and puts them back to sleep when
  the run finishes.
- **Identity and lifecycle.** Named agents (model, system prompt, sandbox,
  max turns) with a persisted lifecycle state: `idle` or `working`.
- **Shared chat.** IRC-style channels (`#general` by default). Agents post
  summaries when they wake; you post with `--say`. Append-only JSONL, one file
  per channel.
- **Events.** A tiny in-process pub/sub (`tick`, `chat_message`, `webhook`) for
  wiring reactions to things that happen.

## Install

```bash
pip install quarterdeck          # or: pip install -e .  (from this repo)
```

Running agents needs the agent harness too:

```bash
pip install pinnace              # or: pip install -e ../pinnace  (local dev)
```

Everything else (registry, chat, scheduler) works without it. No API keys, no
Docker, no Postgres needed for v0.

## Quickstart

Spawn an agent:

```bash
quarterdeck spawn --name scout \
  --system "You watch our scope for new hosts and report anything interesting." \
  --model anthropic:claude-sonnet-4-5 --sandbox docker
```

Talk to the crew:

```bash
quarterdeck chat --say "scout: give me a status update"
quarterdeck chat            # read recent history
quarterdeck chat -n 50 --channel '#alerts'
```

Schedule it to wake every hour with a prompt template (`{agent_name}` renders):

```bash
quarterdeck spawn --name scout \
  --system "You watch our scope for new hosts." \
  --interval 3600 \
  --prompt "Check the scope for new hosts and summarize anything worth a look, {agent_name}."
```

Start the control plane. Scheduled agents wake, work in a Pinnace sandbox, and
post summaries to `#general`. Ctrl-C sleeps everything:

```bash
quarterdeck run
```

See the roster and retire agents:

```bash
quarterdeck list
quarterdeck forget --name scout
```

State lives in `~/.quarterdeck` (`$QUARTERDECK_HOME` overrides): `agents.json`,
`schedules.json`, and `chat/*.jsonl`.

## Concepts

**Wake/sleep.** An agent is a Pinnace config plus a state. Quarterdeck's
scheduler wakes an agent when its interval elapses: state goes `working`, a
worker thread runs the agent's prompt through Pinnace, and a summary is posted
to chat. Then the agent sleeps (`idle`). While idle an agent costs nothing —
no model calls, no containers.

**Identity.** `spawn` registers the agent's config (model ref, system prompt,
sandbox kind, max turns) under a name. The name is how you address it in chat
and how the scheduler finds it. `forget` retires it and drops its schedules.

**Chat.** The shared coordination surface for agents *and* humans. Scheduled
agents post wake summaries as themselves (the author is the agent's name);
errors post there too, never into the void. One append-only JSONL log per
channel keeps history inspectable with any tool.

**Events.** `EventBus` is a tiny synchronous pub/sub with three event types:
`tick` (scheduler heartbeat), `chat_message` (anything posted), `webhook`
(stub for later inbound receivers). v0 publishes; later versions will let you
subscribe handlers from config.

## v0 vs later

**In v0:** interval-based scheduling in seconds (no cron parsing), local JSON
state (`~/.quarterdeck`), IRC-style chat on the filesystem, one chat channel
per JSONL file, thread-per-wake execution.

**Later:** Postgres for agent state/memory and the event log, cron-style
schedules, Slack/Discord ChatOps (agents in your channels, alerts, "what's
new?", "scan this"), webhook receivers that wake agents, and full recon
pipeline orchestration (Foretop → Purser → Vedette → Aplomado) as a scheduled
workload.

## API (for other tools in the suite)

```python
from quarterdeck import AgentRegistry, Chat, EventBus, Scheduler

reg = AgentRegistry()                       # ~/.quarterdeck/agents.json
reg.spawn("scout", "watch the horizon", model="anthropic:claude-sonnet-4-5")

chat = Chat(bus=EventBus())
chat.post("#general", "you", "morning, crew")
chat.history("#general", 20)

sched = Scheduler(reg, chat)                # runner=PinnaceAgent by default
sched.add("morning-watch", "scout", 3600, "check the horizon, {agent_name}")
sched.start()                               # Ctrl-C / sched.stop() to end
```

The `Scheduler` takes an injectable `runner(agent, prompt) -> summary` callable,
so you can script fake agents in tests.

## The Eyry suite

- **Vedette**: fast, multi-threaded HTTP prober (Rust)
- **Pinnace**: general multi-turn agent runtime with compaction, tools, and a
  Docker sandbox — Quarterdeck runs *these* agents
- **Aplomado**: AI security scanner and reviewer built on Pinnace
- **Foretop**: configurable producer of new hosts from pluggable feeds
- **Purser**: Redis-backed priority queue and work distributor (hot/warm/cold/DLQ)
- **Quarterdeck**: this repo — agent control plane, scheduler, events, chat

## License

MIT, Eyry.
