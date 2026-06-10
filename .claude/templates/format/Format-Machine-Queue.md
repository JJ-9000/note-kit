---
type: format
tags:
  - format
  - standards
  - queue
date: 2026-06-10
parent: "[[01-Format]]"
reviewed: true
status: complete
---

# Format Machine Queue

The shape of `<machine-queue>` — the user→AI mirror of [[Format-User-Queue]]. The user writes it; the action-agent reads each open item hourly and the note-kit-ui Queue bucket renders it as a live checklist.

## Skeleton

```markdown
# Machine Queue

- [ ] <One action per line, plain imperative: what to do and to what>
- [ ] <Another action> *(needs-live-session: <why it can't run unattended>)*
- [x] <A completed item — checked by the action-agent after the action finished and was logged>
```

## The shape

- The file is a flat checklist: every `- [ ]` line is one routable action, read wherever it sits — headings carry no meaning here.
- Only checkbox lines reach the machine reader; surrounding prose is inert. Material larger than a one-line instruction (notes, transcripts, briefs, links) lives as a file dropped in `<outbox>`, which the action-agent owns and routes.
- `[x]` is a completion receipt: set by the action-agent after the action finished and was logged to `<logs>/<agent>/`, visible for one pass, cleared by the next run's sweep.
- An item that cannot run unattended carries an inline `needs-live-session` annotation and stays `[ ]` until a live session executes it.
- A failed item keeps its `[ ]` and gains the reason; it waits for the next cycle or the user's edit.
