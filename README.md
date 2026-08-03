# quarterdeck

> Agent control plane: scheduler, event system, and IRC-style chat for persistent agents (Python).

Part of **[Eyry](https://eyry.io)**: open-source, agentic recon and offensive security tooling for
bug bounty hunters, red teamers, and pentesters. Quarterdeck is the command deck: it wakes and sleeps agents, keeps their identity and memory, lets them talk, and orchestrates the pipeline.

## Status

🚧 **Early development.** Structure and APIs will change. Star the repo to follow along, and
see [eyry.io](https://eyry.io).

## What it does

- Scheduler and event-driven system that wakes and sleeps agents
- Persistent agent identity and memory; IRC-style inter-agent chat
- Orchestrates the recon pipeline; ChatOps over Slack and Discord

## Install

Coming soon.

## The Eyry suite

- **Vedette**: fast, multi-threaded HTTP prober (Rust)
- **Foretop**: configurable producer of new hosts from pluggable feeds (certstream first)
- **Purser**: Redis-backed priority queue and work distributor (hot/warm/cold/DLQ)
- **Pinnace**: general multi-turn agent runtime with compaction, tools, and a Docker sandbox
- **Aplomado**: AI security scanner and reviewer built on Pinnace
- **Quarterdeck**: agent control plane, scheduler, events, IRC-style chat, and pipeline orchestration

## License

MIT, Eyry.
