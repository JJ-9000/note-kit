# Always-on rules

- Your output is a draft: write it to `<inbox>` with `reviewed: false`.
- Never delete or destructively edit vault content outside of the `<inbox>`; version and `<archive>` it instead.
- Editing a `reviewed: true` note resets it to `reviewed: false` in the same change, and moves it back to the `<inbox>`. Exceptions live in CONFIG: a living document (cover, canonical plan) is maintained in place, and a queue-approved frontmatter repair keeps its approval.
- Give every note a `type` and a link up (`parent` or `project`) so nothing is orphaned. Stamp only a link that resolves — search first; no match means leave it empty for inference, never invent a name.
- Match the vault's existing vocabulary and shapes before inventing lingo — search first (`mcp__vault__vault_search` before Glob/Grep).
- Assets carry no frontmatter.
- Artifacts stand alone: no conversation residue, changelog, attribution, or next-step notes in draft files.
- Match voice to the artifact: procedural text (skills, agents, plans) instructs — say what to do, plainly; knowledge text (references, standards) states how things are — declarative, evergreen, no imperatives. Both in plain words, no backstory or padding.
- A question asked in chat that goes unanswered lands in the `<user-queue>` before the session ends.
- Name every note-kit specific tool sub-agents need in their spawning prompt, do not assume they can see these helper files and tools implicitly.
- Before authoring a re-written or original draft artifact, compare against the user's saved voice (prose), design (methodology) and format (visual) standards, and the type's `Format-<Type>` note.
- When uncertain or stuck, look through your notes. Archive, don't delete. New content is written to the inbox, `reviewed: false`.