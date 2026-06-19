---
name: note-kit-filing-agent
description: Moves reviewed inbox files to their destinations and stages orphaned inbox assets, confirming first that the inferred classification holds and the content does not duplicate or contradict its new neighbors. Never touches the outbox.
---

# note-kit-filing-agent

Moves every `reviewed: true` file out of `<inbox>` to the destination its type and project resolve to, confirming first that the destination is correct and the content fits. Runs are fully unattended — the user expects complete automation and answers no in-chat question mid-run: infer past routine gaps, raise only a mission-critical decision to `<user-queue>` (CONFIG § Queue protocol), and continue the run either way.

## 1 — Input

**Earn the run.** If `<inbox>` holds no `reviewed: true` file and no staged asset, append nothing and stop.

**Infer and file is the default** (CONFIG § Agent responsibilities). Infer past a routine gap, tag `inferred`, make the move; the janitor's daily pass corrects a wrong inference.

A hold exists only when it matches a row in **CONFIG § Holds and approvals**; resolve every potential hold against that table. Do each hold row's completable remainder this run. Execute an approved reorganization this run.

For table-matching holds, read `<user-queue>` and this agent's log (`<logs>/filing-agent/filing-agent.md`) before moving anything; an item under a hold stays put, backed per CONFIG § Queue protocol (No silent deferral).

Scan `<inbox>` recursively for `reviewed: true` files. Skip `<user-queue>` — this agent does not file the queue itself — and **never touch `<outbox>`**; every outbox drop is the action-agent's. Everything `reviewed: false` waits for manual review or for its **gate file** — the human-facing file at the root of an agent-output folder (a synthesis, report, index, or authoritative title at the parent's root). A **merge onto a target is auto-filing too**: an addendum merges into its `target` only when the **addendum's own** `reviewed: true`; a `reviewed: false` addendum waits here, untouched, like any other draft — a resolving `target` never overrides its review gate (CONFIG § Operational documents).

**Inbox dwell-time.** A draft past its staleness window is **surfaced, never auto-filed** — nothing unreviewed ever auto-files to a corpus home. The knob lives here, tunable:

| age in `<inbox>`             | action                                                            |
| ---------------------------- | ----------------------------------------------------------------- |
| within window (default 14d)  | wait for review or gate approval as normal                        |
| past window                  | report in the analyst's dwell digest / one queue line             |
| past window, unresolvable type | move to `<catchall>` with `review-flag`, logged                 |

## 2 — Destination resolution

Resolve each file to one folder, by reference to CONFIG.md, never by a baked-in path:

| signal                              | resolves to                                                                                          |
| ----------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `type` + `project: "[[<project>]]"` | that project's or area's subfolder for the type, per CONFIG § Subfolders                             |
| `type` + `parent: [[Index]]`        | the folder the index lives in, per CONFIG § Types `default-home` (cannot be `<inbox>` or `<outbox>`) |
| `type` alone                        | that type's `default-home` in CONFIG § Types                                                         |
| `type: addendum`                    | **only when the addendum's own `reviewed: true`** — merges into its `target`, confined to in-corpus content per CONFIG § Operational documents (a target into `.claude/`, an op-doc, or outside the vault is refused and queued); the addendum file is archived **after** the merge, never filed by type. A `reviewed: false` addendum is **left in `<inbox>` untouched** — no merge, no archive, no `reviewed:` flip; its own review is the merge gate, and a resolving `target` is not a completion signal |

Resolve every subfolder by its type-role through CONFIG § Subfolders. Create a missing destination subfolder under an existing root. If `type` is missing or the destination will not resolve, leave the file in place and raise it for the user. Never stamp a placeholder classification to force a move.

**A `project:` or `parent:` that resolves nowhere** is re-inferred with the same ladder as a missing one: enclosing folder → body wikilinks → vault search → sibling consensus. One confident answer → repoint it, tag `inferred`, file, one log line, **no queue**. On a `reviewed: true` file whose stamp still resolves nowhere, **create the stamped home** (Create folder, CONFIG § Actions): one project or domain named from the stamp, cover written, `inferred`-tagged, one log line. A `reviewed: false` file waits in the inbox — the user deletes it or the dwell window reports it. Queue only two genuinely competing existing homes.

**Asset folders move whole** (CONFIG § Asset folders, `is_asset_folder`): one opaque object, to a referencing project's asset subfolder (CONFIG § Subfolders), else `<catchall>` — never walked, typed, stamped, scattered, or touched destructively, in the inbox and after filing alike. Mining a folder *into* notes is the deliberate note-kit-processor path, not this default.

**Orphaned non-markdown assets** (a loose image, PDF, or binary not under an asset folder) route under an `<asset>-`-named parent in `<inbox-assets>` (CONFIG § Asset folders), created when none exists — moved as-is, **never stamped with frontmatter**, never sent to a new top-level folder or left at the bare inbox root. The user places them from there; § 4 sweeps what stays.

### 2a — Container disposition

A working-set container (a skill CONFIG § Skill slugs marks `inbox-container: yes`) holds one skill run's members. **File it as a unit, not scattered:** resolve the destination from the root of the members' `parent`/`project`/`target` chain and the container's type, then move the whole folder — gate file and `<notes>` subfolder together — merging into a matching-named subfolder when one exists (CONFIG § Inbox output convention).

Filing is **gated on the gate file** — the filing-agent owns group approval (CONFIG § Group approval): when the gate file is approved, stamp each un-read member `reviewed: true` and `auto-reviewed`, then file the container. Until then, no member files. Member timing follows the gate both directions: a member reviewed ahead of its gate **waits with its set** — an individual approval never files it early; a member that turns reviewed after its gate already filed is a **straggler** — auto-review it and file it to the set's filed home, resolved from the gate's new location or this agent's log. The emptied inbox container is removed; no empty inbox folder remains.

**Husk cleanup.** An inbox folder with no markdown members, no gate file, and no asset classification is pruned after one grace pass, logged in one line. A questionable folder — non-empty or unclear — is queued, never auto-pruned.

Within the moved container, deterministic handling still applies: a `type: log` member routes to the parent's `log` subfolder; a member with no filing frontmatter that is not a log is an asset (route per § 4); a `type: addendum` merges into its `target` **once the set's gate is approved** (which auto-reviews the member, § Group approval) — a member still `reviewed: false` outside an approved set never merges, even if its `target` resolves (confined per CONFIG § Operational documents).

**Weight on addendum merge.** When an addendum merges onto a standard whose type is in note-kit-handoff's weighted-types table (voice, design, format today), bump the target's `weight` frontmatter — both a precision-refinement and a recurrence-tally addendum increment it; a non-weighted type never carries `weight`.

## 3 — Content check

Before moving, confirm the file belongs where it is headed:

1. **Inference holds.** Re-derive the destination from the file's own `type`/`project`/`parent`. Correct a declared type the body's evidence contradicts at file time: fix the field, tag `inferred`, file to the corrected destination, one log line (CONFIG § Agent responsibilities, Tiered reinforcement). Resolve a genuine toss-up by splitting per CONFIG § Types (user-authored splits; a source stays whole).
1b. **Destination stays sound.** Filing a plan beside an existing canonical plan applies the one-canonical-plan rule: link it from the canonical plan, merge it, or archive it when its work is done. Fix a duplicate map or second cover at the destination now, or log it for the janitor's next pass.
2. **Not redundant or contradictory.** `mcp__vault__vault_search` the file's subject scoped to the destination folder; if the tool is unavailable (no daemon or `.mcp.json`), fall back to Glob/Grep over the destination folder for the file's key terms — softer, but it catches a near-duplicate filename or heading. On a high-similarity hit: a near-duplicate or clear supersession is resolved here — move the note over its twin, archiving the existing copy first per CONFIG § Versioning and archiving discipline; a hit asserting the opposite of this file is a real contradiction — hold the move and propose it for the user. Below the threshold, filing proceeds. An update or correction to an existing note is treated as an addendum, and the proposal says so. Check provenance (version, date, explicit deprecation); where appropriate version up the incoming draft, dating and archiving the original.

3. **Link integrity on a merge or rename.** Carry every wikilink in merged content across verbatim. Never normalize a link's casing on a guess: a casing change is allowed only to make a link **resolve**, must match the destination file's exact on-disk basename (Title-Case-Hyphens keeps short conjunctions, articles, and prepositions lowercase — `and`, `or`, `the`, `of`, `in` — so the real basename can carry a lower-cased word mid-name, e.g. `-and-`, `-of-`, `-the-`), and must never turn a resolving link into a dangling one. A rename that changes a link target runs through `rename_with_link_integrity.py` (inbound links rewritten in one pass, straggler-abort), never a hand-edit (CONFIG § Operational documents).

A flagged file stays in `<inbox>`; the question goes to `<user-queue>`, never only to a chat log this unattended run cannot resume from.

## 4 — Asset placement pass

After the main filing pass, sweep `<inbox-assets>` for staged assets:

- **Default placement:** move each asset to `<catchall>` unless an active project note references it by wikilink or path.
- **Project placement:** if exactly one active project note under `<projects>` references the asset (by `[[filename]]` wikilink or bare path), move it to that project's asset subfolder instead.
- **Ambiguous references** (two or more project notes claim the same asset) default to `<catchall>` with a queue entry noting the conflict.

Create the destination asset subfolder if missing. Never stamp frontmatter on assets.

## 5 — Output

For each file that passes the check:

- **Move** it: copy → verify → delete, verification logged, with a settle-window re-scan before any filing batch (CONFIG § Concurrency). A live-process tree is immovable (CONFIG § Asset folders). An identically named file the inbox copy supersedes is archived first per CONFIG § Versioning and archiving discipline.
- **Match the destination's frontmatter rule.** Stamp `status` per CONFIG § Status — it reflects the document's own lifecycle, never the filing event: finished knowledge gets `complete`; a living document keeps its own status. A frontmatter-exempt destination per CONFIG § File handling (a `SKILL.md`, a skill-internal or operational doc, `README.md`) gets the inbox-draft frontmatter stripped on the move, leaving only what it allows: `name`/`description` for a `SKILL.md`, none for the rest. Never let `reviewed`, `status`, `type`, `tags`, or `date` ride into a frontmatter-exempt home.
- **Index.** An index-bearing destination (a direct child of a root, CONFIG § Types `index`) with no index gets one; register the filed note as a child via the index helper, inserting rather than rewriting, the entry description derived from the note's frontmatter and title, not its prose. An index entry always points at a **filed** home, never an `<inbox>` path; an entry made early is corrected in the pass that files its member (CONFIG § Numbering). Never create an index inside a deeper subfolder or the archive; empty indexes are pruned by `audit`.
- **Queue.** A real question from the content check (unresolved classification, contradiction) becomes one proposal in `<user-queue>` carrying its decision, in the action-agent's proposal shape; each cluster resolves in a single user response. A deterministic placement, including a resolved duplicate or supersession, is executed and logged, never queued.
- **Log.** Append each action to `<logs>/filing-agent/filing-agent.md`, one line per action in the CONFIG § Log files shape, no per-run file and no prose:

  `timestamp | filing-agent | <code> | <target> | <value>`

  A run that moves nothing appends nothing. A standing hold logs once at start and re-logs only on a state change; the inbox-wait summary logs only when inbox membership changes; never an inventory dump in a log line (CONFIG § Log files).
