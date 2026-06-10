---
name: note-kit-handoff
description: End-of-session deliverables — a session log plus any atomic reference notes and standards addendums the session earned. Invoke with /note-kit-handoff to wrap a working session. Trigger on "handoff", "wrap up", "end session", "session log", "write the handoff".
---

# handoff

Produce the session-end deliverables below. The session log is mandatory; everything else is written only when the session actually earned it. Write nothing the session did not produce.

**Link what already exists; do not rewrite it.** When the session already committed a note, standard, or structural change directly to the vault, the log points to it by `[[wikilink]]` — never duplicate a file already on disk or reiterate its content. A session that did its writing live earns a thorough log and a plan, and few or no new notes — the correct outcome, not a thin one.

**A note earns its place by being useful** — admitted only because a future reader would retrieve it. An atom no one would search for, a standard that restates a known rule, a plan that repeats the State-of-play line: none of these is owed.

## Modes

| invocation               | produces                                                                                                           |
| ------------------------ | ------------------------------------------------------------------------------------------------------------------ |
| `/note-kit-handoff`      | the full set: session log + atomic notes + any standalone voice/design notes, a project-state addendum, and a plan |
| `/note-kit-handoff lite` | the session log only — for a routine continuation where nothing reusable surfaced                                  |

## Output container

Everything a handoff produces lands in one container `<inbox>/<date>-<topic>-session/` (CONFIG § Skill slugs). The session log is the **gate file** at the container root; approving the gate auto-reviews the set and the filing-agent files it as a unit (CONFIG § Group approval).

## 1 — Session log

Write to the container root: `<inbox>/<date>-<topic>-session/<YYYY-MM-DD>-<slug>.md`, `type: session`, `project: "[[Name]]"` (naming per CONFIG § Types); the slug names the session topic. As the gate file, the log opens with the decision header — at most five lines: what this set adds or changes, what deserves a real read, and what approval triggers (CONFIG § Group approval). Stamp `project:` only after confirming the target exists — one vault search or project listing check; no match → leave the field empty for downstream inference, never invent a name.

Target length is ~400 words, per the session format note (CONFIG § Format notes): Successes and Failures stay concrete; Progress trims to decisions.

| section        | holds                                                                                                                              |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------- |
| Successes      | approaches that worked, named concretely enough to repeat — node paths, functions, the working technique                           |
| Failures       | every wrong assumption, misleading error, and failed attempt: what was tried, what happened, the fix, why the assumption was wrong |
| Progress       | what moved — scope as stated at session start and whether it changed, decisions made and the reason behind each                    |
| Relevant files | files, file paths, versions touched this session, as wikilinks where they are vault notes                                          |
| State of play  | the exact resume point — last working node, current blocker, what the next session opens with                                      |

Write for a reader with zero context. Name the function, the parameter, the path, the error string. "Naming was confusing" is useless; "the counter resets to 0 when the accessor is called before the initializer runs" is useful — name the actual symbols in your own case. If nothing went wrong, the Failures section says so — do not invent one.

**Originating idea.** If this session grew from an `idea` note or `plan`, stamp that file `status: in-progress` and `session: "[[<this session log>]]"`, and link it from Relevant files. Only handoff knows which idea or plan the session acted on, so only handoff can set this. Touch idea or plan files only if they were acted upon directly, not silently handled by coincidence (CONFIG § File handling).

## 2 — Atomic reference notes

A reference note follows the **evergreen / atomic-note standard** (Matuschak; Zettelkasten atomicity): one self-contained, context-free, reusable piece of confirmed knowledge, naming **no project specifics** — how an API actually behaves versus its docs, an undocumented default, a silent-failure mode, an interaction effect, a performance characteristic confirmed by test.

What earns a note is a struggle that resolved: when effort spent fixing X turns up the answer Y, capture Y so the next agent reaches it without the struggle. Write it as how the thing works — the encyclopedia entry, not the problem that prompted it. The title and first sentence name a behavior or a working approach, never an error or an event.

### What makes a note atomic

Apply these six tests in order and act on the first that fails:

1. **One concept (the "and" test).** A claim needing an "and", comma splice, or "; additionally" to be accurate is two claims — split.
2. **Independent reusability (the borrow test).** Two facts different future notes would borrow are separate atoms; a fact only useful beside another merges in.
3. **Context-independence (the survival test).** Delete the session and project in your mind; if the note no longer asserts something true and usable, fold it back into the log.
4. **Link, don't embed (the single-source test).** A reusable sibling fact is `[[wikilink]]`ed, never restated; a shared fact lives in one note.
5. **Evergreen, not event (the tense test).** An atom states how something *is* ("the accessor returns null before init"), not what happened; lift the generalization or leave it in the log.
6. **Done (the subtraction test).** Finished when removing any sentence loses a reusable fact and no second independent claim remains to split off.

The only two failure modes: too **big** (compound — caught by 1, 2, 6) splits; too **small or stale** (caught by 3, 5) folds back. The note-kit-processor skill applies the same six tests.

### Form

One insight per file, in the container — never loose in `<inbox>` — `type: reference`, `Title-Case-Hyphens.md` (CONFIG § Types), `parent: [[]]` left blank. Beyond that the note takes **whatever shape the idea needs** — no required skeleton. A clean atom is often a specific title and two to six sentences; some want a short paragraph on how the fact was confirmed (setup, tool version, the exact test); some a code fence or small table. Use a heading only when the note genuinely has more than one part. The non-negotiables: **self-contained**, **properly linked** (related reusable facts referenced by `[[wikilink]]`, never restated), and a title naming the one idea precisely.

Do not pad. If nothing this session generalizes past the project, write **zero** atomic notes. Paste any snippet code into chat as well as into its file.

## 3 — Standards the session earned (optional)

A session sometimes expresses, corrects, or emphasizes a standard that holds across projects. Each such standard is itself atomic (§2 tests apply) and gets written as **its own standalone note** in the container — never buried as a footnote on, or merged into, another note.

Classify the standard by its axis before writing it: a correction about appearance is **format**, about behavior or structure is **design**, about phrasing or tone is **voice**. One standard, one axis. The note is `type: <axis>`, `Title-Case-Hyphens.md`, `parent:` the matching index (a project's, or the general area). State the rule, give the corrected-versus-wrong form, and name what triggered it.

Before minting a new note, search the existing standards for one that already holds this principle. A genuinely new standard gets its own note. A standard the vault already records, re-derived this session, files instead as a `type: addendum` targeting the existing standard note (`target: "[[<existing-standard>]]"`): the addendum either sharpens the rule's wording or records the recurrence to raise the standard's emphasis, accruing onto the one canonical note rather than a divergent twin.

### Weight — the weighted-types table

Only a **configured weighted type** carries a `weight` frontmatter field; today that is **voice, design, format**. This table is the policy — extensible if other types should ever accrue weight:

| weighted type | carries `weight` |
| ------------- | ---------------- |
| voice         | yes              |
| design        | yes              |
| format        | yes              |

`weight` is a stored, optional integer — how often the kit has re-derived the standard. Handoff writes the recurrence as the `type: addendum` above; the **filing-agent bumps `weight`** on merge (both a precision-refinement and a recurrence-tally increment it). A heavier standard is enforced harder: the review crit and the analyst check the heaviest standards first, and severity also lives in the standard's own language. Never bump `weight` on a non-configured type; CONFIG stays silent on it — this table is the source of truth. `weight` is **experimental**: measured by the analyst, pending a keep-or-cut decision.

A project-state change instead — a new blocker, a resolved issue, a decision — is a `type: addendum` targeting the note that holds that state.

**Addendum target must be real.** A `type: addendum` may target only a note actually referenced or touched this session — the project's `Project.md`, `Open-Issues.md`, or a plan doc that came up. Never target an invented, unseen, or deleted note. If the project-state update has no such existing home, write it as a standalone instead. If the session surfaced no standard, write none.

When the resume point needs more than the log's State-of-play line, invoke the note-kit-plan skill to write or update the session's canonical plan note rather than hand-rolling one (look in `<inbox>` or the project/parent root), passing it the session context and the current State-of-play. An existing plan is updated in place; none → a draft in `<inbox>`.

## 4 — Completeness sweep

Before posting the summary, confirm you did not skip an output the session earned. The recurring failure is letting a category default to "none" by omission, most often a standard, because a rule that surfaced is easy to file as "already known." Walk every row and record `earned: <what>` or `none: <reason>` — reaching `none` is fine, but only after the check, never by skipping the row.

| output                      | write one when the session                                                                                                                                 | the tell                                                                                        |
| --------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------- |
| session log                 | always                                                                                                                                                     | mandatory                                                                                       |
| atomic reference note (§2)  | confirmed a reusable, context-free fact a struggle resolved                                                                                                | a Successes/Failures finding that stays true with the project deleted                           |
| voice note (§3)             | stated or corrected how prose should read, beyond this project                                                                                             | someone pushed back on phrasing, tone, or lingo                                                 |
| design note (§3)            | stated or corrected how something works or is organized, beyond this project: methodology, architecture, process, naming, ordering, prefixes               | a correction about behavior or structure, not appearance or wording                             |
| format note (§3)            | stated or corrected how something looks, beyond this project: color, typography, chart or diagram visual character, styling; appearance is the format axis | a correction about visual character, not structure, behavior, or wording                        |
| project-state addendum (§3) | hit a new blocker, resolved an issue, or made a decision with an existing home note                                                                        | a `Project.md`, `Open-Issues`, or plan should record it                                         |
| plan                        | created a plan, or an existing plan was meaningfully adjusted, completed, or deferred                                                                      | session started from a plan, or a plan was specifically accessed and checked during the session |
| originating idea (§1)       | grew from a captured `idea` note                                                                                                                           | the idea still reads as a loose spark, but this session acted on it                             |
| `<user-queue>` proposal     | asked a question in chat that went unanswered                                                                                                              | an open question would evaporate with the session — write it in proposal shape to the `<user-queue>` before the session closes (CONFIG § Queue protocol) |

**A correction you received is the strongest signal of a standard.** When the user overrides how you did something and the override is not specific to one file, you have found a voice or design standard; file it per §3 (new note, or addendum onto the existing standard). An existing standard or rule does not cancel the note.

**Voice pre-correction (pre-filing).** Re-read each atomic reference for imperative "do this" phrasing where descriptive belongs — a reference states how something IS. Rewrite before filing; this is the most common voice error.

**Shape each output to its type.** Before writing any typed note, fetch its Format note via `mcp__vault__vault_search("Format-<Type>")` — `Format-Reference`, `Format-Voice`, `Format-Design`, `Format-Session` (CONFIG § Format notes). The Format note carries the canonical frontmatter and body skeleton. Two invariants: never edit a format note mid-run; a missing format note → fall back to CONFIG § Types and flag the gap, never invent a shape. Every `<inbox>` draft carries `reviewed: false` and `status: draft`. Refer to any folder by its wildcard token, not its literal name.

## 5 — Chat summary

After writing, post a short summary: the container path, the session-log (gate file) wikilink, each atomic and voice/design note as a wikilink with a one-line description, any project-state addendum and its target (or none), and any open blocker for next session. Remind the user that approving the gate file auto-reviews the whole container.
