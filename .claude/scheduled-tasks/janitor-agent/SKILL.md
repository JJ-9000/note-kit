---
name: note-kit-janitor-agent
description: Resolves the frontmatter inference the audit script deferred (type, parent, project, tags), owns structural cleanup — wrong declared types, duplicate maps and indexes, plan family shape, the content-lifecycle retirements — completes it in place, flags a stale reviewed note, and queues only the calls that are the user's to make.
---

# note-kit-janitor-agent

Runs are fully unattended — the user expects complete automation and answers no in-chat question mid-run: infer past routine gaps, raise only a mission-critical decision to `<user-queue>` (CONFIG § Queue protocol), and continue the run either way.

## 0 — Run the audit

At run start, rotate the event logs before any other work — `python <kit-root>/scripts/rotate_logs.py --apply` rolls an oversized or aged event-log head into its dated segment (CONFIG § Log files); `--apply` is the flag that performs the rotation, so carry it. Append the line the script prints — the rotation or the skip — to this run's entry in `<logs>/janitor-agent/janitor-agent.md`: the pass counts as rotated when that line stands in the log.

Flag the archive's aged material next, so the segments this pass just closed are in scope — `JANITOR_VAULT_ROOT=<vault-root> python <kit-root>/scripts/age_to_cold_storage.py` runs the flag pass, writing its sweep manifest under `<logs>/manual-operations/` and moving nothing without `--apply`; the analyst judges each move into `<history>` (CONFIG § Helper-script automation; the script's own docs carry the manifest shape). Append the manifest path and the counts the script prints to this run's log entry.

Then run the audit:

```
JANITOR_VAULT_ROOT=<vault-root> python <kit-root>/scheduled-tasks/janitor-agent/audit.py --apply
```

`JANITOR_VAULT_ROOT` is the vault root. Writes are gated: a bare run is detect-only; `--apply` (or `JANITOR_APPLY=1`) applies the deterministic fixes — including pruning an empty subfolder and an empty index — and refreshes the state snapshot this run consumes. A supervised pass may withhold a contested fix class and land the rest with `--skip <class>` (or land one class alone with `--only <class>`); a withheld class logs its would-fixes like a detect-only run. An unattended pass runs un-filtered. A trustworthy deterministic fix auto-applies silently with one log line and **no janitor row**; the janitor receives only genuine inference and judgment calls. If the script exits non-zero or prints a traceback, stop and report the error.

**The run lease.** `--apply` runs under the vault-wide run lease `<logs>/run-lease.md` (CONFIG § Concurrency rule 1). When the script reports the lease held by another agent, it defers the pass: append the deferral line to the event log, leave the vault untouched, and close at § 3's run close.

## 1 — Input

Read `<logs>/Vault-State-Index.md`, the state snapshot `build_state_index` overwrites each run. Its counted rows are the work list; read the event log `<logs>/janitor-agent/janitor-agent.md` only for a prior decision on a file in front of you.

**Earn the run.** If the snapshot's counted rows are all zero, resolve nothing, append no resolution lines, and go straight to § 3's run close.

The snapshot carries fixed-field rows (CONFIG § Log files). Act by `code`:

- `parent-finding`, `parent-orphan`, `type-resolution`: a deferred call on type, parent, or project. The primary work list; resolve per § 2.
- `tag-resolution`: a tag `normalize_tag` could not map to one canonical candidate; route to the queue as a rename, add-alias, add-to-vocabulary, or remove choice.
- `idea-archive`: an in-progress idea whose originating session completed; resolve per § 2.
- `reviewed-stale`: a `reviewed: true` note whose linked plan or spec changed after its review, or was explicitly overridden or deprecated; flag per § 2.
- `index-drift`, `index-missing`: an index whose child-links `audit` found stale or incomplete, or an index-bearing folder with none. Correct the links by hand, or create the index in place (CONFIG § Types `index`); never create one inside a deeper subfolder or the archive.
- `dangling-link`: a body wikilink whose basename resolves to no file in the vault — the standing reference-cohort metric `build_state_index` drives toward zero. Repair it in place per § 2's repair ladder, reference-typed sources first; this is correctable work the janitor closes, not solely a queue route.
- `body-wikilink-resolution`, a `*-would-*`, `*-collision`, or `*-failed` code: something the script could not finish; resolve it or route it to the queue.

Honor `compliance_exceptions: [<slug>]` on a file; skip the listed checks. Read no other sources.

Check `audit.py`'s pass on three signals:

- **Exit code** — a non-zero exit or a traceback stops the run and reports the error (§ 0).
- **`folder-reverted` lines** — each records a hand rename of a kit folder the script put back (CONFIG § Folders); route a rename that recurs to the queue as a proposal to adopt the new name.
- **Row counts** — the codes the run logged against the counted rows in the state snapshot; a count that disagrees is itself a finding, logged with both numbers.

## 2 — Resolve

**Asset hands-off check first.** Before any inference or stamp, run the asset check (CONFIG § Asset folders, `is_asset_folder`): a file inside an asset folder is never typed, parented, or normalized — skip the row.

The janitor infers — scripts act on the deterministic, agents infer the ambiguous, only a judgment call reaches `<user-queue>`. Deterministic placement, naming, tag normalization, relocation, and asset staging already ran in `audit.py`; never redo them here. **No re-litigation:** a value already tagged `inferred` is not re-inferred or re-questioned absent contradicting evidence (CONFIG § Tags). Before re-deriving an unwritten convention by counting (e.g. "standards carry no parent"), consult the log's `convention` lines; a newly confirmed convention is logged once with code `convention` (CONFIG § Log files).

**Queue or fix in place.** Fix in place, tagged `inferred`, when one rung of the ladder yields a single confident answer. Route a row to the queue in two cases: the choice would **change how the user thinks of the file** (its type, project, or home), or **two or more answers stay plausible** after the ladder runs. When unsure whether a fix is cosmetic or identity-changing, treat it as cosmetic and fix it.

For each deferred-call row, walk the ladder for its field and take the first rung that yields one confident answer; apply the value and add the `inferred` tag. A rung holding two or more meaningfully different answers stops the ladder and sends the row to the queue.

| field | rung 1 | rung 2 | rung 3 | default |
|---|---|---|---|---|
| type | folder maps to one type-default | filename or structure signal (date-prefixed in a session subfolder → session; link-dense body → index; ≥3 code blocks → snippet) | body peers: dominant match against files in a candidate type's folder | `note` |
| parent | enclosing project, area, or domain folder's index | dominant index among the body's wikilinks | `vault_search` the file's distinctive terms for the owning parent index, then read sibling files in the candidate folder for their dominant `parent`, narrowing by type, tags, and enclosing folder | type needs `parent`, two or more plausible indexes survive rung 3 → queue; `note` → leave unstamped, the `inferred` tag exempts it from orphan detection |
| project | `<projects>/<project>/…` → `[[<project>]]` | dominant project among the body's wikilinks | `vault_search` the file's distinctive terms for the owning project, then read sibling files in the candidate folder for their dominant `project`, narrowing by type, tags, and enclosing folder | session or research, two or more plausible projects survive rung 3 → queue |
| date | the archived pre-migration copy's date | the originating session log's date | first appearance in `<history>` or the import records | the import date, tagged `inferred` |

(`audit.py` resolves most dates deterministically; the ladder covers the remainder.) No field stays permanently unresolvable and re-reported — every row ends in a value or a queue item.

Each rung is a structured lookup against the canonical tables and the existing vault, never a free guess. Three guards: parent inference **rejects self-reference** — a root or cover note's parent is its enclosing area or domain index, or none, never itself; an inferred value is written **only to frontmatter**, never as a body line; the sibling-consensus rungs count only values not themselves tagged `inferred` (CONFIG § Tags). A scan error on one file appends to the event log and the pass continues.

**Completed-idea archival.** Resolve an `idea-archive` row by reading, not reflex: open the idea and its linked session and confirm the session carried the idea's substance forward. If it did, archive the idea (CONFIG § Versioning and archiving discipline) and append the decision to the log; handoff's labeling was the consent, so no queue proposal. If the session did not address the idea, leave it and log why; a non-match is the analyst's to revisit.

**Stale reviewed note.** Resolve a `reviewed-stale` row by reading the note against the plan or spec it links. If the upstream change actually invalidates the reviewed content, add `review-flag` so the note returns to the user's gate, and append the decision to the log. If not, leave it and log why.

**Version confidence.** When a parent folder holds several files of the same type where one should be canonical or active (`v001` vs `v002`, plans of conflicting prominence), check them against each other and recent sessions to enforce the current canon. Archive each stray version per CONFIG § Versioning and archiving discipline: the copy lands at `<archive>/<source-path>/<date>-<filename>-<version>` and is confirmed before the source goes.

**Structural cleanup** (CONFIG § Agent responsibilities):

- **Wrong declared type.** Correct a declared type the body's evidence contradicts (a dated work log typed `project`, a checklist typed `reference`): fix in place, tag `inferred`, one log line. No-re-litigation protects tagged values, not contradicted declarations. Resolve a genuine toss-up by splitting per CONFIG § Types (user-authored splits; a source stays whole).
- **Duplicate or foreign maps.** Merge a second cover, a `moc`, or any foreign-type map into the canon index: fold in entries pointing at live content, repair inbound wikilinks, archive the foreign note (CONFIG § Versioning and archiving discipline).
- **Plan family shape.** A scope's plan family is legal when every subordinate plan declares `parent` at the scope's canonical plan and carries a lane label in its mission line (CONFIG § Types, `plan`). Resolve a `duplicate-canonical-plan` finding by reading those two declarations, never the count of files: a subordinate carrying both stays as filed, and the finding closes with one log line. Stamp a missing declaration in place where the canonical plan and the subordinate's body agree on its scope and lane; route the row to the queue where two readings stay plausible. Archive a finished plan whose outcome the canonical plan or a filed session records, and link a still-active subordinate from the canonical plan with one line on what it covers. This edit is plan maintenance — `reviewed` is untouched.
- **Cover shape.** A project or area cover (the folder-note named after its root) states current state plus grouped member links (Format-Project / Format-Area), not an accreting changelog. When a cover drifts off-shape — a run of dated `**Update (…)**` entries the session logs already hold, a length far past the format's target — flag it to the queue: propose moving the dated entries into the scope's canonical plan changelog and rewriting the cover to the current-state-plus-grouped-links shape. Reshaping a cover is identity-adjacent structural work, so it routes to the queue, never an in-place rewrite; scan `<user-queue>` for a pending entry on that cover first.
- **Dangling links — the reference cohort ships clean.** Resolve each `dangling-link` row by reading the line and taking the repair that leaves clean, well-formed prose; only a genuinely ambiguous or identity-changing case reaches the queue. Reference and other established-knowledge notes are the priority cohort — **reference before note before session** — and ship with no unresolved link.
  - **repoint** — a renamed, relocated, or atomized target exists (evidence: a rename line in the event log, a single case-variant / alias / Levenshtein-≤1 file, or one high-confidence `vault_search` hit) → link to it.
  - **demote** — the link text reads as meaningful prose → drop the brackets, keep the words.
  - **remove link** — an inline reference whose text is irrelevant once dead → remove it and mend the sentence.
  - **remove citation** — a dead pointer carrying nothing live (a standalone list item, or a chapter pointer beside a live title or URL) → remove only the dead element, leaving any live citation intact.
  - **queue** — the repair would change how the user thinks of the note, or two options stay plausible.
  All four repairs apply unattended under the janitor's judgment (CONFIG § Versioning and archiving discipline: archive-first). The edited note keeps no inline trace of the change (artifacts stand alone, `reviewed` untouched), and each repair is one event-ledger line naming the note, the ghost, and the option taken.
- **Memory references read as plain text.** A body wikilink whose basename is an auto-memory slug from the agent's `~/.claude` `MEMORY.md` is the **demote** case of the ladder above.
  - De-link it to plain text in a living document: `[[some-memory-slug]]` → `some-memory-slug`.
  - The class surfaces as `dangling-link` (or the legacy `body-wikilink-resolution`), since a memory file resolves to no vault path.
  - This is the standing resolution for the class: archive-first, one log line, no queue item.
  - `build_state_index` suppresses a lowercase-kebab memory slug cited inside an immutable session log (CONFIG § Helper-script automation) — sessions only, every other type in scope — so a row outside a session is a genuine miss: a memory slug to de-link, or a broken link to walk through the ladder.

**Content lifecycle** (CONFIG § Content lifecycle — the four rows this agent owns). Work them on the daily per-file pass, taking the state-index findings and `audit.py`'s `retired-token` hits as the work list. Each retirement archives per CONFIG § Versioning and archiving discipline and appends one log line naming the class, the file, and the trigger that fired.

- **Superseded plan.** Retire a plan on its supersession trigger per CONFIG § Content lifecycle: stamp `status: superseded`, archive it to the destination that row names, and keep one forward link in the successor. The ratification runs as one logged pass (CONFIG § Versioning and archiving discipline).
- **Completed plan element.** Migrate a long-checked `[x]` item on an `active` plan into that plan's `<Plan>-Changelog` note per CONFIG § Content lifecycle, created on the first migration in the shape `Format-Plan` carries; the changelog follows its plan's disposition. The plan keeps its open items, and this maintenance leaves `reviewed` untouched.
- **Transient comms.** Archive a message on either trigger per CONFIG § Content lifecycle — its carrying mechanism gaining a `## Retired tokens` row, or its age past the consuming run's log entry. The `retired-token` hits are the work list for the first trigger.
- **Duplicate asset.** Consume a `duplicate-asset` finding that has persisted per CONFIG § Content lifecycle: the canonical copy stays at its filed path and each redundant copy archives. A redundant copy past that row's size threshold goes to `<user-queue>` for delete-on-approval, carrying its hash and the surviving twin's path. A `diverged-asset` finding routes the canon choice to the queue instead.

## 3 — Output

- **In place:** completed frontmatter and `parent:` stamps, each inferred value carrying the `inferred` tag; `review-flag` on concerns. Per CONFIG § Tags a `review-flag` never exists without its matching queue item — when the janitor flags, it writes the queue proposal in the same pass.
- **Event log:** append one line per resolution to `<logs>/janitor-agent/janitor-agent.md` (CONFIG § Log files), one line per cluster routed to the queue. A run that resolves nothing appends no resolution lines; the run-boundary lines — rotation, retention flag, and the release below — record the pass itself and stand on every run.
- **Queue:** a proposal only where the ladder stopped on a genuine user choice (CONFIG § Queue protocol). Before appending, scan `<user-queue>` for a pending entry already covering that file or cluster; if present, skip. Suppress any slug listed in the file's `compliance_exceptions`.
- **Run close:** release the run lease and log it — the final step of every pass, run on each exit path: the pass that resolved rows, the deferred pass, the earn-the-run stop at § 1, and the pass that stopped on an error. Read `<logs>/run-lease.md`; where it still carries a line this agent took, clear the file so the next mutating agent runs on its own cadence (CONFIG § Concurrency rule 1). Append one `lease-released` line to `<logs>/janitor-agent/janitor-agent.md` naming the lease path and the state found: released, already clear, or held by another agent.
