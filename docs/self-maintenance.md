# Self-Maintenance

Agents keep themselves sharp. Every 3rd proactive tick (~30 min) the agent
reviews:

- **Memory bloat** — `memory.md` size shown in KB. If bloated:
  `quarterdeck_compact_memory()` archives the current file and returns it;
  the agent summarizes (keep learnings, drop chit-chat) and calls
  `quarterdeck_rewrite_memory()` with the compacted text.
- **Prompt accuracy** — are `identify.md` / `runbook.md` still true?
  `quarterdeck_rewrite_prompt(section, content)` rewrites one section.
  Also covers `system_prompt` (registry-level; rebuilds the Pinnace agent).

Every rewrite archives the previous version to
`~/.quarterdeck/agents/<name>/prompt_history/<section>-<timestamp>.md` —
nothing is ever lost, and the agent (or a human) can roll back.

The loop is: learn from conversations → `quarterdeck_memory_write` →
periodically compact → refine prompts → get better over time. All through
tools the agent calls itself.
