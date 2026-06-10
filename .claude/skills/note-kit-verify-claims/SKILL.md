---
description: Take an existing document, find a primary source for every factual claim,
  assign a verdict, and produce a citation-backed copy. Triggers on "verify this doc",
  "verify the claims in", "fact-check this", "produce a citation-backed version",
  "run a citation pass on".
name: note-kit-verify-claims
---


# verify-claims

Hand this skill a document and it produces a citation-backed copy: every factual claim atomized, traced to a primary source, and laddered to a verdict carried in an inline footnote. The output carries claims, citations, and verdicts — nothing about the run that produced them. Verdicts come from the shared ladder (CONFIG § Loop budget): VERIFIED / DISPUTED / UNRESOLVED.

## Output

The verified copy is merged onto the document it verifies, not left beside it:

- **In-vault target** (an existing vault note): write the cited version directly onto the canonical path. Archive and version the un-verified original first (Phase 4's merge step); the filename is unchanged.
- **External or pasted text** (no vault home): land a new draft at `<inbox>/<target>-verify-claims/`, the source untouched. A new file is named by its type's pattern (CONFIG § Types).

Frontmatter on the output: keep the source doc's own `type`, `tags`, `date`, and `parent`, then add the `claims-verified` tag, `source: <relative path to the archived original or source doc>`, `status: draft`, and `reviewed: false`. External or pasted text with no frontmatter takes the type the kit infers for it. No verdict-count fields — Phase 5 logs per-run stats to the archive.

## Invocation

<!-- note-kit:sync pipeline-protocol — transposed from CONFIG.md § Pipeline protocol by sync_config; edit CONFIG, not here -->
| mode | behavior |
| ---- | -------- |
| interactive | pause at the skill's named checkpoints for in-chat confirmation; the default when a user invokes the skill directly |
| automatic | run end to end; flag ambiguities in the working set; raise open questions to `<user-queue>` without stopping; the default for action-agent dispatch |
| queue | run the first stage only, write one `<user-queue>` proposal per genuine ambiguity, and stop; the action-agent re-invokes on the working set once the user answers (resume is queue-verified — a working set claiming its own resolution is refused) |

Stages run in order, each writing its own file and reading only the prior stage's output — producing and judging contexts never mix. The `<inbox>` container is the resume state: the gate file at its root and **all transient material in its `<notes>` subfolder, never pooled at the container or inbox root**; no separate paused-run artifact exists. A re-invocation reads the partials and continues from the first unsettled unit, versioning up archive-first (CONFIG § Versioning and archiving discipline). Findings loop per CONFIG § Loop budget, then take a firm verdict with a named resolution path. Inside a sub-agent, stages run serially with identical output; spawned sub-agents run on the top line model (CONFIG § Sub-agent execution).
<!-- /note-kit:sync pipeline-protocol -->

**This skill's checkpoints and ambiguities:** interactive pauses are the "Check with the user first" questions below plus any would-be UNRESOLVED claim surfaced before the cap closes it; an ambiguity is an unresolvable source or merge-target question — the first stage is claim extraction, and the resume unit is the first unsettled claim (check the partial set in `<notes>/` before re-extracting). An in-vault target with no container carries the same state inline in its own partial render; the gate file is the verified copy itself.

## Check with the user first

Ask only when one applies, then proceed on confirmation: a claim-sparse doc (essay, opinion, creative draft) — "this reads as opinion, so the pass may produce few or zero footnotes; proceed?"; a transcript or speech — "verifying dialogue attributes claims to the document, not the speaker; fact-check mode or a lighter sourcing pass?"; a very short doc (under ~5 claims) — "faster by hand; still want the full pass?". If the user offers local sources (a book PDF, a paywalled paper they hold, a session log for personal-observation claims), take them; the skill never scans the vault or any directory for sources, so each one is offered explicitly. The drafts location is the kit `<inbox>`, not asked.

## Phase 1 — Extract claims

A claim is a falsifiable assertion — "Auto-compaction triggers at ~40% context" is a claim; "I find compaction unintuitive" is not. Hedging does not disqualify ("probably around 40%" still asserts); pure speculation asserts nothing. Skip preferences, opinions, stylistic characterizations ("elegant", "powerful"), tautologies, and terms the doc itself coins.

**Atomize before verifying.** A claim is checkable only as a single falsifiable unit:

1. Walk the doc sentence by sentence; a sentence asserting two or more separable facts splits into one claim each ("X shipped in v3 and doubled throughput" → "X shipped in v3" + "X doubled throughput") — they can land on different rungs.
2. From a mixed sentence, lift the assertion and drop the aside; from a compound assertion, keep each falsifiable unit and discard connective framing.
3. Resolve each claim's references in place — replace pronouns and "it/this/the former" with the noun, so the atom stands alone when read as a footnote.
4. Genuinely ambiguous → treat as a claim.

Hold the claim set in memory for a short pass; for a long doc, persist the partial set in the container's `<notes>/` so a re-invocation continues from it. Write no per-claim files.

**Priority.** STRUCTURAL = load-bearing, the doc's argument fails if it is wrong; people, places, times, prices. FUNCTIONAL = supporting color, the doc survives if it is off (e.g. "raised christian" vs "raised catholic"). Priority shows in the footnote header; among UNRESOLVED claims, triage STRUCTURAL first. Reference docs, skill files, and handoffs start at STRUCTURAL minimum.

## Phase 2 — Domain + primary source

Pick a verification domain per claim — the knowledge area that defines its primary source: `api-docs` (the vendor's reference), `academic` (the paper itself — DOI, PMID, publisher PDF), `industry-standard` (the RFC/W3C/ISO itself), `news-of-record`, `vendor-blog`, `personal-observation` (the user's own dated observations, valid only for their environment and only when they name the source), `anecdotal` (lowest — forum/social posts, weight only when a critical mass aligns with evidence). When a claim's subject fits none, coin a doc-local domain (e.g. `food-safety`) without asking and name its authority — a provisional footnote label, correctable later, not a locked vocabulary. That a document exists somewhere is not enough; it must be accessible and legible.

Primary means the vendor's own docs for their product, the paper not a post about it, the standard not a summary, the ruling/legislation/filing for legal claims, a **published dataset from the authority** (a data.gov or agency JSON, CSV, or XLS, including the data a lookup app serves — treat a record in it as quotable; point the citation link at the human-readable home or lookup tool, not the raw file, and keep the record id and value in the footnote text), or a user-offered authoritative file. Not primary: Wikipedia and tertiary sources (cite what they cite), blog posts summarizing a primary, the user's own vault notes, AI-generated summaries, a press release standing in for the thing it announces, or a search-engine result summary — it points to a primary, it is never itself the quote. Replace a secondary candidate with the primary it references where findable, else raise it to the user; reject tertiary or AI-generated. Batch ambiguous source questions into one round before fetching.

## Phase 3 — Evaluate

**Retrieve.** Get a source in hand by trying these paths in order; each path tried is one loop attempt (CONFIG § Loop budget):

1. **User-offered local file** — open it, locate the passage.
2. **Published authority dataset or lookup app** — when Phase 2 named one (a `.gov`/agency JSON, CSV, XLS, or the data an interactive app serves), fetch the underlying data endpoint and read the record directly. An app having no static page is not a dead end; the data behind it is the primary source.
3. **Web** via WebFetch/Defuddle — 1–3 URLs, 30s timeout, one retry after 5s on 4xx/5xx/timeout.
4. **Internet Archive fallback** — if a live fetch is blocked (403/4xx, routine for `.gov`/`.mil` and other bot-walled hosts), query `http://archive.org/wayback/available?url=<URL>`, then fetch the `id_` raw snapshot (`web.archive.org/web/<timestamp>id_/<URL>`) and extract from it. Cite the canonical URL and append "via Internet Archive snapshot YYYY-MM-DD" to the footnote.

If a fetched URL is derivative, follow the chain to the primary and re-fetch. Quote only verbatim text from a fetched primary, never a search-result summary, which paraphrases or invents specifics the primary lacks. Extract a verbatim quote ≤30 words; before embedding, replace inner `"` with `'`, escape `]` and `)`, strip HTML.

**Ladder.** With a source in hand, walk the verdict ladder top-down and settle at the first rung that holds:

1. **VERIFIED** — the primary source's own words assert the claim at its stated scope (a passage saying "≈40%" verifies "around 40%", not "exactly 40%"), and no primary source you found contradicts it. Mismatched scope is not a verification: narrow the claim or drop a rung.
2. **DISPUTED** — holding a supporting reading, test for a contradicting one: at least one primary source contradicts the claim at the same scope. Cite both.
3. **UNRESOLVED** — no primary source resolves it either way, and the loop cap is reached (below).

Do not stop at **VERIFIED** without the contradiction check, and do not record **UNRESOLVED** for a claim a retrieved primary actually settles.

**Loop cap and settling rule.** Resolving one claim follows the shared loop budget: `loop_count` starts at 0 and caps at 2 retrieval attempts (CONFIG § Loop budget); the paths above and any user-named source are those attempts. At the cap, the claim takes a firm verdict: VERIFIED, DISPUTED, or UNRESOLVED with a named limit category and a concrete resolution path (Phase 4). There is no "more research needed" and no open-ended search; a claim a named authority could still settle records that authority as its resolution path, not a non-finding. When a user is present, surface a would-be UNRESOLVED claim in chat before the cap closes it: the user may name a source, accept a reframing, or confirm it is editorial. Unattended runs route per Phase 5.

| Verdict    | Rule                                                                                                                                                  |
| ---------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| VERIFIED   | a primary source asserts the claim; no primary source or overwhelming majority contradicts it                                                         |
| DISPUTED   | at least one primary source or overwhelming public majority contradicts. The copy shows both views in a `> [!quote]` callout above the cited sentence |
| UNRESOLVED | no primary source resolved it within the loop cap. Rendered as an inline admission (Phase 4) with a methodology-limit footnote                        |

Where no primary returns the claim but two or more secondary sources independently corroborate, render it VERIFIED and note the secondary basis in the footnote.

## Phase 4 — Render & merge

**Markers.** Inline marker on the claim: `...text[^slug].` — plain, no span wrapper; presentation belongs to the UI plugin (CONFIG § Optional UI plugin). Slug is 1–3 content words from the claim, lowercase, 40 chars max, collision-suffixed; renaming the doc never changes a slug. Multiple markers stack with no separator.

**Footnote — VERIFIED / DISPUTED.** One line: verdict tag, an em-dash, then the citation. Single-line only — a two-line footnote's indented continuation renders as raw text in Obsidian Live Preview and the link never resolves:

```
[^slug]: **VERDICT | PRIORITY | DOMAIN** — [Source title — section](URL-or-path) — "verbatim quote ≤30 words" Accessed YYYY-MM-DD.
```

Keep the footnote lean: verdict tag, citation, quote, and at most one short forward-useful caveat (a scope limit, a value the source omits). Never a process note — what a value used to be, what the run tried, what was corrected, softened, or extended. The footnote serves the next reader.

Claims sharing one source emit ONE definition; the header appends ` · also cites slug-b, slug-c`. DISPUTED adds a callout above the sentence:

```
> [!quote] **DISPUTED · <PRIORITY>**
> <claim text as the source doc has it>[^slug]
> **Conflict:** <what the contradicting primary source asserts.>
```

**Reframe a derivable claim before admitting it.** Some UNRESOLVED claims are syntheses of other claims already cited in this doc, not unsourced facts. Reframe those as connective prose pointing to the cited rows ("keeps only as long as its shortest-lived component, below") and drop the standalone footnote. Only a claim with no such grounding falls through to the admission.

**Unknowns become admissions, not hidden flags.** An UNRESOLVED claim with no grounding is never left asserting itself behind a footnote. Rewrite the body sentence to admit the limit plainly so the prose carries the context, then shrink the footnote to a methodology-limit quick-ref stating only why it stayed open:

```
[^slug]: **UNRESOLVED | PRIORITY | DOMAIN** — Limit: <category> — <one-line resolution path>
```

- *Before:* "Practical window: **3–5 days** for good eating." + a bare UNRESOLVED marker
- *After (prose):* "No authority publishes a shelf life for an assembled plate; as a rough estimate, **3–5 days** for good eating."
- *After (footnote):* `**UNRESOLVED | STRUCTURAL | food-safety** — Limit: no-authority — no body publishes an assembled-plate shelf life; derived estimate only.`

The admission states the limit in plain prose. It never narrates the run: no "automated verification failed", no "the app did not return a quote", no "should be checked rather than assumed".

**Limit categories** (enumerated and stable, so the analyst-agent can tally them across `claims-verified` notes): `no-authority` (editorial, no source can exist), `source-silent` (an authority exists but does not address the point), `no-url`, `paywalled`, `version-specific`, `private-api`, `local-env`. The `no-authority` split from `no-url`/`paywalled` separates a claim that *cannot* be sourced from one that simply *was not* this run. Each UNRESOLVED claim also gets a task line: `- [ ] Re-verify: <slug> in [[<stem>]] — <category>: <one-line fix>`. The fix names a concrete path — a publication, an endpoint, an access method, a reframing — never "research more" or "TBD". A `no-authority` claim is terminal: no re-verify line; its admission is the resolution.

**Voice.** The merged note is read cold, so it follows the kit's prose-voice standard. Write any prose you add — recontextualized admissions, the Phase 5 summary — in that voice: no "not X, it's Y", no hedging filler, no apology. Prefer a comma, colon, or fresh sentence to an em dash where the line reads as well without one. Do not mechanically strip em dashes or rewrite the author; flag deeper voice issues instead. The footnote citation format keeps its ` — ` separators.

**Write order & validation.** Body = the prose with markers (unknowns recontextualized); append `## Sources %% fold %%` (merge into an existing footnote-only Sources block; halt and ask if that heading holds other prose) with the definitions, plus any DISPUTED callouts. Build the render as the partial output in the container's `<notes>/` (in-vault target: as the in-progress copy beside the work), so an interrupted pass resumes from it. **Validation:** every body `[^slug]` has exactly one matching definition; every definition has at least one body marker; no two definitions share a slug. Any failure → leave the partial render in `<notes>/` with the diagnostic in chat and exit; the next invocation resumes from it.

**Merge step (in order):**

1. **Archive the un-verified original first, dated** → `<archive>/<source-path>/<YYYY-MM-DD>-<filename>.md`; confirm the copy exists before overwriting (CONFIG § Versioning and archiving discipline). The archive keeps every claim verbatim.
2. **Commit the verified copy.** In-vault target: write the validated render onto the original filename. External/pasted text: promote the validated render to the container root as the cited copy.
3. Hand off (Phase 5).

## Phase 5 — Handoff

**Log the run.** Call `python <kit-root>/scripts/verify_claims_log_v002.py --source <rel-path> --total N --verified A --disputed B --unresolved C`; it appends one pipe line to `<logs>/verify-claims/verify-claims-runs.md`. Verdict counts live there, not in the note.

**Unattended runs — route the unknowns.** With no user to talk to, each UNRESOLVED claim becomes a `<user-queue>` item (CONFIG § Queue protocol) carrying its named resolution path. If unresolved work blocks completion entirely — a STRUCTURAL claim that guts the doc, or an undetermined merge target — write the open question to `<user-queue>` and keep run state in the working set; the action-agent re-invokes the skill on that set once the user answers.

**Summary** (under 200 words): source file, the merged/output file, the verdict tally, the UNRESOLVED count with its limit categories and task lines, and one line on reading the doc — each cited claim has a footnote marker; click through to Sources for quote and URL; unknowns are admitted inline. If validation failed, give the partial-render path in `<notes>/` and the assertion message instead.
