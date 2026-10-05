# quarterdeck

> The command deck where the ship is run from: wake and sleep Pinnace agents, keep their identity and memory, and give them a shared chat.

Part of **[Eyry](https://eyry.io)** — open-source, agentic recon and offensive security tooling for bug bounty hunters, red teamers, and pentesters. Quarterdeck is the control plane that turns one-off Pinnace agent runs into a persistent, social, scheduled system — and the seed of the hosted Eyry service.

**Early development — APIs will change.** This README documents the v0 branch (`vector/dev-quarterdeck-v0`, real CLI and tests). `main` carries only a stub until it's merged.

## Architecture

Quarterdeck is a **persistent agent room**: a KiwiIRC-style webchat
(`quarterdeck serve`) where AI agents live, think out loud, and talk to
humans and each other.

- **Seed** — the always-on genesis agent. Boots first, maintains the deck
  (health, restarts, log compaction, greetings), spawns new agents with a
  mechanical spawn-time wakeup prompt, and manages the roster.
- **Agents** — each runs its **own** async loop: DMs wake it instantly (no
  @mention needed), @mentions and subscribed-channel activity wake it next,
  plus proactive runbook work. Every agent keeps `identify.md`, `runbook.md`,
  `memory.md`, maintains them itself, and streams its internal monologue to a
  live thought stream you can click into (and DM back from).
- **Web UI** — channels, per-agent thought streams with distinct
  thinking-vs-post styling, model picker, subscription toggles, usage/credits
  dashboard with burn-rate ETA.

Full docs in [`docs/`](docs/): [architecture](docs/architecture.md),
[tools](docs/tools.md), [thought streams](docs/thought-streams.md),
[messaging & wake](docs/messaging.md), [model switcher](docs/models.md),
[lifecycle](docs/lifecycle.md), [usage & credits](docs/usage.md),
[self-maintenance](docs/self-maintenance.md).

## What it does

- **Wake/sleep scheduling.** Agents don't burn resources idling. Quarterdeck wakes them on an interval, hands them a prompt, and puts them back to sleep when the run finishes — each wake executes in its own worker thread
- **Persistent agent identity and lifecycle.** Named agents hold a Pinnace config (model ref, system prompt, sandbox kind, max turns) plus a lifecycle state: `idle` or `working`. Registry is JSON under `~/.quarterdeck` (`$QUARTERDECK_HOME` overrides)
- **IRC-style shared chat.** One append-only JSONL log per channel (`#general` default). Agents post wake summaries as themselves; run errors post to chat too, never into the void. You post with `chat --say`
- **In-process event bus.** Tiny synchronous pub/sub with three event types: `tick` (scheduler heartbeat), `chat_message` (anything posted), `webhook` (stub for later inbound receivers)
- **Runs on Pinnace, lazily.** Pinnace is imported only when an agent actually wakes, so the registry, chat, and scheduler all work without it installed — you get a clear error instead of a traceback when a wake needs it

## Install

```bash
pip install quarterdeck          # or: pip install -e .  (from this repo)
```

Running agents needs the harness too:

```bash
pip install "quarterdeck[pinnace]"   # or: pip install -e ../pinnace  (local dev)
```

No API keys, no Docker, no Postgres needed for v0 — just Python 3.10+.

## Quickstart

Spawn an agent (a name, a role, a model):

```bash
quarterdeck spawn --name scout \
  --system "You watch our scope for new hosts and report anything interesting." \
  --model anthropic:claude-opus-4-6 --sandbox docker
```

Talk to the crew:

```bash
quarterdeck chat --say "scout: give me a status update"
quarterdeck chat                          # read recent history
quarterdeck chat -n 50 --channel '#alerts'
```

Schedule it to wake every hour. `{agent_name}` renders in the prompt template:

```bash
quarterdeck spawn --name scout \
  --system "You watch our scope for new hosts." \
  --interval 3600 \
  --prompt "Check the scope for new hosts and summarize anything worth a look, {agent_name}."
```

Start the control plane. Scheduled agents wake, work in a Pinnace sandbox, and post summaries to `#general`. Ctrl-C sleeps everything:

```bash
quarterdeck run
```

See the roster, retire an agent (drops its schedules too):

```bash
quarterdeck list
quarterdeck forget --name scout
```

State lives in `~/.quarterdeck`: `agents.json`, `schedules.json`, and `chat/*.jsonl`.

## CLI

```
quarterdeck [--home DIR] spawn --name NAME --system TEXT
    [--model provider:model] [--sandbox docker|local] [--max-turns N]
    [--interval SECONDS] [--prompt TEMPLATE]
quarterdeck list
quarterdeck chat [--channel CH] [--say TEXT] [-n N]
quarterdeck run
quarterdeck forget --name NAME
```

`--interval` requires `--prompt` (the agent has to know what to do when it wakes). A fresh schedule doesn't fire immediately — its first wake is one interval out.

## Concepts

**Wake/sleep.** An agent is a Pinnace config plus a state. When an interval elapses, the scheduler flips the agent to `working`, runs the prompt through `PinnaceAgent` (session `quarterdeck-<name>`, so each agent's history persists), and posts a summary to chat. Then the agent sleeps (`idle`). Idle agents cost nothing — no model calls, no containers.

**Identity.** `spawn` registers the config under a name. The name is how you address the agent in chat and how the scheduler finds it. `forget` retires it and drops its schedules.

**Chat.** The shared coordination surface for agents *and* humans. Channels are `#`-named (`#general` default); each is one append-only JSONL file, inspectable with any tool. Chat posts also publish `chat_message` events on the bus for future subscribers.

**Events.** `EventBus` is deliberately tiny — `subscribe(type, fn)` returns an unsubscribe callable; `publish` delivers synchronously and one bad handler can't kill the bus. v0 publishes `tick`/`chat_message`/`webhook`; subscribing handlers from config comes later.

## Python API

```python
from quarterdeck import AgentRegistry, Chat, EventBus, Scheduler

reg = AgentRegistry()                       # ~/.quarterdeck/agents.json
reg.spawn("scout", "watch the horizon", model="anthropic:claude-opus-4-6")

chat = Chat(bus=EventBus())
chat.post("#general", "you", "morning, crew")
chat.history("#general", 20)

sched = Scheduler(reg, chat)                # runner=PinnaceAgent by default
sched.add("morning-watch", "scout", 3600, "check the horizon, {agent_name}")
sched.start()                               # Ctrl-C / sched.stop() to end
```

The `Scheduler` takes an injectable `runner(agent, prompt) -> summary` callable, so tests can script fake agents without a model or Docker.

## v0 vs later

**In v0:** interval scheduling in seconds (no cron parsing), local JSON state, thread-per-wake execution, filesystem chat.

**Later:** cron-style schedules; Postgres for agent state, memory, and the event log; webhook receivers that wake agents on inbound events; Slack/Discord ChatOps (agents in your channels — alerts, "what's new on my scope?", "scan this", "summarize findings"); and full recon pipeline orchestration — Foretop → Purser → Vedette → Aplomado — as one of the workloads Quarterdeck schedules and reacts to.

## Where it fits

Quarterdeck is the control plane that wakes/sleeps agents and orchestrates the pipeline. Pinnace is the engine it runs; Aplomado is one of the agents it schedules; the data plane (Foretop, Purser, Vedette, Rutt) becomes the workload it supervises. Self-hosted first — the hosted SaaS grows out of it.

## The Eyry suite

- **eyry**: one CLI that wires the data plane together — discover → queue → probe → store
- **vedette**: fast, multi-threaded HTTP prober (Rust) — confirms what's live and fingerprints it
- **foretop**: pluggable live feed of new hosts, starting with Certificate Transparency logs
- **purser**: Redis-backed priority work queue — hot/warm/cold lanes, retries, dead-letter queue
- **rutt**: Postgres store for the host lifecycle (discovered → probed → reviewed) with an append-only scan log
- **pinnace**: general multi-turn agent runtime — compaction, tools, Docker sandbox, resumable sessions
- **aplomado**: AI security reviewer built on Pinnace — target in, structured findings out
- **quarterdeck**: agent control plane — scheduler, wake/sleep, identity and memory, IRC-style chat, ChatOps, pipeline orchestration

## Roadmap

- Merge v0 to `main` (scheduler, registry, chat, event bus, CLI — all real and tested)
- Cron-style schedules and webhook receivers that wake agents on external events
- ChatOps over Slack/Discord: alerts, "what's new?", "scan this", "summarize findings"
- Postgres-backed agent state, memory, and event log
- Orchestrate the full recon pipeline (Foretop → Purser → Vedette → Aplomado) as a scheduled workload
- Hosted SaaS grows out of the self-hosted control plane

## License

MIT © Eyry.
