# hooks

Three Python hooks Claude Code runs at fixed moments in a session. Each prints JSON to stdout that Claude Code injects as context (or as a reminder). They're wired in `settings.json` — see Part 4 of the main README.

## What's in here

| File                       | Event              | What it does                                                                                                                                                                                  |
| -------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `load-rules.py`            | `UserPromptSubmit` | Reads `.claude/RULES.md` and injects the always-on rules on a cadence: the session's first prompt and every `rules-injection-period` prompts after (CONFIG § Rules injection, default 30), re-anchoring against long-session drift without per-prompt repetition. |
| `session-start-context.py` | `SessionStart`     | Asks the vault-search daemon what you're working on and injects a brief: active project, recent sessions, cited references, quality gaps.                                                     |
| `session-end-audit.py`     | `Stop`             | Post-session upkeep: runs `sync_config` when the session edited `CONFIG.md`; reminds Claude to re-check documentation when hooks, rules, skills, or `CLAUDE.md` changed; gates the stop on a handoff when the session edited vault content (≥3 files blocks, fewer reminds). |

## load-rules.py

The rule loader. Reads `.claude/RULES.md` and injects it on a cadence: the session's first prompt and every `rules-injection-period` prompts after (default 30 — prompts 1, 31, 61, …), so obligations re-anchor against long-session drift without repeating on every message. The period is read from `CONFIG.md` § Rules injection at each invocation; period 1 means every prompt. Prompt position is tracked per session in a counter file under the OS temp dir, keyed by `session_id`; on any state failure the hook fails open and injects. Only `RULES.md` is loaded here — vocabulary (types, folders, tags, actions) lives in `CONFIG.md`, and agent procedure lives in each agent's own `SKILL.md`.

- **Change an always-on rule:** edit the `CONFIG.md` § Rules table — `RULES.md` is generated from it by `sync_config` (the `reminder` column, full rule text where a cell is empty).
- **Change the cadence:** edit `rules-injection-period` in `CONFIG.md` § Rules injection.
- **Change vocabulary:** edit `CONFIG.md` § the relevant table.
- **Dependencies:** none (Python standard library).
- **Install:** Part 4 of the main README.

## session-start-context.py

The orientation hook. On session start it calls the vault-search daemon's `/api/session_brief` endpoint and formats the reply into a context block.

- **Depends on the vault-search daemon** (Part 5). Without it the hook still runs, injects a one-line "daemon may not be running" note, and Claude falls back to on-demand search.
- **Timeout:** 2 seconds.
- **Daemon address:** `http://127.0.0.1:8765` (hardcoded — change here if you moved the daemon's port).
- **Dependencies:** none (Python standard library).
- **Install:** Part 4 of the main README. Requires the daemon — see Part 5.

## session-end-audit.py

The post-session upkeep hook. When a session ends, it scans the transcript once and runs three jobs:

1. **Config sync** — if the session edited `CONFIG.md`, runs `scripts/sync_config.py` so every generated surface (orientation tables, `## Always-on rules` blocks, `RULES.md`, harness permissions) stays in sync with the canon.
2. **Documentation-drift guard** — if the session touched `.claude/hooks`, `.claude/skills`, `.claude/rules`, `scheduled-tasks`, `CONFIG.md`, or `CLAUDE.md`, reminds Claude to verify the documentation surfaces still reflect reality.
3. **Handoff gate** — if the session edited vault content (outside the kit root) and never ran a handoff skill: at `HANDOFF_BLOCK_THRESHOLD` (3) or more distinct files it blocks the stop until `note-kit-handoff` runs; below it, it reminds. Fails open — an unreadable transcript never blocks.

- **Configure your documentation surfaces:** edit the `SURFACES` list at the top of the file. Add a system-architecture doc, a hooks reference, etc., if you keep them.
- **Configure the handoff gate:** edit `HANDOFF_BLOCK_THRESHOLD` at the top of the file.
- **Dependencies:** none (Python standard library).
- **Install:** Part 4 of the main README.
