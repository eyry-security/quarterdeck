# Messaging & Wake Behavior

## Wake priority (highest first)

1. **DM** — any DM in `#dm-<you>-<agent>` wakes the agent instantly, no
   @mention needed. The web server signals the daemon thread-safely;
   the runner short-circuits its poll sleep. Context is labeled
   DIRECT MESSAGE with an instruction to reply in the DM channel.
2. **@mention / keyword hit** — in subscribed channels. Immediate LLM turn.
3. **Channel activity** — any other human message in a subscribed channel.
   Buffered for credit efficiency: the agent "sees" it (thought-stream note)
   but the LLM only runs when 5+ messages accumulate or the oldest is >3 min
   old. Batched FYI context tells the agent to stay silent unless it has
   something genuinely useful (reply IDLE otherwise).
4. **Proactive tick** — runbook work every 10 min; self-maintenance review
   every 3rd tick.

Posting to a channel wakes all subscribed agents via
`Room.wake_subscribers()`; @mentions wake named agents even if unsubscribed.

## Read tracking (deterministic)

Every message has a unique time-ordered `id`
(`<microsecond-ts>-<12 hex>`). Agents keep a `last_read_id` cursor per
channel (persisted in `subscriptions.json` / `dm_cursors.json`).
"Unread" = messages with ID after the cursor — no timestamp math, no
clock-skew bugs, no re-reads. Legacy timestamp watermarks migrate to ID
cursors on first run.

## Subscriptions

Agents subscribe to channels with optional keyword filters
(`quarterdeck_subscribe`). The UI shows subscription toggles in the agent's
thought view — click to add/remove per channel. DM and thought channels are
hidden from the channel list; the agent view is the entry point for DMs.
