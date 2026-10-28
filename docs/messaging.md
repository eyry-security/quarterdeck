# Messaging & Wake Behavior

## Wake priority (highest first)

1. **DM** — any DM in `#dm-<you>-<agent>` wakes the agent instantly, no
   @mention needed. The web server signals the daemon thread-safely;
   the runner short-circuits its poll sleep. Context is labeled
   DIRECT MESSAGE with an instruction to reply in the DM channel.
2. **Monitored channel wake** — the service-level monitor wakes the captain
   on an @mention, a message from the operator in a monitored channel, or a
   subscribed system event (scan completion/failure). Other agents wake on an
   @mention by name. The runner receives the triggering context and recent
   channel history.
3. **@mention / keyword hit** — in subscribed channels. Immediate LLM turn.
4. **Channel activity** — any other human message in a subscribed channel.
   Buffered for credit efficiency: the agent "sees" it (thought-stream note)
   but the LLM only runs when 5+ messages accumulate or the oldest is >3 min
   old. Batched FYI context tells the agent to stay silent unless it has
   something genuinely useful (reply IDLE otherwise).
5. **Proactive tick** — runbook work every 10 min; self-maintenance review
   every 3rd tick.

## Channel monitoring

`quarterdeck.monitoring.Monitor` runs in the service and consumes
`chat_message` plus system events from the shared event bus. It maintains an
effective monitored-channel set per agent:

- the captain `self` monitors every group channel it belongs to, channels it
  has subscribed to, and always `#general`;
- every other agent monitors its joined groups and explicit subscriptions.

Wake policy is deliberately narrower than "notify on everything": the
captain wakes on mentions, operator messages, and subscribed system events;
other agents wake only when named. Rapid messages are batched, wakes are
rate-limited by a per-channel cooldown, and the daemon's per-agent lock keeps
a single turn in flight per agent. The context delivered to the agent includes
the trigger and recent messages so it can respond, delegate, or stay silent.

Every wake is appended to `wake-log.jsonl` with agent, channel, trigger, and
message id. Inspect it through the service API:

```
GET /api/monitoring
GET /api/monitoring/wakes?agent=self&channel=%23ops
GET /api/monitoring/{agent}
POST /api/monitoring/{agent}   {"channels": ["#ops"], "events": ["scan.completed"]}
```

`monitoring.json` stores explicit monitored-set and event-subscription
overrides; without overrides the defaults above are computed live from group
membership and subscriptions. REST and websocket posts both flow through the
same `Chat.post` event, so neither can skip monitoring.

## Read tracking (deterministic)

Every message has a unique time-ordered `id`
(`<microsecond-ts>-<12 hex>`). Agents keep separate mention and activity
cursors per channel (persisted in `subscriptions.json`), plus DM cursors in
`dm_cursors.json`. One reader cannot consume another reader's messages.
"Unread" = messages with ID after the cursor — no timestamp math, no
clock-skew bugs, no re-reads. Legacy timestamp/shared watermarks migrate to
the new ID cursors on first run.

## Subscriptions

Agents subscribe to channels with optional keyword filters
(`quarterdeck_subscribe`). The UI shows subscription toggles in the agent's
thought view — click to add/remove per channel. DM and thought channels are
hidden from the channel list; the agent view is the entry point for DMs.
