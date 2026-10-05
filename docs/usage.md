# Usage & Credits Dashboard

Pinnace meters every inference. Quarterdeck funnels those records into
`~/.quarterdeck/usage.jsonl` (`usage.py`) and serves aggregates.

## What is tracked

Per inference: timestamp, agent, model, input/output tokens, cost in USD.
Pinnace prices records itself; when it can't, Anthropic list-price fallbacks
apply (marked `cost_estimated: true`).

## API

- `GET /api/usage` → `{summary, burn_1h, burn_24h, eta}`
  - `summary`: total/input/output tokens, cost, per-agent breakdown
  - `burn_1h` / `burn_24h`: tokens/hour and $/hour over trailing windows
  - `eta`: credit balance, spent, remaining, projected runway hours
- `PUT /api/usage/balance` with `{"balance_usd": 25.0}` → set credit balance

## UI

The top bar shows `🔥 1.2M tok / $3.42 · ~4h left`. It turns amber under
8h of runway, red under 2h. Click for the full panel: totals, burn rates,
per-agent bars, and a credit-balance input.

## Credit balance

Anthropic has no billing API for this, so the balance is user-supplied:
`ANTHROPIC_CREDIT_BALANCE` env var wins, otherwise the UI setting (stored
in `~/.quarterdeck/settings.json`).

## Burn rate & ETA

ETA uses the **max** of the 1h and 24h burn rates (conservative):
`runway = (balance − spent) / burn_rate`. No balance set → no ETA.
