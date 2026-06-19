# hooks

Three Python hooks Claude Code runs during a session. Two inject context (`load-rules` on prompts, `session-start-context` at session start); one (`config-sync`) runs a config sync silently after an edit to `CONFIG.md`. They're wired in `settings.json` — see Part 4 of the main README.

## What's in here

| File                       | Event              | What it does                                                                                                                                                                                  |
| -------------------------- | ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `load-rules.py`            | `UserPromptSubmit` | Reads `.claude/RULES.md` and injects the always-on rules on a cadence: the session's first prompt and every `rules-injection-period` prompts after (CONFIG § Rules injection, default 30), re-anchoring against long-session drift without per-prompt repetition. |
| `session-start-context.py` | `SessionStart`     | Asks the vault-search daemon what you're working on and injects a brief: active project, recent sessions, cited references, quality gaps.                                                     |
| `config-sync.py`           | `PostToolUse`      | Runs `sync_config` when an Edit/Write/MultiEdit touches `CONFIG.md`, regenerating the `CLAUDE.md`/`AGENTS.md`/`RULES.md` tables from the canon. Silent — prints nothing, exits 0.            |

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

## config-sync.py

The config-sync hook. It runs after every Edit/Write/MultiEdit tool call; when the edited file is `CONFIG.md`, it runs `scripts/sync_config.py` so every generated surface (orientation tables, `## Always-on rules` blocks, `RULES.md`, harness permissions) stays in sync with the canon. For any other file it exits immediately.

- **Silent by design:** prints nothing to stdout and exits 0 on every path, so it never injects context or re-invokes the session — the sync is a pure side effect.
- **Gating:** fires only when `tool_input.file_path` ends in `CONFIG.md`; a `CONFIG.md` edit made through any other mechanism does not trigger it (fails closed — no sync, never an error).
- **Timeout:** the `sync_config` subprocess is capped at 55s, just under the hook's 60s `settings.json` timeout.
- **Dependencies:** none (Python standard library).
- **Install:** Part 4 of the main README.
