---
name: note-kit-review
description: Multi-stage document revision pipeline with directive isolation between stages. Activates when the user supplies inline commentary on a document, a separate review note with pointed adjustment directives, or invokes /note-kit-review with a target and feedback. Four stages — Parse, Apply, Audit, Revise — each on a separate file so the directive-handling sub-agents and the fresh-reader auditor never share a working document.
---

# review

User commentary in, revised document out. Parse the commentary into a directive plan, Apply the edits with sub-agents, Audit the compiled draft as a fresh reader, Revise into the final copy. Two editorial standards govern: **directive isolation** — the agents executing edits and the agent auditing the result never share a context; and the **fresh-reader audit** — the compiled draft is read once by an agent that has seen neither the commentary nor the directive list. Each stage reads only the prior stage's output and the relevant user documents, in its own context. A request to compare manual revisions to an earlier version also runs through this pipeline.

The target's canonical `plan` is the primary source of user input for the review. If one does not exist, create one with the note-kit-plan skill, run every stage, and write the plan to the review target's `<inbox>` folder, referencing the user-supplied notes and criticism. If no user-supplied notes exist, ask for them. Each directive and finding is a checkbox in the plan; the run's state is this set of checked boxes. When resuming, add new items to the plan, then take the first open box.

Running inside a sub-agent (no nested spawn): run the four stages serially in this context — identical output, no fan-out (CONFIG § Sub-agent execution). Run each Stage 3 audit lane serially too, emitting the same verdict tables; the serial auditor still reads each working copy as a context it did not produce.

## Definitions

| term                  | meaning                                                                                                                                                                                                                                                                         |
| --------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Directive             | one instruction targeting a specific passage, derived from the commentary, executable on its own. Test: remove every other directive — is this one still fully actionable?                                                                                                      |
| Pattern               | a recurring issue named specifically (`undefined-term`, `passive-imperative`), appearing in two or more directives, applied as a global check. "Style issue" is not a pattern.                                                                                                  |
| Fresh-reader audit    | a fresh reading of the compiled draft by a sub-agent that has not seen the directive list. The cold-reader lane in Stage 3.                                                                                                                                                     |
| Crit pattern-set      | one named checklist a Stage 3 crit lane audits against: the resolute standards on one axis (voice, design, or format — CONFIG § Types), each note one check, or the CONFIG-conformance set for the target's artifact type. The lane emits a verdict for every check in its set. |
| Independent-read gate | a plan box flips only when a non-producer opens the target file and confirms the edit is present. A producing sub-agent's "done" is a claim, not the confirmation.                                                                                                              |
| Open item             | a directive unresolvable within the target document's scope, or needing information the skill cannot supply. Surfaces at the end with why and what would resolve it. A noticed-but-unactioned defect is an open item, never a silent scope cut.                                |

## Review Target

Name the target: which document, and which version. Infer both without asking wherever possible — a prior **review** draft in `<inbox>` is the target and supersedes any live copy; if none exists, the named or live document is the target. Ask in the `<user-queue>` only when the target or its version is genuinely ambiguous.

A review **promotes** the target into a NEW `<inbox>` container before any edit. A `vNNN`-suffixed target continues from that number (`vNNN` → `vNNN+1`); an unversioned target starts at `v001`. Copy the target into the container and edit the copy — never the source. A prior reviewed draft already carries its earlier fixes and accepts the same notes for re-enforcement. Keep in-progress review files in the inbox. Wait for explicit permission to file or apply any changes to a filed/live project or asset.

Write the chosen target and the version method (source version, new container version) into the Review Summary.

## Output

Container folder `<inbox>/<target>-review/`. Only the human-facing gate file — the Review Summary — sits at the container root; every other output (working draft, audits, coverage notes, logs, intermediate drafts, inactive plans) sits in the container's `<notes>/` subfolder (CONFIG § Inbox output convention). Every file the run writes follows its type's naming pattern (CONFIG § Types).

The Review Summary is the **gate file** (CONFIG § Group approval): approving it auto-approves the rest of the container, and the filing-agent files the set per CONFIG § Subfolders.

**Loop behavior.** Across loop iterations the container folder, its `<notes>/` subfolder, and the filenames stay **stable** — no per-loop subfolder. Only a file's own `-vNNN` suffix advances when another round of revision replaces it, archiving the prior copy first. A new container is created only when the user re-runs the skill from scratch. After 2 attempts on a finding (CONFIG § Loop budget), classify it an **open item** with a named resolution path, and raise it to the `<user-queue>` if not easily inferred.

## Invocation

<!-- note-kit:sync pipeline-protocol — transposed from CONFIG.md § Pipeline protocol by sync_config; edit CONFIG, not here -->
| mode | behavior |
| ---- | -------- |
| interactive | pause at the skill's named checkpoints for in-chat confirmation; the default when a user invokes the skill directly |
| automatic | run end to end; flag ambiguities in the working set; raise open questions to `<user-queue>` without stopping; the default for action-agent dispatch |
| queue | run the first stage only, write one `<user-queue>` proposal per genuine ambiguity, and stop; the action-agent re-invokes on the working set once the user answers (resume is queue-verified — a working set claiming its own resolution is refused) |

Stages run in order, each writing its own file and reading only the prior stage's output — producing and judging contexts never mix. The `<inbox>` container is the resume state: the gate file at its root and **all transient material in its `<notes>` subfolder, never pooled at the container or inbox root**; no separate paused-run artifact exists. A re-invocation reads the partials and continues from the first unsettled unit, versioning up archive-first (CONFIG § Versioning and archiving discipline). Findings loop per CONFIG § Loop budget, then take a firm verdict with a named resolution path. Inside a sub-agent, stages run serially with identical output; spawned sub-agents run on the top line model (CONFIG § Sub-agent execution).
<!-- /note-kit:sync pipeline-protocol -->

**This skill's checkpoints and ambiguities:** interactive pauses after Parse (to surface the directive list) and after Audit (to show findings before Revise); an ambiguity is a comment with more than one reasonable reading — the first stage is Parse, the resume state is the plan's checked boxes, and on a verified resume each answered directive is amended while the directive list stays otherwise locked.

## Stage 1 — Parse

**Input:** the target document(s) + the review commentary. Author the plan through the note-kit-plan skill at `<notes>/<target>-plan-vNNN.md` (`type: plan`) **before** any sub-agent spawns — the plan is the resume log and every sub-agent reads it.

1. Resolve the review target (§ Review Target): copy it into the container's `<notes>/` subfolder as the working copy and edit the copy, never the source. Note the target and version in the plan.
2. Read the target and the commentary. Identify every comment — ALL-CAPS notes, bracket annotations, marked passages, marginal notes, or a separate review file.
3. Load the crit spec. The resolute standards on each axis (the `voice`, `design`, and `format` notes; CONFIG § Types) contribute one named pattern-set per axis — each note one check; the CONFIG protocol sections governing the target's artifact type contribute a conformance set. Carry these as the crit pattern-sets Stage 3 audits against, alongside the directives.
4. Per comment, extract one discrete directive in imperative mood, carrying three parts: the **located target** (section, sentence, or passage), the **action verb** (replace / cut / expand / reorder / define / reconcile), and the **intent** in the user's own terms — e.g. "Replace X with Y in section Z". One comment naming two independent changes splits into two directives. Trace each directive back to its source comment so Stage 4 can audit fidelity.
5. A comment with more than one reasonable reading → `[ambiguity: <description>]`: defer the reading and raise it in chat (interactive), to the queue (queue mode), or as a flag in the plan (automatic).
6. Compile patterns — recurring issues across two or more comments, each named and slugged, as global checks.
7. Generalize a directive to similar issues the user may not have seen only when at least one other directive matches the pattern; label those `inferred-...`.
8. Group and order directives by dependency through the note-kit-plan skill; groups touching disjoint documents run in parallel. Number each group, pattern, and directive within a group, ordered by severity. Each directive is a checkbox Apply ticks and Audit verifies.

**Constraint:** add only the directives the user wrote. **Output:** the plan file.

## Stage 2 — Apply

**Input:** the plan + the target sections. One sub-agent per directive group, in dependency order, each prompted per § Stage sub-agent prompts → Stage 2. Each edits the WIP target version of that file, exactly what its directives name, checks every pattern against its section, and reports the change back. A Stage 2 sub-agent **does not flip its own plan boxes**: its report is a claim the Stage 3 independent read confirms before any box is checked. A directive the sub-agent cannot land is reported as an open item naming the blocker — never quietly dropped or scoped out.

## Stage 3 — Audit

**Input:** the working copy + the plan + the crit pattern-sets from Stage 1. The audit is the **independent-read gate**: every lane is a sub-agent that did not produce the edits it reads, so its confirmation — not the producer's report — flips a plan box. Spawn one **cold-reader lane**, one **crit lane per crit pattern-set**, and one **verify lane**, as parallel contexts, none shared, each prompted per § Stage sub-agent prompts. Every lane — Apply, cold-reader, crit, verify, Revise — runs on the top line model (CONFIG § Sub-agent execution).

The lanes only read and emit findings; the coordinator is the single writer that merges all lanes into one versioned-up plan, flips the boxes the verify lane confirmed, and repeats Stage 1's directive-building steps over the new findings. Findings touching the same passage reconcile into one directive. **Output:** `<notes>/<target>-audit-vNNN.md` (the consolidated lane findings) and the versioned-up plan. Done when every directive box is verified-checked or carries an open item and no substantial finding remains, or the loop budget is reached.

## Apply–Audit loop

Pass the versioned-up plan back to Stage 2. Loop each finding per CONFIG § Loop budget, then **open item** with a resolution path. In interactive mode, surface unresolved high-severity findings in chat before re-triggering.

## Stage 4 — Revise

**Input:** the review target(s) + the completed plan files + the edited working copy. The Revise sub-agent compares the working copy against both the original document and the original commentary.

1. Read all prior outputs first — what was created, removed, corrected, and left open.
2. Look at the project as a whole, all files together in context — top-level changes affecting interoperability, consistency, format and standardization.
3. Make final edits in the working copy, reinforcing earlier sub-agent changes where needed, especially global notes.
4. Re-check patterns one last time against the user's voice, design, and format standards filed in their notes. Voice governs phrasing; design governs structure and methodology; format governs visual conventions. Pay special attention to guidance in that project's project folder, if present.
5. Confirm the plan log is honest: every box is verified-checked, and every unresolved item carries an open entry naming the files and the fix. No box is checked on a producer's claim alone, and no noticed defect is left as a silent scope cut.
6. Write the Review Summary to the container root — the only file at root, the **gate** file. State the chosen target and version method (source version → new container version) here:

```
## Review Summary
### Directives resolved
- [#N] [what changed]
### Pattern coverage
- [pattern-slug]: [N] resolved, [M] remaining (with locations)
### Open items
- [what could not be resolved and what would resolve it]
```

If the changes were targeted and affected 10 lines or fewer, include the verbatim edits in the summary, linking the edited section, appended to "Directives resolved".

**Output:** `<notes>/<logs>/<target>-log-vNNN.md`.

## Stage sub-agent prompts

The coordinating agent constructs each sub-agent prompt from the blocks below. The working copy lives at `<notes>/<target>-vNNN.md`, the plan at `<notes>/<target>-plan-vNNN.md`; across loop iterations these paths stay stable — only a file's own version suffix advances. A producing lane and a verifying lane never share a context.

### Stage 2 — Apply (one sub-agent per directive group)

> Satisfy the directives in this group, one of many in a multi-stage revision.
> Your directives: [PASTE ONLY THIS GROUP'S DIRECTIVES BY SEQUENCE NUMBER]
> Pattern list (check globally across your section): [PASTE FULL PATTERN LIST + SLUGS FROM THE PLAN]
> Target section: [PASTE ONLY THE RELEVANT SECTION(S) — not the full document unless a directive is cross-document]
> Tools: name custom retrieval tools (`mcp__vault__vault_search`) explicitly; you inherit no MCP context.
>
> 1. Read the directives and the source in scope. The working copy is the source copied verbatim to `<notes>/<target>-vNNN.md`. **Confirm the verbatim copy exists before editing.**
> 2. Change exactly what each directive asks — touch nothing it does not name.
> 3. Check each pattern in the list against your section.
> 4. Write the revised section under its directive number, listing the documents touched. Edit the working copy in place. Leave versioning to the coordinator and box-flipping to the audit — your report is a claim the audit verifies before a box flips.
> 5. A directive you cannot land: report it as an open item naming the blocker and what would resolve it. Never narrow your scope to skip it.

### Stage 3a — Cold reader (one sub-agent, first-time reader)

> Read the compiled draft as a first-time reader. You have NOT seen the plan, any directive, or any crit checklist — work from the text alone.
> Your input is the working copy at `<notes>/<target>-vNNN.md`.
> Tools: name custom retrieval tools explicitly.
>
> 1. **First pass — straight through, for comprehension.** Read the highest-version working copy per document end to end without stopping to fix anything, as a reader meeting it for the first time. At the end, state in one line what the document claims and whether it held together. A spot where you had to re-read to follow the thread is a finding; log where it was.
> 2. **Second pass — targeted, for edit damage.** Hunt the seams edits leave, each its own check:
>    1. broken flow between adjacent passages;
>    2. emergent redundancy — the same information made twice;
>    3. contradictions introduced by edits;
>    4. unclear antecedents — a pronoun whose noun was edited away;
>    5. sections that no longer connect;
>    6. a defined term used before its definition;
>    7. logic or code bugs from combined edits;
>    8. formatting inconsistency — a repeated token, format, or code block spelled two ways (`<wildcard-token>` in one place, `!TOKEN-WILDCARD!` in another); standardize to the dominant convention.
> 3. Flag a pattern only where it appears more than twice. Check for scope creep, redundancy, and missing / incomprehensible / outdated references.
> 4. Emit a findings table per section, then a one-paragraph summary of what the audit changed overall:
>
>    | Section | Finding | Issue type | Severity (high / med / low) | Suggested correction |
>    | ------- | ------- | ---------- | --------------------------- | -------------------- |

### Stage 3b — Crit lane (one sub-agent per crit pattern-set)

> Audit the compiled draft against one crit pattern-set, one lane of several. You judge only your set; another lane covers the rest.
> Your pattern-set: [PASTE ONE PATTERN-SET — the resolute standards on one axis (voice, design, or format), one note per check, or the CONFIG-conformance set for the target's artifact type. Each line is one check with its governing note.]
> Your input is the working copy at `<notes>/<target>-vNNN.md`.
> Tools: name custom retrieval tools (`mcp__vault__vault_search`) explicitly; you inherit no MCP context.
>
> 1. For **every** check in your set, read the whole working copy and decide its verdict: pass or deviation. Emit one row per check.
> 2. On a deviation, give the exact location (section, line, or quoted phrase) and the governing note. On a pass, mark it pass.
> 3. Order the rows **heaviest first** by each standard's stored `weight`, read from the per-axis standards index (the folder's cover index). A check you genuinely cannot evaluate from the working copy is an **open item** carrying what would resolve it — never a silent drop.
> 4. Emit the verdict table, nothing else:
>
>    | Check | Verdict (pass / deviation / open) | Location | Severity (high / med / low) | Governing note | Correction |
>    | ----- | --------------------------------- | -------- | --------------------------- | -------------- | ---------- |

### Stage 3c — Verify lane (one sub-agent, independent-read gate)

> Confirm each applied directive is actually present in the working copy. You did NOT produce these edits; you are the independent read that lets a plan box flip.
> Your directive list: [PASTE EACH STAGE 2 DIRECTIVE BY SEQUENCE NUMBER, WITH ITS LOCATED TARGET AND INTENT — from the plan]
> Your input is the working copy at `<notes>/<target>-vNNN.md`.
> Tools: name custom retrieval tools (`mcp__vault__vault_search`) explicitly; you inherit no MCP context.
>
> 1. For **every** directive, open the working copy at its located target and read whether the intended change is present. Do not trust any "done" report — read the file.
> 2. Emit one row per directive: `applied` (the change is in the file) or `absent` (claimed but not found / partial). On `absent`, quote what is there now.
> 3. A directive that names a defect the edit dodged by narrowing scope is `absent`, carrying the files and fix that would resolve it.
> 4. Emit the verdict table, nothing else:
>
>    | Directive # | Verdict (applied / absent) | Located target | What the file shows | Resolution if absent |
>    | ----------- | -------------------------- | -------------- | ------------------- | -------------------- |
