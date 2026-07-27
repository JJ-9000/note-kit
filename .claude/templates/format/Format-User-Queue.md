---
type: format
tags:
  - format
  - standards
  - queue
date: 2026-06-10
parent: "[[Format]]"
reviewed: true
status: complete
---

# Format User Queue

The shape of an item in `<user-queue>`. Three readers share it: the user decides by checking boxes, the note-kit-ui Decide bucket renders items in this shape as decisions, and the action-agent executes checked options. An item outside the shape surfaces only as a "needs reading" row, with nothing the user can act on in place.

An item poses a decision, not a yes/no acknowledgement: it names the concrete alternatives and marks the one the producer recommends, so the user chooses among specified outcomes rather than approving a lone box.

## Skeleton

```markdown
## <producer-agent> — YYYY-MM-DD

### <Item title — the judgment call, in plain words>

One context line: the file and the decision needed, answerable cold without opening anything.

- [ ] <Option A — fully specified outcome: exact destination, name, or value> (recommended)
- [ ] <Option B — fully specified alternative outcome>
- [ ] <Option C — a fill-in when the answer can't be enumerated: "File to this project: REPLACE-WITH-PROJECT-NAME">

_proposed: YYYY-MM-DD by <agent>_
```

## The shape

- An item is one `###` heading, one context line, and at least one `- [ ]` option. `##` headings are producer/date group headers and carry no options of their own.
- The context line reads in plain established vocabulary, answerable cold; prose between the heading and the first option is the item's context, and prose after the options begins carries no meaning to the parser.
- Every option names its concrete outcome — the exact destination, name, or frontmatter value — so a checked box executes with no further interpretation.
- **An option carries its substance on its own line.** Everything a checked option executes on — destination, name, scope, count — sits in the option's own text. A nested bullet beneath an option is not an option, because the parser reads only checkbox lines, and it renders nowhere in the Decide bucket: detail parked there never reaches the user. Fold it into the option line, or into the context line above the options.
- **A decision offers its alternatives.** A producer frames the choice as the concrete options the user picks among — two or more fully-specified outcomes — rather than a lone approve/reject box. A genuine binary still names both outcomes (do X, or leave it as Y), so the user reads a choice rather than an acknowledgement.
- **One judgment per item.** Every option on an item is an alternative resolution of the same judgment. A set of independently executable steps is a checklist, and a checklist routes to the `<plan>`, never the queue. A producer pass raises as many open decisions as its evidence genuinely yields — one item per decision, each independently evidenced (CONFIG § Queue protocol; the one-decision-per-pass cap was retired 2026-07-26). The rule binds every queue writer — filing, janitor, analyst, action-agent, and any skill surfacing a clarification.
- **One option carries a `(recommended)` marker** when the producer has a steer: the recommended outcome sits in the option text as `… (recommended)`. The user checks the recommendation to accept it or checks another option to override; the marker guides the choice without making it. At most one option per item is recommended.
- A fill-in field carries an explicit `REPLACE-WITH-<what>` placeholder on the option line the action reads it from, so the placeholder-demotion rule below holds the item open until it is filled. One item may carry several fields, collecting a destination and a name, or a date and a project, across its options in a single decision. A context-line field is authored once the mad-libs build lands (§ Parse contract) — until then it renders as static text and no demotion guards it. A derivable candidate appears as its own concrete option; the fill-in is the fallback for a value that cannot be enumerated.
- **A short-answer item asks in free prose.** A question whose answer is neither enumerable nor a single value carries one option holding a `REPLACE-WITH-ANSWER` field; the user writes prose in place of the placeholder and checks the box, and the action-agent reads the prose as the answer. The placeholder-demotion rule below applies unchanged.
- **A multi-select item executes every checked option.** An item whose context line carries the literal `select-many` is a set of independently selectable outcomes of one judgment (which of these to take), and the action-agent executes every `[x]` option instead of treating the first as the whole decision. Without the literal, one `[x]` decides the item.
- **A hold option is the one legal deferral.** An option reading `Hold — convert to a resume prompt on [[<plan>]]` defers the decision while naming its conversion: the `[x]` clears the line and writes a resume record on that plan — `- [ ] HOLD (YYYY-MM-DD) — <forward-looking resume prompt>` (CONFIG § Holds and approvals) — so the checked-item-never-lingers invariant holds. The prompt restates the blocked step as the opening move of a new session: a hold on files stuck behind a login becomes "Let's resume the <project>, starting by getting the files stuck behind my login." A bare "decide later" option remains illegal.
- A checked option whose field still holds an unedited placeholder demotes to open rather than executing. `REPLACE-WITH-` is the single field-declaration token for the one-option case, the short-answer, and a mid-item field.
- An advisory — something that needs the user's eyes but no choice — is an item whose single option is `- [ ] Acknowledged — clear this item`. Between enumerable choices, the fill-in field, the short answer, the hold, and the dismissal option, a compliant shape exists for any ask.
- States are `[ ]` open, `[x]` approved, `[-]` rejected. An item is open while it holds a selectable `[ ]` and no `[x]`; one `[x]` on a multiple-choice item is the action (`select-many` items excepted, above). An approved item stays in the Decide bucket dimmed, its fate labelled, until the action-agent's pass clears it from the file (0.4.15) — unchecking re-opens it.
- A resolved item leaves the queue; its outcome lives in the executing agent's log in `<logs>/<agent>/`. Results, answers, and FYIs live under `<logs>` and in inbox notes — the queue holds open decisions only.
- `_proposed: <date> by <agent>_` closes the item; the parser skips `_`-prefixed lines as context.

## One judgment, not a checklist

The worked bad example: one item titled "<project> build: gates I can't run in-session" carrying eight independent gate steps as options — rebuild the tool, run each verification pass, record the throughput number, and so on. No option resolves any other; a checked box answers nothing; and the same holds now invite one answer in the queue and a different one in session — the drift the single-home rule exists to prevent.

The legal decomposition of that session's output: the eight gates route to the project's `<plan>` as steps (machine gates and user-presence work belong to the plan's state, surfaced by the holds mechanism); the user-presence cluster becomes one resume record on that plan (`- [ ] HOLD (YYYY-MM-DD) — Let's resume the <project> build, starting with the tool rebuild`); and the queue receives each real judgment call the session left open, one item apiece.

## Parse contract

The note-kit-ui Now view's decision parser (`parseDecisions` / `isOpenDecision`) recognizes:

- option: `^(\s*[-*]\s+\[)(.)(\]\s+)(.*\S)(\s*)$` — a `-`/`*` bullet, one state char, non-empty text
- heading: `^(#{2,})\s+(.*\S)\s*$`
- decision: a heading plus the checkbox lines beneath it
- open: has a `[ ]` option and no `[x]`

A heading with body prose but no options renders as a "needs reading" row that opens the file (since 0.4.10); a bare heading directly above another heading is a group header. Only items in the canonical shape carry decisions the user can act on in place.

**Field rendering — deployed and pending.** The deployed bucket renders a `REPLACE-WITH-` placeholder on an option line as a text input whose value replaces it as the box checks (0.4.12) — the short-answer shape rides this today — and reads a `(recommended)` marker as plain option text. A field inside the context line renders as static text until the mad-libs capability ships — the parser and renderer gaining field recognition anywhere in an item, and the recommendation gaining its own styling, are a note-kit-ui build tracked on its own amendment note. The `select-many` literal is inert to the deployed parser — the item renders as ordinary checkboxes, and the execute-every-checked semantics live in the action-agent until the multi-select rendering lands with the change request filed on the note-kit-ui plan. A hold option parses as an ordinary option; its conversion is the action-agent's. Author with option-line placeholders until the mad-libs build lands.
