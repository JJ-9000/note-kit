# Vault

An Obsidian vault co-managed by an AI assistant and a human user. Drafts land in `<inbox>` with `reviewed: false`; the person reviews and sets `reviewed: true`; scheduled agents file approved items to their homes. `CONFIG.md` is canonical for types, folders, tags, actions, and protocols. This file orients a fresh session and never overrides it.

## Tool guidance

- If the `vault` MCP tools are attached, use them: `mcp__vault__vault_search` and `mcp__vault__vault_recall` for any vault lookup, before Glob/Grep. Read, Edit, Write for files; the link-integrity rename helper when a path change must keep inbound wikilinks.
- Sub-agents inherit this file and the memory hierarchy but not the always-on rules or the session-start brief; the built-in Explore and Plan agents skip even that inheritance. Name every retrieval tool a sub-agent needs — including `mcp__vault__vault_search` — in its spawning prompt, and inject a specific CONFIG table only when spawning Explore or Plan.

## Search guidance

- Search the vault before writing code, configuration, or detailed reference. A result above 0.85 that fits → copy it and cite the source path; no match → proceed and write the new work as a draft.

## Interaction guidance

User profile and preferences live here; vault interaction logic belongs in the CONFIG § Rules table (distributed below as `## Always-on rules`).

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

## Always-on rules
<!-- note-kit:sync always-on-rules — auto-generated from CONFIG.md; do not edit between these markers -->
_Generated from CONFIG.md by sync_config — do not hand-edit; edit CONFIG.md instead._

- Your output is a draft: write it to `<inbox>` with `reviewed: false`.
- Never delete or destructively edit vault content outside of the `<inbox>`; version and `<archive>` it instead.
- Editing a `reviewed: true` note resets it to `reviewed: false` in the same change, and moves it back to the `<inbox>`. Exceptions live in CONFIG: a living document (cover, canonical plan) is maintained in place, and a queue-approved frontmatter repair keeps its approval.
- Give every note a `type` and a link up (`parent` or `project`) so nothing is orphaned. Stamp only a link that resolves — search first; no match means leave it empty for inference, never invent a name.
- Search the vault before writing anything new (`mcp__vault__vault_search` before Glob/Grep); never invent names, fields, or mechanisms.
- Assets carry no frontmatter.
- Artifacts stand alone: no conversation residue, changelog, attribution, or next-step notes in draft files.
- Procedural text instructs; knowledge text declares; both in plain words.
- A question asked in chat that goes unanswered lands in the `<user-queue>` before the session ends.
- Sub-agents inherit none of this — name every tool and rule they need in the spawning prompt.
- Maintain any active plans, crossing out finished work and correcting course on altered targets.
- Use the vault's configured skills (§ Skill slugs) when alias actions are invoked (planning, researching, etc.).
- All edits to vault content outside of `<inbox>` check with the destination's mission, plans, and project scope; alterations are logged in `<archive>`.
- On a repeated correction or lengthy confusion, stop and raise the issue in the `<user-queue>` to clarify intent rather than guessing again.
<!-- /note-kit:sync always-on-rules -->

