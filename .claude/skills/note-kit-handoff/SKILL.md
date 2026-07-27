---
name: note-kit-handoff
description: End-of-session deliverables — a session log plus any atomic reference notes and standards addendums the session earned. Invoke with /note-kit-handoff to wrap a working session. Trigger on "handoff", "wrap up", "end session", "session log", "write the handoff".
---

# handoff

Produce the session-end deliverables below. The session log is mandatory; everything else is written only when the session earned it — write what the session produced.

**Link what already exists; do not rewrite it.** When the session already committed a note, standard, or structural change directly to the vault, the log points to it by `[[wikilink]]` — never duplicate a file already on disk or reiterate its content. A session that did its writing live earns a thorough log and a plan, and few or no new notes.

**A note earns its place by being useful** — admitted only because a future reader would retrieve it. An atom no one would search for, a standard that restates a known rule, a plan that repeats the State-of-play line: none of these is owed.

## Modes

| invocation               | produces                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `/note-kit-handoff`      | the full set: session log + atomic notes + any standalone voice/design notes, a project-state addendum, and a plan |
| `/note-kit-handoff lite` | the session log only — for a routine continuation where nothing reusable surfaced                                  |

## Output container

Everything a handoff produces lands in one container `<inbox>/<date>-<topic>-session/` (CONFIG § Skill slugs). When the session earned tag-along files (atoms, standards, an addendum, a plan), the session log is the **gate file** at the container root: approving the gate auto-reviews the set and the filing-agent files it as a unit (CONFIG § Group approval). When the log stands alone — no tag-alongs — it is not a gate, just the session note; it reviews on its own and carries no gate framing.

## 1 — Session log

Write to the container root: `<inbox>/<date>-<topic>-session/<YYYY-MM-DD>-<slug>.md`, `type: session`, `project: "[[Name]]"` (naming per CONFIG § Types); the slug names the session topic. The log opens with the decision header — at most five lines: what this set adds or changes, what deserves a real read, and — only when it gates tag-along files — what approval triggers (CONFIG § Group approval). A session log that stands alone names no gate and triggers no set approval. Stamp `project:` only after confirming the target exists — one vault search or project listing check; no match → leave the field empty for downstream inference, never invent a name.

Target length is ~600 words, per the session format note (CONFIG § Format notes): Successes and Failures stay concrete; Progress trims to decisions. Trim the draft to that target before writing the file — cut narration and anything a linked file already carries, keeping the concrete detail.

| section        | holds                                                                                                                              |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Successes      | approaches that worked, named concretely enough to repeat — node paths, functions, the working technique                           |
| Failures       | every wrong assumption, misleading error, and failed attempt: what was tried, what happened, the fix, why the assumption was wrong |
| Progress       | what moved — scope as stated at session start and whether it changed, decisions made and the reason behind each                    |
| Relevant files | files, file paths, versions touched this session, as wikilinks where they are vault notes                                          |
| State of play  | the exact resume point — last working node, current blocker, what the next session opens with                                      |

Write for a reader with zero context. Name the function, the parameter, the path, the error string. "Naming was confusing" is useless; "the counter resets to 0 when the accessor is called before the initializer runs" is useful — name the actual symbols in your own case. If nothing went wrong, the Failures section says so — do not invent one.

**Originating idea.** If this session grew from an `idea` note or `plan`, stamp that file `status: in-progress` and `session: "[[<this session log>]]"`, and link it from Relevant files. Only handoff knows which idea or plan the session acted on, so only handoff can set this. Touch idea or plan files only if they were acted upon directly, not silently handled by coincidence (CONFIG § File handling).

**Cover currency.** Session close leaves the cover current: rewrite the state-of-play block in place on the folder-note cover (CONFIG § Numbering; a living document is maintained where it lives and stays inside the currency threshold, CONFIG § Status). Give the block the shape its cover's format note defines — `Format-Project` for a project cover, `Format-Area` for an area cover; where that note carries no state-block skeleton, use the detector-parsable heading `## State of play — <YYYY-MM-DD>`. Update the block already on the cover, leaving exactly one dated block. A session that changed no state the cover records names that reason in the log's **State of play** row instead.

## 2 — Atomic reference notes

A reference note follows the **evergreen / atomic-note standard** (Matuschak; Zettelkasten atomicity): one self-contained, context-free, reusable piece of confirmed knowledge, naming **no project specifics** — how an API actually behaves versus its docs, an undocumented default, a silent-failure mode, an interaction effect, a performance characteristic confirmed by test.

What earns a note is a struggle that resolved: when effort spent fixing X turns up the answer Y, capture Y so the next agent reaches it without the struggle. Write it as how the thing works — the encyclopedia entry, not the problem that prompted it. The title and first sentence name a behavior or a working approach, never an error or an event.

### What makes a note atomic

Apply the six atomicity tests in order and act on the first that fails — the tests and their two failure modes live in CONFIG § Atomicity tests.

### Form

One insight per file, in the container — never loose in `<inbox>` — `type: reference`, `Title-Case-Hyphens.md` (CONFIG § Types), `parent: [[]]` left blank. Beyond that the note takes **whatever shape the idea needs** — no required skeleton. A clean atom is often a specific title and two to six sentences; some want a short paragraph on how the fact was confirmed (setup, tool version, the exact test); some a code fence or small table. Use a heading only when the note genuinely has more than one part. The non-negotiables: **self-contained**, **properly linked** (related reusable facts referenced by `[[wikilink]]`, never restated), and a title naming the one idea precisely.

Write only what generalizes: if nothing this session carries past the project, write **zero** atomic notes. Paste any snippet code into chat as well as into its file.

## 3 — Standards the session earned (optional)

A session sometimes expresses, corrects, or emphasizes a standard that holds across projects. Each such standard is itself atomic (§2 tests apply) and gets written as **its own standalone note** in the container — never buried as a footnote on, or merged into, another note.

Classify the standard by its axis before writing it: a correction about appearance is **format**, about behavior or structure is **design**, about phrasing or tone is **voice**. One standard, one axis. The note is `type: <axis>`, `Title-Case-Hyphens.md`, `parent:` the matching index (a project's, or the general area). State the rule, give the corrected-versus-wrong form, and name what triggered it.

Before minting a new note, search the existing standards for one that already holds this principle. A genuinely new standard gets its own note. A standard the vault already records, re-derived this session, files instead as a `type: addendum` targeting the existing standard note (`target: "[[<existing-standard>]]"`): the addendum either sharpens the rule's wording or records the recurrence to raise the standard's emphasis, accruing onto the one canonical note rather than a divergent twin.

### Weight

Record a re-derived standard as the `type: addendum` above and leave `weight` to the merge that raises it (CONFIG § Weighted types).

A project-state change instead — a new blocker, a resolved issue, a decision — is a `type: addendum` targeting the note that holds that state.

**Addendum target must be real.** A `type: addendum` targets a note this session referenced or touched — the project cover (the folder-note, CONFIG § Numbering) or its canonical plan — confirmed on disk before the `target:` field is stamped. A project-state update with no such home is written as a standalone instead. A session that surfaced no standard writes none.

When the resume point needs more than the log's State-of-play line, invoke the note-kit-plan skill to write or update the session's canonical plan note rather than hand-rolling one (look in `<inbox>` or the project/parent root), passing it the session context and the current State-of-play. An existing plan is updated in place; none → a draft in `<inbox>`.

## 4 — Completeness sweep

Before posting the summary, confirm you did not skip an output the session earned. The recurring failure is letting a category default to "none" by omission, most often a standard, because a rule that surfaced is easy to file as "already known." Walk every row and record `earned: <what>` or `none: <reason>` — reaching `none` is fine, but only after the check, never by skipping the row.

| output                      | write one when the session                                                                                                                                 | the tell                                                                                        |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| session log                 | always                                                                                                                                                     | mandatory                                                                                       |
| project cover (§1)          | changed state the project cover records — a blocker, a decision, a new resume point                                                                        | the cover's newest dated block predates this session                                            |
| atomic reference note (§2)  | confirmed a reusable, context-free fact a struggle resolved                                                                                                | a Successes/Failures finding that stays true with the project deleted                           |
| voice note (§3)             | stated or corrected how prose should read, beyond this project                                                                                             | someone pushed back on phrasing, tone, or lingo                                                 |
| design note (§3)            | stated or corrected how something works or is organized, beyond this project: methodology, architecture, process, naming, ordering, prefixes               | a correction about behavior or structure, not appearance or wording                             |
| format note (§3)            | stated or corrected how something looks, beyond this project: color, typography, chart or diagram visual character, styling; appearance is the format axis | a correction about visual character, not structure, behavior, or wording                        |
| project-state addendum (§3) | hit a new blocker, resolved an issue, or made a decision with an existing home note                                                                        | the project cover or its canonical plan should record it                                        |
| plan                        | created a plan, or an existing plan was meaningfully adjusted, completed, or deferred                                                                      | session started from a plan, or a plan was specifically accessed and checked during the session |
| originating idea (§1)       | grew from a captured `idea` note                                                                                                                           | the idea still reads as a loose spark, but this session acted on it                             |
| `<user-queue>` proposal     | left an answerable **judgment call** unresolved in chat — one item per decision, each independently evidenced                                              | a genuine choice would evaporate with the session — write it in proposal shape to the `<user-queue>` (CONFIG § Queue protocol). A live-session **gate checklist** ("re-flash the device", "run the live test") is not a queue item — it routes to the plan and the log's State of play |

**Machine-gates route to the plan, not the queue.** A user-presence gate — a step needing the user's hardware, GPU, webcam, or eyes ("re-flash the device", "run the live test", "judge the output by eye") — is a checklist item, not an answerable decision. Record every such gate on the session's canonical plan (invoke note-kit-plan, §3) and in the log's **State of play**, where the session brief surfaces them on resume. The `<user-queue>` receives a genuine judgment call the action-agent cannot resolve on its own, one item per decision. A list of gates in the queue degrades it (CONFIG § Queue protocol; § Holds — user-presence).

**Check each queue row before writing it.** A `<user-queue>` row passes three checks at emit time: every option on the item resolves the same judgment; the item holds a decision rather than a set of independently executable steps; and every option executes from its own line, with no destination, name, scope, or count parked in a nested bullet beneath it. A second judgment gets its own item, independently evidenced; a set of steps routes to the session's canonical plan; nested detail folds up into the option or the context line (CONFIG § Queue protocol, `Format-User-Queue`).

**A correction you received is the strongest signal of a standard.** When the user overrides how you did something and the override is not specific to one file, you have found a voice or design standard; file it per §3 (new note, or addendum onto the existing standard). An existing standard or rule does not cancel the note.

**A verdict sweeps forward.** A session that falsifies a mechanism lists every document known to assert it, edits the ones whose correction is mechanical — each carrying a forward pointer to the verdict — and records the remainder in the log's **State of play** for the analyst's sweep. Retiring the mechanism's vocabulary runs in the same pass (CONFIG § Deprecation).

**Voice pre-correction (pre-filing).** Re-read each atomic reference for imperative "do this" phrasing where descriptive belongs — a reference states how something IS. Rewrite before filing; this is the most common voice error.

**Shape each output to its type.** Before writing any typed note, fetch its Format note via `mcp__vault__vault_search("Format-<Type>")` — one per output this run writes: `Format-Session`, `Format-Reference`, `Format-Voice`, `Format-Design`, `Format-Format`, `Format-Addendum`, and `Format-Project` or `Format-Area` for the cover (CONFIG § Format notes). The Format note carries the canonical frontmatter and body skeleton. Two invariants: never edit a format note mid-run; a missing format note → fall back to CONFIG § Types and flag the gap, never invent a shape. Every `<inbox>` draft carries `reviewed: false` and `status: draft`. Refer to any folder by its wildcard token, not its literal name.

## 5 — Chat summary

After writing, post a short summary: the container path, the session-log wikilink (the gate file when the container holds tag-alongs), each atomic and voice/design note as a wikilink with a one-line description, any project-state addendum and its target (or none), and any open blocker for next session. When the container holds tag-alongs, remind the user that approving the gate file auto-reviews the whole container; a lone session log needs no such reminder.
