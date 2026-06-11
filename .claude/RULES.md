# Always-on rules

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
