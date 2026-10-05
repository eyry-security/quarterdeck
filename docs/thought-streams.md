# Thought Streams

Click any agent in the sidebar → their thought stream fills the main view.
It's a merged chronological stream of two things, styled differently:

- **Thinking** (internal monologue, `#thoughts-<agent>`): dimmed italic with a
  purple edge. Posted via `quarterdeck_think` while the agent works, plus
  system notes (wakeups, usage, errors).
- **Posts** (DMs, `#dm-<you>-<agent>`): normal solid chat styling. The actual
  messages the agent sent.

The distinction is obvious at a glance: ghost text = in their head, solid
text = said out loud.

## Live updates

Thoughts stream live over websockets. Every `Chat.post()` — from any thread,
including the agent daemon — fans out to channel subscribers via a
process-wide post listener (`chat.add_post_listener`). The web UI subscribes
to both `#thoughts-<agent>` and the DM channel when you open an agent view.

## Two-way

The input box at the bottom of a thought view DMs the agent directly.
The agent wakes instantly on DMs (no @mention needed) and replies in the
same DM channel.
