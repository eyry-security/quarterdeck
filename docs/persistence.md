# Persistence: what survives a restart

Quarterdeck agents run in Docker sandboxes. This page documents exactly
what persists across container restarts, daemon restarts, and droplet
reboots — and what doesn't.

## Persistent (survives everything)

| What | Where | Notes |
|---|---|---|
| Agent memory (`memory.md`) | `~/.quarterdeck/agents/<name>/memory.md` | Host filesystem. The agent's long-term learnings. |
| Runbook (`runbook.md`) | `~/.quarterdeck/agents/<name>/runbook.md` | Host filesystem. Procedures the agent follows. |
| Identity (`identify.md`) | `~/.quarterdeck/agents/<name>/identify.md` | Host filesystem. Who the agent is. |
| Prompt history | `~/.quarterdeck/agents/<name>/prompt_history/` | Versioned rewrites of the agent's own prompts. |
| Subscriptions | `~/.quarterdeck/agents/<name>/subscriptions.json` | Channel subscriptions + read cursors. |
| **`/work` contents** | `~/.quarterdeck/agents/<name>/work/` | Host-mounted into the container. Scratch files, downloads, tool output — all survive restarts. |
| Chat history | `~/.quarterdeck/` JSONL logs | Channel messages, DMs, thought streams. |
| Usage logs | `~/.quarterdeck/usage.jsonl` | Token/cost tracking for the dashboard. |

## Ephemeral (reset on container rebuild)

| What | Notes |
|---|---|
| Running processes | Killed when the container stops. |
| Installed packages (runtime) | Anything `apt-get install`ed inside a running container is lost on rebuild — but the base image (`qd-agent:latest`) already ships the toolchain, so this is rarely needed. |
| Environment variables (container) | Set per-container; the image defaults apply on rebuild. |
| `/tmp` inside the container | Standard tmpfs behavior. |

## The toolchain image

`quarterdeck/docker/Dockerfile` builds `qd-agent:latest` with the recon
toolchain baked in: `curl`, `wget`, `nmap`, `dnsutils` (dig/nslookup),
`whois`, `netcat`, `ping`, `traceroute`, `git`, `jq`, `unzip`, plus Python
`requests`, `httpx`, `beautifulsoup4`, `lxml`.

To add a tool permanently, edit the Dockerfile and rebuild:
```bash
cd quarterdeck/docker && docker build -t qd-agent:latest .
```
Then reset agents' environments to pick it up.

## Resetting the environment

Persistence is the default, but sometimes you want a clean slate:

- **Agent self-service:** `quarterdeck_reset_env()` tool — wipes your own `/work`, rebuilds from the clean image.
- **Seed managing others:** `quarterdeck_reset_agent_env(agent)` — same, for another agent.
- **UI:** "reset env" button in an agent's thought view (with confirmation).
- **API:** `POST /api/agents/{name}/reset-env`.

Reset wipes `/work` but never touches memory, runbook, identity, or chat
history — the agent's brain survives; only its scratch space is cleaned.

## For agent authors

- Put anything you need to keep in `/work` — it persists.
- Don't rely on runtime `apt-get install` — request toolchain additions to the Dockerfile instead.
- Your memory/runbook/identity are always safe; they're on the host, outside the container entirely.
