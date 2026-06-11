# Vault

An Obsidian vault co-managed by an AI assistant and a person. Drafts land in `<inbox>` with `reviewed: false`. The person reviews and sets `reviewed: true`. Scheduled agents file approved items to their permanent homes. `CONFIG.md` is the canonical source for types, folders, tags, actions, and protocols; this file orients a fresh runner and never overrides it.

The whole kit installs to `<vault>/.claude/` — `CONFIG.md`, `CLAUDE.md`, `AGENTS.md`, and `RULES.md` all sit there, alongside `skills/`, `scheduled-tasks/`, `scripts/`, and `hooks/`. This file, `AGENTS.md`, is the orientation for non-Claude runners; point a non-Claude runner at `.claude/AGENTS.md`. (Claude Code does not read it — it loads `CLAUDE.md`.) `RULES.md` is the always-on rules file the runner's prompt hook injects on a cadence — the session's first prompt and every `rules-injection-period` prompts after (CONFIG § Rules injection, default 30) — so the obligations re-anchor through long sessions; a runner without hook support reads it once at orientation.

## Tool guidance

- Use the vault search tool (`mcp__vault__vault_search`, or the runner's equivalent semantic index) before generic file globbing or grepping for any vault lookup.
- Standard read/write/edit tools for file operations. Use the link-integrity rename helper when a path change must preserve inbound wikilinks.
- Sub-agents do not receive the always-on rules or the session-start brief; they do inherit this orientation file and the memory hierarchy where the runner supports it (some built-in exploration/planning agents skip that inheritance). Name every retrieval tool they need — including the vault search tool — in the spawning prompt regardless; inject a specific CONFIG table only for an agent that does not see this file.

## Search guidance

- Before writing code, configuration, or detailed reference: search for a one-line description of the problem, read the top 3 results.
- A result scoring above 0.85 that matches the task → copy-paste it and cite the source path.
- No match → proceed, and write the new solution as a draft for review.

## Interaction guidance

- Generated output is a draft. Write it to `<inbox>` with `reviewed: false`; the person owns whether it is correct, the agents own where it goes.
- Editing a `reviewed: true` document resets it to `reviewed: false` in the same change.
- Escalation ladder:

  | correction | response |
  |---|---|
  | first | adjust immediately |
  | second | stop and ask the person to clarify intent |
  | third | wrap the session via handoff and start fresh |

## Standards

A **standard** is a resolute, vault-wide principle stored as its own note on one of three axes: **voice** (how prose reads), **design** (how things work or are organized), **format** (how things look — including each note type's canonical shape, the `Format-<Type>` notes). They live in the areas standards homes, or a project's matching subfolder when project-scoped. When they are consulted: producers fetch the relevant `Format-<Type>` before authoring a typed note (CONFIG § Format notes); the review skill's crit lanes audit against all three axes, heaviest `weight` first; the analyst checks filed notes against them for drift. A correction the user gives that holds beyond one file is a new standard or an addendum onto an existing one — never a one-off fix.

## Session-start defaults

<!-- note-kit:sync session-start — auto-generated from CONFIG.md; do not edit between these markers -->
_Generated from CONFIG.md by sync_config — do not hand-edit; edit CONFIG.md instead._

| type | typical folder | naming pattern | required frontmatter | description |
| ---- | -------------- | -------------- | -------------------- | ----------- |
| project | `<projects>/` | Title-Case-Hyphens | type, tags, date, parent | active work with a defined goal, start, and end |
| area | `<areas>/` | Title-Case-Hyphens | type, tags, date, parent | active role or system maintained without start or end |
| reference | `<reference>/` | Title-Case-Hyphens | type, tags, date, parent | canonical knowledge valuable with or without context. Used to form larger ideas by linking together references in active sessions. |
| voice | parent's `<voice>/` subfolder, else `<areas>/<voice>/` | Title-Case-Hyphens | type, tags, date, parent | resolute prose-voice principle for human-facing text |
| design | parent's `<design>/` subfolder, else `<areas>/<design>/` | Title-Case-Hyphens | type, tags, date, parent | resolute design principle for skills, agents, and specs |
| format | parent's `<format>/` subfolder, else `<areas>/<format>/` | Title-Case-Hyphens | type, tags, date, parent | resolute visual-format principle, and the canonical shape of a note type |
| research | parent's `<research>/` subfolder, else `<areas>/<research>/` | Title-Case-Hyphens | type, tags, date, project parent | investigation, experimentation; unproven |
| session | parent's `<sessions>/` subfolder, else `<archive>/<sessions>/` | YYYY-MM-DD-kebab-slug | type, tags, date, project | system-authored work log |
| plan | parent's `<plans>` subfolder, else `<areas>/<plans>/` | Title-Case-Hyphens | type, tags, date, parent | forward-looking mission and execution |
| note | parent's `<notes>/` subfolder, else `<areas>/<notes>/` | Title-Case-Hyphens | type, tags, date, parent | default plain-language markdown |
| journal | `<areas>/<journal>/` | YYYY-MM-DD-kebab-slug | type, tags, date, parent | self-authored personal reflection |
| idea | parent's `<idea>/` subfolder, else `<areas>/<ideas>/` | Title-Case-Hyphens | type, tags, date, parent | single-shot capture of a spark |
| snippet | `<snippets>/` | kebab-case | type, tags, date, parent | functional code, ready to paste |
| source | parent's `<sources>/` subfolder, else `<areas>/<sources>/` | Title-Case-Hyphens | type, tags, date, parent | external artifact supporting another document |
| index | the folder it covers (its `00-` or `01-` cover note, § Numbering) | Title-Case-Hyphens | type, tags, date | a folder's cover note linking **every** child for scoped navigation; only the direct children of a root (project, area, reference domain, snippet group) carry one — never a deeper subfolder or the archive |
| addendum | `<inbox>/` if unreviewed, `<archive>/<inbox>/<addendum>/` when completed | Title-Case-Hyphens | type, tags, date, target | transient edit merging into its target |
| log | parent's `<logs>/` subfolder, else `<archive>/<logs>/<agent> or <skill>/` | kebab-case | type, tags, date | operational log scoped to an agent or session run |
| revision | target's `<notes>/<revision>-<version>` subfolder, else `<archive>/<target>/<revisions>/` | Title-Case-Hyphens | type, tags, date, target | edited working copy produced by the review skill |
<!-- /note-kit:sync session-start -->

## Scheduled agents

<!-- note-kit:sync scheduled-agents — auto-generated from CONFIG.md; do not edit between these markers -->
_Generated from CONFIG.md by sync_config — do not hand-edit; edit CONFIG.md instead._

| agent | recommended cadence | scope |
| ----- | ------------------- | ----- |
| note-kit-janitor-agent | daily | whole vault, per file |
| note-kit-filing-agent | daily | inbox file vs. its destination |
| note-kit-analyst-agent | weekly | whole vault, macro |
| note-kit-action-agent | hourly | both queues and all of `<outbox>` |
<!-- /note-kit:sync scheduled-agents -->

