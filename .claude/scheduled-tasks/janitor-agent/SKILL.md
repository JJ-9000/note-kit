---
name: note-kit-janitor-agent
description: Resolves the frontmatter inference the audit script deferred (type, parent, project, tags), completes it in place, flags a stale reviewed note, and queues only the calls that are the user's to make.
---

# note-kit-janitor-agent

Runs are fully unattended — the user expects complete automation and answers no in-chat question mid-run: raise any issue to `<user-queue>` as a decision or deliberately drop it (CONFIG § Queue protocol), and continue the run either way.

## 0 — Run the audit

```
JANITOR_VAULT_ROOT=<vault-root> python <kit-root>/scheduled-tasks/janitor-agent/audit.py --apply
```

`JANITOR_VAULT_ROOT` is the vault root. Writes are gated: a bare run is detect-only; `--apply` (or `JANITOR_APPLY=1`) applies the deterministic fixes — including pruning an empty subfolder and an empty index — and refreshes the state snapshot this run consumes. A trustworthy deterministic fix auto-applies silently with one ledger line and **no janitor row**; the janitor receives only genuine inference and judgment calls. If the script exits non-zero or prints a traceback, stop and report the error.

## 1 — Input

Read `<logs>/Vault-State-Index.md`, the state snapshot `build_state_index` overwrites each run. Its counted rows are the work list; read the event ledger `<logs>/janitor-agent/janitor-agent.md` only for a prior decision on a file in front of you.

**Earn the run.** If the snapshot's counted rows are all zero, append nothing and stop.

The snapshot carries fixed-field rows (CONFIG § Log files). Act by `code`:

- `parent-finding`, `parent-orphan`, `type-resolution`: a deferred call on type, parent, or project. The primary work list; resolve per § 2.
- `tag-resolution`: a tag `normalize_tag` could not map to one canonical candidate; route to the queue as a rename, add-alias, add-to-vocabulary, or remove choice.
- `idea-archive`: an in-progress idea whose originating session completed; resolve per § 2.
- `reviewed-stale`: a `reviewed: true` note whose linked plan or spec changed after its review, or was explicitly overridden or deprecated; flag per § 2.
- `index-drift`, `index-missing`: an index whose child-links `audit` found stale or incomplete, or an index-bearing folder with none. Correct the links by hand, or create the index in place (CONFIG § Types `index`); never create one inside a deeper subfolder or the archive.
- `body-wikilink-resolution`, a `*-would-*`, `*-collision`, or `*-failed` code: something the script could not finish; resolve it or route it to the queue.

Honor `compliance_exceptions: [<slug>]` on a file; skip the listed checks. Read no other sources. Quickly check `audit.py`'s work to flag serious issues.

## 2 — Resolve

**Asset hands-off check first.** Before any inference or stamp, run the asset check (CONFIG § Asset folders, `is_asset_folder`): a file inside an asset folder is never typed, parented, or normalized — skip the row.

The janitor infers — scripts act on the deterministic, agents infer the ambiguous, only a judgment call reaches `<user-queue>`. Deterministic placement, naming, tag normalization, relocation, and asset staging already ran in `audit.py`; never redo them here. **No re-litigation:** a value already tagged `inferred` is not re-inferred or re-questioned absent contradicting evidence (CONFIG § Tags). Before re-deriving an unwritten convention by counting (e.g. "standards carry no parent"), consult the ledger's `convention` lines; a newly confirmed convention is logged once with code `convention` (CONFIG § Log files).

**Queue or fix in place.** Fix in place, tagged `inferred`, when one rung of the ladder yields a single confident answer. Route a row to the queue in two cases: the choice would **change how the user thinks of the file** (its type, project, or home), or **two or more answers stay plausible** after the ladder runs. When unsure whether a fix is cosmetic or identity-changing, treat it as cosmetic and fix it.

For each deferred-call row, walk the ladder for its field and take the first rung that yields one confident answer; apply the value and add the `inferred` tag. A rung holding two or more meaningfully different answers stops the ladder and sends the row to the queue.

| field | rung 1 | rung 2 | rung 3 | default |
|---|---|---|---|---|
| type | folder maps to one type-default | filename or structure signal (date-prefixed in a session subfolder → session; link-dense body → index; ≥3 code blocks → snippet) | body peers: dominant match against files in a candidate type's folder | `note` |
| parent | enclosing project, area, or domain folder's index | dominant index among the body's wikilinks | `vault_search` the file's distinctive terms for the owning parent index, then read sibling files in the candidate folder for their dominant `parent`, narrowing by type, tags, and enclosing folder | type needs `parent`, two or more plausible indexes survive rung 3 → queue; `note` → leave unstamped, the `inferred` tag exempts it from orphan detection |
| project | `<projects>/<project>/…` → `[[<project>]]` | dominant project among the body's wikilinks | `vault_search` the file's distinctive terms for the owning project, then read sibling files in the candidate folder for their dominant `project`, narrowing by type, tags, and enclosing folder | session or research, two or more plausible projects survive rung 3 → queue |
| date | the archived pre-migration copy's date | the originating session log's date | first appearance in `<history>` or the import records | the import date, tagged `inferred` |

(`audit.py` resolves most dates deterministically; the ladder covers the remainder.) No field stays permanently unresolvable and re-reported — every row ends in a value or a queue item.

Each rung is a structured lookup against the canonical tables and the existing vault, never a free guess. Three guards: parent inference **rejects self-reference** — a root or cover note's parent is its enclosing area or domain index, or none, never itself; an inferred value is written **only to frontmatter**, never as a body line; the sibling-consensus rungs count only values not themselves tagged `inferred` (CONFIG § Tags). A scan error on one file appends to the event ledger and the pass continues. **Edit this table to update the expected behavior based on type.**

**Completed-idea archival.** Resolve an `idea-archive` row by reading, not reflex: open the idea and its linked session and confirm the session carried the idea's substance forward. If it did, archive the idea (CONFIG § Versioning and archiving discipline) and append the decision to the ledger; handoff's labeling was the consent, so no queue proposal. If the session did not address the idea, leave it and log why; a non-match is the analyst's to revisit.

**Stale reviewed note.** Resolve a `reviewed-stale` row by reading the note against the plan or spec it links. If the upstream change actually invalidates the reviewed content, add `review-flag` so the note returns to the user's gate, and append the decision to the ledger. If not, leave it and log why.

**Version confidence.** When a parent folder holds several files of the same type where one should be canonical or active (`v001` vs `v002`, plans of conflicting prominence), check them against each other and recent sessions to enforce the current canon. Move stray versions to the parent folder's `<archive>/` subfolder, creating one if missing.

## 3 — Output

- **In place:** completed frontmatter and `parent:` stamps, each inferred value carrying the `inferred` tag; `review-flag` on concerns. Per CONFIG § Tags a `review-flag` never exists without its matching queue item — when the janitor flags, it writes the queue proposal in the same pass.
- **Event ledger:** append one line per resolution to `<logs>/janitor-agent/janitor-agent.md` (CONFIG § Log files), one line per cluster routed to the queue. A run that resolves nothing appends nothing.
- **Queue:** a proposal only where the ladder stopped on a genuine user choice (CONFIG § Queue protocol). Before appending, scan `<user-queue>` for a pending entry already covering that file or cluster; if present, skip. Suppress any slug listed in the file's `compliance_exceptions`.
