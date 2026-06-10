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

# Format User Queue

The shape of an item in `<user-queue>`. Three readers share it: the user decides by checking boxes, the note-kit-ui Decide bucket renders items in this shape as decisions, and the action-agent executes checked options. An item outside the shape surfaces only as a "needs reading" row, with nothing the user can act on in place.

## Skeleton

```markdown
## <producer-agent> — YYYY-MM-DD

### <Item title — the judgment call, in plain words>

One context line: the file and the decision needed, answerable cold without opening anything.

- [ ] <Option A — fully specified outcome: exact destination, name, or value>
- [ ] <Option B — fully specified outcome>
- [ ] <Option C — a fill-in when the answer can't be enumerated: "File to this project: REPLACE-WITH-PROJECT-NAME">

_proposed: YYYY-MM-DD by <agent>_
```

## The shape

- An item is one `###` heading, one context line, and at least one `- [ ]` option. `##` headings are producer/date group headers and carry no options of their own.
- The context line reads in plain established vocabulary, answerable cold; prose between the heading and the first option is the item's context, and prose after the options begins carries no meaning to the parser.
- Every option names its concrete outcome — the exact destination, name, or frontmatter value — so a checked box executes with no further interpretation.
- A fill-in option carries an explicit `REPLACE-WITH-<what>` placeholder; the Decide bucket renders the placeholder as a text input whose value replaces it as the box checks (0.4.12), and a checked option with an unedited placeholder demotes to open rather than executing. A derivable candidate appears as its own concrete option; the fill-in is the fallback.
- An advisory — something that needs the user's eyes but no choice — is an item whose single option is `- [ ] Acknowledged — clear this item`. Between enumerable choices, the fill-in option, and the dismissal option, a compliant shape exists for any ask.
- States are `[ ]` open, `[x]` approved, `[-]` rejected. An item is open while it holds a selectable `[ ]` and no `[x]`; one `[x]` on a multiple-choice item is the action.
- A resolved item leaves the queue; its outcome lives in the executing agent's log in `<logs>/<agent>/`. Results, answers, and FYIs live under `<logs>` and in inbox notes — the queue holds open decisions only.
- `_proposed: <date> by <agent>_` closes the item; the parser skips `_`-prefixed lines as context.

## Parse contract

The note-kit-ui Now view's decision parser (`parseDecisions` / `isOpenDecision`) recognizes:

- option: `^(\s*[-*]\s+\[)(.)(\]\s+)(.*\S)(\s*)$` — a `-`/`*` bullet, one state char, non-empty text
- heading: `^(#{2,})\s+(.*\S)\s*$`
- decision: a heading plus the checkbox lines beneath it
- open: has a `[ ]` option and no `[x]`

A heading with body prose but no options renders as a "needs reading" row that opens the file (since 0.4.10); a bare heading directly above another heading is a group header. Only items in the canonical shape carry decisions the user can act on in place.
