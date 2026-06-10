---
name: note-kit-red-vs-blue
description: Adversarial hardening loop for any instrument that reads or judges a subject — a diagnostic script, a linter, a validation suite, a review checklist, an agent prompt. Plants known faults in copies of a healthy fixture, has manifest-blind sub-agents diagnose through the instrument alone, scores with layer attribution, hardens the instrument where it was blind, then escalates the test battery to attack the instrument's next-weakest element. Activates on "red team this", "red vs blue", "harden the <instrument> against <fixture>", "find this tool's blind spots", or /note-kit-red-vs-blue with an instrument and a fixture.
---

# red-vs-blue

Instrument and fixture in, hardened instrument and a scored blind-spot ledger out. The loop plants faults the orchestrator alone knows, lets fresh manifest-blind solvers diagnose through the instrument's output and nothing else, scores each match against the hidden truth, hardens the instrument where the signal failed, and then **escalates the fault battery** so the test always attacks the instrument's current weakest element. Both sides improve every round: the instrument grows detectors; the battery grows fault classes the instrument has not beaten. Use when an instrument's worth depends on what it *fails to surface* — and a quality review can only find what the reviewer imagines. Not for revising prose against commentary (`/review`), verifying factual claims (`/verify-claims`), or one-off debugging of a known fault.

Running inside a sub-agent (no nested spawn): run matches serially in this context, holding the manifest in a closed file the solving pass never opens; identical scoring, no parallelism (CONFIG § Sub-agent execution).

## Definitions

| term | meaning |
| ---- | ------- |
| Instrument | the artifact under hardening: anything that reads a subject and reports — a script, test suite, checklist, prompt, agent. It must be versionable and re-runnable on demand. |
| Fixture | a healthy subject the instrument reads — a scene file, codebase, document set, dataset. Its ground truth is establishable, and copies of it can be broken safely. |
| Lens | any summarizer or digest between the instrument's raw output and the solver's eyes. The lens is part of the system under test and is scored as its own layer. |
| Control | an untouched fixture copy. Its instrument output is the **noise floor**; every score subtracts it. |
| Variant | a fixture copy carrying one or more planted faults, prepared in a sandbox so the preparation itself changes nothing else (see NOOP baseline). |
| Seed manifest | the orchestrator-only record of every planted fault: site, mechanism, expected surface signal, source in the fault catalogue. The blind side never reads it; a leak voids the round's matches. |
| Healthy decoy | a variant with **no** fault, indistinguishable in framing from a faulted one. Decoys measure precision — an instrument that cries wolf fails differently than one that misses. |
| Match | one variant (or decoy) → instrument run → blind solve → reveal → score. The unit of the loop. |
| Blind solver | a fresh sub-agent that receives only the instrument's output and neutral framing. No manifest, no control, no memory of prior matches, no knowledge of whether a fault exists. |
| Layer attribution | every miss is charged to a layer: **raw** (the signal is absent from the instrument's full output), **lens** (present in raw, lost in the digest), or **solver** (present in what the solver read, not acted on). Hardening targets the charged layer. |
| Provenance | the documented origin of a fault, one of three admissible classes: **user-reported** (the user hit it, reported it, or surfaced it in live confirmation), **ledger** (a recorded blind spot or documented-hard of this instrument or a kindred one), or **community** (a user-submitted issue on a forum, tracker, or practitioner thread about similar tools or scripts, cited). A fault with no provenance is inadmissible — invented faults produce easily-solved matches that flatter both sides. |
| Softball | a match the current instrument was already expected to win: the expected signal lands in a well-covered channel and the solver one-shots it with high confidence. Softballs score as regression checks only — they advance nothing and cap at one per batch. |
| Documented-hard | a blind spot judged out of reach of the current architecture, logged with a named resolution path (a specific reader, API, or data source). Never a silent drop. |
| Quiet-on-healthy gate | a new or changed detector ships only if it emits nothing on every control in the corpus. A detector that lights on healthy subjects is a regression, whatever it catches. |

## Fit test

Run only when all four hold; otherwise say which fails and stop:

1. **An instrument exists** and its output, not the subject itself, is what the solver will read.
2. **Ground truth is closable** — the orchestrator can know the planted truth exactly (it planted it), and a control exists to diff against.
3. **Breaking is safe** — variants are prepared on copies in a sandbox; the live fixture, and anything it depends on, is never touched (CONFIG § Versioning and archiving discipline).
4. **Re-running is cheap enough to loop** — one match's cost permits dozens of matches; if not, shrink the fixture first.

Self-authored fixtures make this a **smoke-tier** oracle: the same intelligence plants and scores. State that tier honestly in the ledger. Faults sourced from real-world failure reports, and fixtures the user holds private truth for, graduate matches toward ground truth.

## Output

One container, `<inbox>/<instrument>-red-vs-blue/` (CONFIG § Inbox output convention). The **Round Index** — ledger plus resume point — is the only file at the container root and the gate file (CONFIG § Group approval). Everything else sits in `<notes>/`: the fault catalogue, the seed manifest (orchestrator-only, named `_manifest-*`), per-match records, baselines, and scoring tables. Instrument versions live in the container's `instrument/` subfolder, one file per version, never edited in place. Bulk variant data (big binaries, scene copies) lives in the parent project's assets folder; the container links to it. All notes carry `type: note` (`type: index` for the Round Index), `parent`, `reviewed: false`.

## Invocation

| mode | behavior |
| ---- | -------- |
| interactive (`/note-kit-red-vs-blue`) | pause after Calibrate to confirm the noise floor, and after each batch's scoring before hardening. Default in-session. |
| automatic (`--automatic`) | run batches end to end; raise judgment calls to the `<user-queue>` without stopping (CONFIG § Queue protocol). |
| resume | the Round Index is the resume state. Read it, take the first open item: an unplayed variant, an unhardened blind spot, or an unswept regression. |

## Phase 0 — Calibrate

**Input:** the instrument, the fixture, the sandbox path.

1. Copy the fixture to the sandbox **with every dependency it resolves at runtime** — a copy that silently loses its data reads as a sea of phantom faults.
2. **NOOP baseline:** push an untouched copy through the full variant-preparation path (copy, open, save, nothing else) and run the instrument on it. Any delta from the original's output is a **harness artifact** — fix the harness before any match; never score against a contaminated baseline.
3. Record the **noise floor**: every issue, warning, and flag the instrument emits on the control, by channel, with counts. Scoring subtracts this verbatim.
4. Record the **report budget**: the control output's size (tokens or lines, total and per channel). Hardening may not grow it past budget (Phase 5).
5. Where available, add **sibling controls** — other healthy subjects of the same kind — so quiet-on-healthy is proven on a corpus, not one file.

**Output:** `<notes>/calibration-vNNN.md`.

## Phase 1 — Catalogue

Every fault must carry **provenance** (see Definitions) — a real failure someone actually hit, not one the red team finds convenient to plant. The catalogue is what stops the red team from authoring easily-solved blue-team tasks. Build it before authoring anything:

1. Mine the three admissible classes in order. **User-reported:** the user's own corrections, live-confirmation findings, and complaints in session logs (`mcp__vault__vault_search`). **Ledger:** this instrument's blind-spot ledgers and documented-hard lists, plus those of kindred instruments in the vault. **Community:** official issue trackers, forum threads with confirmed resolutions, practitioner threads about similar tools or scripts — each entry cites the post or report it came from.
2. Per entry: the cited source, the mechanism, the **symptom as the reporter described it** (a competent human's words, not the red team's), what the instrument *should* surface, and a difficulty guess (visible / subtle / architectural).
3. **Expected-catch pre-screen:** per entry, record whether the *current* instrument should already surface it, judged against its channel list. `expected-catch` entries are regression material, not blind-spot probes; a batch is drawn majority from `expected-blind` and `unknown` entries.
4. Mark entries the current instrument architecture plausibly cannot reach — candidate documented-hards, kept in the battery anyway: the point is to find the wall, not to avoid it.

**Constraint:** no invented faults. An unprovenanced fault idea, however plausible, goes into the catalogue as a *lead* to source, not into the battery. **Output:** `<notes>/fault-catalogue-vNNN.md`.

## Phase 2 — Author (red)

1. One catalogue entry per variant, **one fault per variant** in early batches. Prepare each in the sandbox via the calibrated path; admit it to the battery only when it **reproduces the reported symptom** — re-read the broken site *and* observe the behavior the provenance source described (the grey render, the silent no-output, the wrong count). A change that landed but produces no symptom is not the catalogued fault; rework it or drop it. An unlanded fault scores nothing and wastes a match.
2. Batch composition is majority `expected-blind`/`unknown`; at most one `expected-catch` regression check per batch. The red team's job is to lose the instrument, not to feed it.
3. Append each variant to the **seed manifest**: site, mechanism, expected signal, provenance citation, pre-screen class. The manifest stays orchestrator-only.
4. Mix in **healthy decoys** at an undisclosed rate (one per three to five faulted variants). The manifest records them as decoys; nothing else distinguishes them.
5. Batch size three to five: small enough to harden between batches, large enough to amortize setup.

## Phase 3 — Match (blue)

Run the instrument on each variant headless and isolated — never against a live session the user is holding open. Then one blind solver per variant, fresh context, top line model (CONFIG § Sub-agent execution). The solver gets the report and the bare framing below — **nothing else**: no mention of a test, a round, prior matches, decoys, or what kind of answer is expected. Telling a blind agent what it does not know is a leak like any other.

> Something might be wrong in the subject this report describes. If so, identify it and propose a fix.
> Report: [ATTACH THE INSTRUMENT OUTPUT / LENS DIGEST — never the manifest, never the control]
> Tools: [READ-ONLY TOOLS FOR THE REPORT FILES ONLY — name them explicitly (`Read`, `Grep`); you inherit no MCP context. Do not open the subject itself.]
>
> 1. Read the report in full.
> 2. Commit a verdict: **healthy**, or **faulty** with the site, the mechanism, your confidence (high / medium / low), and a minimal proposed fix.
> 3. Quote the report lines your verdict rests on. If your verdict is healthy, name the checks that came back clean.
> 4. Output the verdict block only; no narration.

The solver's **first committed verdict is the scored verdict**. A nudged re-read ("look again, assume something is wrong") may follow for telemetry — it measures whether the signal was present-but-unprioritized — but never changes the score.

## Phase 4 — Score

Orchestrator only, manifest in hand, noise floor subtracted:

1. Per faulted variant: did the **raw** output carry the signal? Did the **lens** keep it? Did the **solver** act on it? Charge any miss to the failing layer (layer attribution). Verdicts: **surfaced** / **surfaced-but-noisy** / **blind** per layer; **found** / **partial** / **missed** for the solver.
2. **Softball check:** a one-pass, high-confidence solver win on an `expected-catch` fault is recorded `softball` — it counts as a regression check, never toward coverage, hardening pressure, or escalation-rung progress. A batch that comes back mostly softballs is a red-team failure: re-author from `expected-blind` entries before playing on.
3. Per decoy: **clean-pass** (solver said healthy) or **false-alarm** (named a fault) — and when a false alarm traces to an instrument channel lighting on healthy input, that channel fails the quiet-on-healthy gate and is itself a finding.
4. Per proposed fix, when applied: **fix-verified** (re-run matches the control) or **fix-reasoned** (applied, effect argued not observed). Never report fix-reasoned as verified (the calibrated-confidence rule).
5. Write one match record per variant to `<notes>/matches/`; update the Round Index ledger row.

## Phase 5 — Harden

For each blind spot, charged layer by charged layer:

1. **Raw-layer miss** → a new or extended detector in the instrument. **Lens-layer miss** → a lens fix; the instrument is innocent, do not touch it. **Solver-layer miss** → a framing or report-ordering fix, or accept as solver variance.
2. Version the instrument — copy to the next `vNNN` in `instrument/`, never edit the prior version (CONFIG § Numbering).
3. Gates, all three before the version is used in play: **quiet-on-healthy** (nothing new on any control in the corpus) · **catch** (the motivating variant now surfaces) · **budget** (control-output size within the Phase-0 budget; a detector that buys one catch with a flood of new tokens is rejected as written).
4. **Regression sweep:** re-run the new version over every prior variant and diff verdicts against the ledger. A formerly-caught fault now missed blocks the version.
5. A blind spot the architecture cannot reach → **documented-hard**, with its named resolution path, in the Round Index. After 2 hardening attempts on one blind spot, stop and classify (CONFIG § Loop budget); raise genuine judgment calls to the `<user-queue>`.

## Phase 6 — Escalate (the test fights back)

A passing battery never ends the round — it triggers authorship. When every existing trap passes (a full batch with no new blind spot, or the loop clearing quickly), the battery — not the instrument — is what improves next, with **new variants hunting vulnerabilities the battery has never probed**, on two standing targets:

- **The instrument's newest surface.** Every detector and code path added since the last batch is the least-tested part of the instrument; author variants aimed squarely at what just changed — faults the new collector should catch but at its edges, inputs that make it false-alarm, and faults its addition may have masked.
- **Fault classes absent from the catalogue.** A fresh Phase-1 catalogue pass (new sources, new failure-mode families) before each escalation batch, so new traps come from documented failures, not recycled ones.

Climb one rung per quiet batch, and aim each rung at the instrument's **best-performing channels**, because that is where unearned confidence lives:

1. **Subtle singles** — same fault classes, weaker symptoms (smaller deltas, partial divergence, the fault one node off the active path).
2. **Stacked variants** — two to five faults per variant, including pairs chosen to mask each other and pairs co-located on one site. Score completeness: every planted fault must surface, not just one.
3. **Found faults** — break the fixture the way a *different* authority describes (a fresh catalogue pass), or import a genuinely broken subject nobody prepared and let the user hold the truth.
4. **New fixture** — a different healthy subject of the same kind (new noise floor, Phase 0 re-run). Detectors tuned to one fixture's floor get exposed here.
5. **Battery mutation** — programmatic perturbation of the fixture (random parameter, wiring, or reference damage) with an automatic external oracle where one exists (output diff against control). Mutation output never enters scored matches directly — it is a blind-spot *scout*, exempt from provenance because no solver plays it. A miss it finds becomes a catalogue lead; it joins the battery only once sourced to a real reported failure, or explicitly ledgered as `mutation-found` with the user's sign-off.

Exit the loop when a full batch at the highest reached rung yields no raw- or lens-layer blind spot, or the user calls time. The instrument's tip version, the ledger, and the documented-hard list are the round's payload.

## Round Index (gate file)

Maintained continuously; the resume point and the user's one read:

```
# Round Index — <instrument> vs <fixture>
[oracle tier: smoke / sourced / user-held]   [instrument: vNNN → vNNN]
## Noise floor        [from calibration, verbatim]
## Ledger             | variant | fault (manifest) | provenance | pre-screen | raw | lens | solver | decoy/softball | fix | instrument bump |
## Blind spots found  [numbered; each → the detector or lens fix that closed it, or documented-hard + resolution path]
## Documented-hard    [the honest wall: what the instrument cannot see and what would change that]
## Escalation rung    [current rung, batches quiet at it]
## Resume point       [first open item]
```

## Honesty rules

The round's value is calibration, and calibration dies of optimism first: report `fix-reasoned` as reasoned, never verified · a leak of manifest content into any blind context voids those matches in the ledger, played again fresh · the nudged re-read never changes a score · a harness artifact discovered mid-round invalidates affected matches explicitly — rescore, do not paper over · the oracle tier (smoke / sourced / user-held) is stated in the Round Index header and never silently upgraded.
