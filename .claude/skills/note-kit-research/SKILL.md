---
name: note-kit-research
description: Multi-stage research pipeline that turns a mission brief into a cited, red-teamed research note from human-made authoritative sources. Activates when the user asks to "research X", "build a roadmap for", "compare options for X", "stand up X", "spec out X", or invokes /note-kit-research with a mission brief. Four isolated stages, Scope, Research, Crit, Synthesis, each reading only the prior stage's output. Topic-agnostic.
---

# research

Mission brief in, one cited briefing out. Four stages run in order — Scope, Research, Crit, Synthesis — each writing its own file and each sub-agent reading only the prior stage's output. Not for single-question lookups or work that needs execution rather than research. If running inside a sub-agent (no nested spawn): run the four stages serially in this context, same output, no parallelism.

## Definitions

| term                 | meaning                                                                                                                                                                                                                                                                                             |
| -------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Mission scope        | one declarative sentence drawn verbatim from the brief: domain, scale, context, exclusions. Locked at Stage 1; no stage may revise it. A claim is in scope when the mission cannot be answered without it. If ambiguous or later stages contradict, pause and raise the issue to the `<user-queue>` |
| Research area        | a named sub-topic investigable on its own, without another area's results first. Each produces one section of the Research file.                                                                                                                                                                    |
| Source               | a document with an identifiable author and date, retrieved this run and read in full, not by abstract or summary. Training-data knowledge is not a source.                                                                                                                                          |
| Load-bearing claim   | one whose negation would force a revision to at least one Synthesis conclusion.                                                                                                                                                                                                                     |
| Conflict of interest | the author has a financial or reputational stake in the claim, typically a vendor on its own product. Not disqualifying, but sole support for a load-bearing claim marks it `provisional` until a second independent source corroborates.                                                           |
| Moderating variable  | a named difference in method, population, period, or definition that reconciles two diverging sources without either being wrong. "Different contexts" is not one.                                                                                                                                  |
| Open item            | a scope-relevant question this pass cannot answer, stated as a specific answerable question.                                                                                                                                                                                                        |
| Admissible           | a finding the Synthesis may carry: load-bearing to a mission conclusion, sourced to a full-text read, and either confident or carrying its uncertainty marker. Working detail that fails this stays in the Research file.                                                                           |

## Output

One container, `<inbox>/<topic-or-date>-research/`. The Synthesis is the only file the user must review: the only file at the container root, the gate file for group approval. The Scope, Research, and Crit files are working material in `<notes>/`, auto-filed when the root synthesis is `reviewed: true`. All files carry `type: research`, `project: "[[Name]]"`, `mission: "<locked scope, verbatim>"`, and `research-stage:`. Stamp `project:` only after confirming the target exists — one vault search or project listing check; no match → leave the field empty for downstream inference, never invent a name. Naming is `<Mission-Slug>-<Stage>-vNNN.md` (CONFIG § Types); the slug is the first three or four content words of the brief.

## Invocation

<!-- note-kit:sync pipeline-protocol — transposed from CONFIG.md § Pipeline protocol by sync_config; edit CONFIG, not here -->
| mode | behavior |
| ---- | -------- |
| interactive | pause at the skill's named checkpoints for in-chat confirmation; the default when a user invokes the skill directly |
| automatic | run end to end; flag ambiguities in the working set; raise open questions to `<user-queue>` without stopping; the default for action-agent dispatch |
| queue | run the first stage only, write one `<user-queue>` proposal per genuine ambiguity, and stop; the action-agent re-invokes on the working set once the user answers (resume is queue-verified — a working set claiming its own resolution is refused) |

Stages run in order, each writing its own file and reading only the prior stage's output — producing and judging contexts never mix. The `<inbox>` container is the resume state: the gate file at its root and **all transient material in its `<notes>` subfolder, never pooled at the container or inbox root**; no separate paused-run artifact exists. A re-invocation reads the partials and continues from the first unsettled unit, versioning up archive-first (CONFIG § Versioning and archiving discipline). Findings loop per CONFIG § Loop budget, then take a firm verdict with a named resolution path. Inside a sub-agent, stages run serially with identical output; spawned sub-agents run on the top line model (CONFIG § Sub-agent execution).
<!-- /note-kit:sync pipeline-protocol -->

**This skill's checkpoints and ambiguities:** interactive pauses after Scope for confirmation (optionally after each Crit pass); an ambiguity is a Stage-1 scope question — the first stage is Scope, and the locked mission scope never changes on resume.

## Stage 1 — Scope

**Input:** the mission brief.

1. Extract the user's exact words for scope, scale, named contexts, and constraints. Stamp each verbatim and locked. Add nothing.
2. List the research areas, names only.
3. Per area, write a one-line scope and a ranked source-type list, best first: official docs, source code, standards, primary research, regulatory filings, vendor specs; then reproducible tests and benchmarks with documented method; then systematic reviews and meta-analyses; then reviewed technical journalism; then well-moderated practitioner forums and named-author blogs; lowest, anonymous posts, undated guides, marketing copy. Flag any source authored by a vendor whose product is being assessed.
4. Where the brief admits more than one reading, surface a clarification: in chat (interactive), to the queue (queue mode, automatic mode).

**Constraint:** do not recommend against the brief; ask for clarification in the queue. **Output:** `<parent>/<notes>/<Mission-Slug>-Scope-vNNN.md`.

## Stage 2 — Research

**Input:** Scope file only. One sub-agent per research area — top line model (CONFIG § Sub-agent execution) — spawned with:

> Task: investigate one slice of a multi-sub-agent research pipeline.
> Your research area: [AREA NAME]
> Locked mission scope (immutable, do not challenge or revise): [PASTE LOCKED SCOPE BLOCK]
> Your area's scope: [PASTE ONE-LINE SCOPE]   Source priority: [PASTE RANKING]
> [the procedure below]
> Tools: use `mcp__vault__vault_search` before Glob or Grep for vault lookups, defuddle or web search for sources.

Each sub-agent:

1. **Plan before retrieving.** Generate sub-questions from multiple angles, at least one query each; self-check the set.
2. **Generate perspectives.** Identify three to five distinct stakeholder or researcher standpoints, derive queries from each independently, merge and dedupe.
3. **Retrieve from multiple channels.** Every run consults (a) two sources of differing retrieval scope, (b) one grey-literature channel, (c) the reference lists of top results, chasing citations backward. Document all three.
4. **Cite as you write.** Write each paragraph from a specific retrieved source; assign its `[^slug]` footnote marker before writing the prose around it.
5. **Iterate.** After the first pass, name coverage gaps and run follow-up queries, at least two rounds for complex sub-questions.
6. Zero results on a sub-question becomes an explicit open item naming the sub-question. No silent gaps.
7. Produce structured findings: scope, claims with inline `[^slug]` citations, an uncertainty marker per claim, costs and timelines where relevant.

**Uncertainty marker** (one word in parentheses, after the claim, before the `[^slug]`): `(confident)` multiple independent sources converge · `(provisional)` single source or low precision · `(inferred)` derived from adjacent evidence · `(contested)` a source directly contradicts at the same scope, both cited.

The coordinator merges per-area outputs into one Research file, restating the locked scope at the top. **Output:** `<parent>/<notes>/<Mission-Slug>-Research-vNNN.md`, each area:

```
## [Area name]
### Scope            [one-line scope, verbatim]
### Sources consulted
- Channel (a): [sources + search strings]
- Channel (b): [grey-literature channel + strings]
- Channel (c): [citation chasing, which reference lists]
### Findings         [structured, inline [^slug], uncertainty markers]
### Open items       [any open item raised here]
```

Sources go at the bottom as `[^slug]` footnote definitions.

## Stage 3 — Crit

**Input:** Research file only; the locked scope is read from its header. One crit sub-agent per area — top line model — spawned with:

> Task: audit one slice of a research pipeline for factual and methodological flaws.
> Your area: [AREA NAME]   Your input is the Research file; the locked scope is in its header.
> [the procedure below]
> Tools: use `mcp__vault__vault_search` before Glob or Grep for vault lookups; you inherit no MCP tools, so they are named here.

Each crit sub-agent:

1. Per load-bearing claim, read the cited verbatim quote: does it cover the claim's stated scope? A mismatch is a scope-gap finding.
2. Search for contradicting sources; each direct contradiction at the same scope makes the claim `(contested)`, both cited.
3. Name missing perspectives: stakeholders or failure-mode experts absent from the Research file.
4. Detect cross-claim contradictions; name the moderating variable, or escalate to an open item naming the contradiction when none is findable.
5. Check source authority per sub-question. A vendor whitepaper is authority for "what the product does", not "is it a good fit". Sole support from a conflicted source downgrades the claim to `(provisional)`.
6. Emit a findings ledger per area, then a one-paragraph "what this changes":

| Claim | Finding | Severity (high / med / low) | Counter-evidence | Suggested resolution |
|---|---|---|---|---|

**Completion:** every high-severity finding is addressed — resolved through follow-up or reclassified — and the rest are medium or low with no actionable gap. A crit producing only medium or low findings passes; proceed to Synthesis. **Output:** `<parent>/<notes>/<Mission-Slug>-Crit-vNNN.md`.

## Research–Crit loop

For each unresolved high-severity finding, run a focused Research follow-up. The follow-up sub-agent (top line model) lists every source already consulted, identifies what the prior pass missed, defines at least one new approach (it may not re-query the same information), appends an addendum section to the Research file, then re-runs Crit on it. `loop_count` starts at 0 and increments per attempt; the cap is **2** (CONFIG § Loop budget). Still unresolved at the cap → classify the finding UNRESOLVED with a named resolution path: a specific test, document type, institutional authority, or access method. "Further research needed" is not one. Unresolved items travel to the Synthesis as named open questions, or to the `<user-queue>`; only Stage-1 scope ambiguities reach the queue.

## Stage 4 — Synthesis

**Input:** Scope, Research (all passes), and Crit (all passes). The synthesis sub-agent (top line model) writes the human-facing briefing, not a status report.

1. Read all prior outputs first: what the pipeline established, contested, and left open.
2. Admit by the Admissible test; working detail, per-channel search logs, and sub-questions that changed no conclusion stay in the Research file. The Synthesis answers the mission; it does not narrate the pipeline.
3. Write toward the inquiry: structure around the research question and its areas, not the pipeline stages. A reader who never saw the working files gets the same answer.
4. Carry each claim's uncertainty into the prose: `(confident)` reads as a declarative · `(provisional)` as "probably" or "likely" · `(inferred)` as "the evidence suggests" · `(contested)` names both sides with the moderating variable, or becomes an open item when none.
5. Cite as you write: every factual claim gets a `[^slug]` marker copied verbatim from the working files. Cite the source directly, not your research.

Required sections: **Scope** (locked, verbatim) · **Inquiry sections** (one per area) · **Open questions** (every open and UNRESOLVED item with its resolution path) · **Limitations** (perspectives uncovered, sources that dominated, sparse areas) · **Sources** (all `[^slug]` definitions). **Output:** `<parent>/<Mission-Slug>-Synthesis-vNNN.md` at the `<inbox>` research container root.

## Citation format

`[^slug]` footnote definitions, each opening with a plain-text verdict tag — presentation belongs to the UI plugin (CONFIG § Optional UI plugin). The slug is one to three hyphenated content words from the claim, not the source, lowercase, collision-suffixed `-2` or `-3`.

```
[^slug]: **(confident)** [Title](URL) — Author, Publisher, Date — "verbatim quote, 30 words or fewer." Accessed YYYY-MM-DD.
```

- Every factual claim needs at least one retrieval call; never cite a source whose full text was not fetched.
- A contested claim needs at least three distinct sources.
- A source older than 24 months is marked stale in its footnote; 12 months for AI or software-capability claims. Re-verify vendor pricing, versions, and regulatory rates against a live source when the prior one is 30 days older than the most recent version or update.
- Verify URLs live at output time; a dead link is marked unverified in its footnote. Never fabricate a DOI, arXiv ID, or other identifier.

## Inbox scrutiny loop

After the Synthesis lands, every user reaction during review is a verification event. Pushback on a claim: re-fetch the cited source live, compare, and write a `type: addendum` targeting the Synthesis if it is wrong. Scope expansion: a focused Research mini-pass on the new question, routed to the original's `<parent>`. Frame clarification: amend the Scope file via addendum and propagate to the affected sections. A forward-momentum question: answer in chat or `<user-queue>`. Exit when every reaction is resolved, corrected, reframed, or explicitly deferred.

## Resume an in-progress run

Resume per the pipeline protocol (§ Invocation). Research-specific: a corrected finding folds into the Scope file as an answered open item (the locked mission scope never changes); the continuation is a focused Research mini-pass, Crit on the addendum, then re-synthesis — and the Synthesis re-versions on every resume that changes a conclusion.
