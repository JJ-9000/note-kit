<!-- generated from CONFIG.md § Rules by sync_config — edit the CONFIG table, not this file -->
# Always-on rules

- Your output is a draft: write it to `<inbox>` with `reviewed: false`.
- Never delete or destructively edit vault content outside of the `<inbox>`; version and `<archive>` it instead.
- Edit a `reviewed: true` note → reset to `reviewed: false`, back to `<inbox>` (exceptions in CONFIG).
- Every note: `type` + a resolving link up (`parent`/`project`); no match → leave empty, never invent.
- `mcp__vault__vault_search` before writing anything new; never invent names, fields, or mechanisms.
- Assets carry no frontmatter.
- Artifacts stand alone: no conversation residue in drafts.
- Procedural text instructs; knowledge text declares; both in plain words.
- An unanswered chat question lands in `<user-queue>` before session end.
- Sub-agents inherit none of this — name every tool and rule in the spawning prompt.
- Maintain any active plans, crossing out finished work and correcting course on altered targets.
- Alias actions (plan, research, …) run their configured skill (§ Skill slugs).
- Edits outside `<inbox>`: check the destination's mission, plans, scope; log alterations in `<archive>`.
- Repeated correction → stop and raise it in `<user-queue>`, not another guess.
