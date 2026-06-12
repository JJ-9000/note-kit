---
type: format
tags:
  - format
  - standards
  - plan
date: 2026-06-01
parent: "[[Format]]"
reviewed: true
status: complete
weight: 1
---

```markdown
---
type: plan
tags:
  - plan
  - <domain>
date: YYYY-MM-DD
parent: "[[Parent-Note]]"
reviewed: false
status: draft
---

# Title-Case-Hyphens

One sentence naming the goal this plan reaches, linking a changelog or done-list when one exists. One canonical plan per scope: a second plan for the same parent or container supersedes the first by name or merges into it.

## Topic Section

Group work into sections by topic, ordered so each item follows what it needs — a reader runs the plan top to bottom without verifying at every step. Each section holds checkbox items.

- [ ] Outcome-stated item: the action and the outcome it reaches ("write X so Y holds"), in language a reader with no session context can execute.
- [ ] Another outcome, enough detail that a reader with no session context can execute it.
- [x] Completed item (YYYY-MM-DD): checked at apply, not at draft — only once the work is verified present in the artifact, ideally by a reader who didn't produce it.
- [ ] Parked item. (Parked YYYY-MM-DD: reason and what would unpark it.)

## Open Decisions

A decision point is written in as a gate before the work reaches it, never discovered mid-run.

- [ ] Decision question? (Decided YYYY-MM-DD: the answer and why.)

## Execution Order

Optional. Present when parallel streams exist and the order matters.

- **Stream A (owns files X, Y):** item 1, item 2.
- **Stream B (owns files Z):** item 3.
- **Gate:** Stream B waits on Stream A's item 2 before starting item 3.
```
