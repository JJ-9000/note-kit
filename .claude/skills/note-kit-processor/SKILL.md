---
name: note-kit-processor
description: Convert information that already exists into kit-compatible atomic notes. Two input modes — migrate a foreign note collection (Notion / Evernote / Roam / Logseq / Obsidian / plain-markdown export) into the kit's types, frontmatter, and naming; or mine one body of written media (a book, article, web page, PDF, long document) into separate atomic vault notes, each carrying its source. This is note-kit-handoff's atomic-extraction applied to material that already exists instead of to a live session. Trigger on "import these notes", "migrate my <system> notes", "convert this export to the vault", "extract notes from this book/article/PDF", "atomize this source", "make vault notes from this". NOT a single readable transcript (note-kit-transcription), NOT a YouTube link (note-kit-youtube-to-note), NOT a research question to investigate (note-kit-research), NOT a live-session wrap-up (note-kit-handoff).
---

# note-kit-processor

Existing information in, kit-compatible atomic notes out. The input is static and external — a foreign note pile, a book, a page. Decompose it into atomic notes that satisfy the kit's contract, deduped against what the vault already holds, each carrying the source it came from.

**A note earns its place by being useful.** Emit an atomic note only because a future reader would retrieve it — not to record that the source contained it. The relevance gate (§2) and the dedup gate (§3) both enforce this.

## Modes

Detected from the input, not from a flag. Both converge on the same emit stage.

| mode    | input                                                            | the work                                                               |
| ------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------- |
| migrate | a collection of existing notes — an export folder, another vault | reshape each note into kit format; split compound notes into atoms     |
| extract | one body of media — a book, article, web page, PDF, long text    | mine the individual ideas relevant to this vault, each as its own note |

## 1 — Intake & survey

Get the full text, then read the whole of it before writing a single note — structure emerges from the whole, as in note-kit-transcription.

| source                         | how to read it                                                                                                                                                                                                            |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| web page / URL                 | a web-clean / markdown-extraction skill if one is installed (clean markdown, no chrome or nav); else read the source directly                                                                                             |
| PDF                            | a PDF-extraction skill if one is installed; else read the source directly                                                                                                                                                 |
| EPUB / book text / pasted text | read in full                                                                                                                                                                                                              |
| foreign note export            | enumerate the files; detect the origin format — Notion (per-note `.md` + CSV props), Evernote (`.enex`), Roam/Logseq (JSON or `.md`), Apple Notes (HTML/text), plain markdown, older Note-Kit version, Old Obsidian vault |

Capture provenance up front — the bibliographic anchor every emitted note will cite: author, title, publisher or site, date, URL or ISBN. For migration also capture, per note, the origin system, the original path, the created date, and the original tags. For a large input, this survey decides the chunking for fan-out.

## 2 — Decompose into candidate atoms

An atom is one self-contained, context-free, reusable idea — the smallest claim that is independently true, reusable, and linkable. Apply these six tests in order and act on the first that fails (the same six note-kit-handoff applies, so both skills recognize a vault-worthy note the same way):

1. **One concept (the "and" test).** A claim needing an "and" or "; additionally" to be accurate is two atoms — split.
2. **Independent reusability (the borrow test).** Two facts different future notes would borrow are separate atoms; a fact only useful beside another merges in.
3. **Context-independence (the survival test).** Stripped of its source, the note still asserts something true and usable, or it is not emitted.
4. **Link, don't embed (the single-source test).** A reusable sibling fact is `[[wikilink]]`ed, never restated; a shared fact lives in one note.
5. **Evergreen, not event (the tense test).** An atom states how something *is*, not what happened in the source. Canonical truth.
6. **Done (the subtraction test).** Removing any sentence loses a reusable fact, and no second independent claim remains to split off.

The two failure modes: too **big** (compound) splits; too **small or too tied** is not emitted.

- **migrate** — a foreign note is either already atomic (convert 1:1) or compound (split into N atoms by the same tests). The original is kept intact in Stage 4, so the split stays reversible.
- **extract** — walk the material and pull each idea, definition, procedure, claim, or fact relevant to this vault's domains. Relevance gate: skip the narrative filler the vault would never retrieve; atomize the parts that earn a note, not the whole book.

Each candidate is the idea — concise and faithful — plus its precise source anchor: page, chapter, section heading, URL fragment, or original file path.

## 3 — Classify & dedup

Assign `type` per CONFIG's Types table:

| the atom is                                                | type      |
| ---------------------------------------------------------- | --------- |
| evergreen knowledge, true with or without its context      | reference |
| runnable code, ready to paste                              | snippet   |
| a procedure or fact that isn't quite evergreen             | note      |
| a spark or proposal, not yet established                   | idea      |
| the preserved original, transcript, cited file or document | source    |

Default to `note` when torn (matching note-kit-transcription).

**Dedup against the vault before emitting.** `mcp__vault__vault_search(<the atom's gist>)`; if the tool is unavailable (no daemon or `.mcp.json`), fall back to Glob/Grep over the candidate destination folders for the atom's key terms — softer, but it catches the obvious duplicates. A match above 0.85 that genuinely covers the atom → do not duplicate: emit a `type: addendum` targeting the existing note so the new angle merges in, or — if the atom adds nothing — record it in the index as "already covered by [[X]]" and emit no file. Re-importing knowledge the vault already holds is the failure this stage exists to prevent. Do not bloat existing references with context, process explanation, or session-specific information.

**Resolve links by search, never by guess.** `parent` / `project` come from a vault_search for the owning note; a single confident match → set it; ambiguous or none → leave the wikilink blank for the downstream agents. Never write an unresolvable reference.

## 4 — Emit into one container

Container folder `<inbox>/<source-slug>-note-kit-processor/`. The folder is the unit of review.

- **One atom, one file.** The naming pattern for its type (CONFIG § Types), full inbox frontmatter (`type`, `tags`, `date`, `reviewed: false`, `status: draft`, plus `parent`/`project` where resolved), and a **`## References` section at the bottom** carrying the citation — full source plus the precise anchor. The citation lives in that bottom section, never as a loose inline provenance line in the body. One source → one reference entry; an atom drawing on two passages of the same work lists both anchors under the one source.
- **Preserve the original.** migrate → keep each original foreign note as a `type: source` file under the container's `<sources>/` subfolder, carrying its original title, created date, and tags in frontmatter; the atoms derived from it cite it. extract → the source itself stays outside the vault; its bibliographic anchor lives in the index. Nothing the user holds is moved or destroyed.
- **Index at the container root**, `type: index`: a provenance block (the full citation of what was processed) followed by a table of every emitted atom — wikilink, type, one-line gist, source anchor, and dedup status (`new` / `merged into [[X]]` / `skipped-duplicate`). The index is the manifest that makes the folder reviewable as a unit; the typed atoms route out of the container only when the filing-agent moves the reviewed files to their homes.

## 5 — Compatibility report

The index closes with what the user must touch before the batch is vault-clean:

- **Counts** — atoms emitted by type; how many merged into existing notes or were skipped as duplicates.
- **Unresolved links** — every atom whose `parent`/`project` was left blank, for the user to set or the filing-agent to infer.
- **Flagged uncertainty** — garbled passages, uncertain proper nouns, claims the source stated ambiguously: flagged, never filled.
- **Unmapped metadata** — foreign fields with no kit equivalent, parked in the source note's frontmatter rather than dropped.

## Fidelity

- **Never fabricate.** An ambiguous, truncated, or garbled source gets flagged inline and left — do not fill the gap. Flag uncertain proper nouns for the user to confirm before they harden into the record.
- **Preserve specifics verbatim** — names, numbers, dates, quoted lines, technical terms. "Tuesday" does not become "earlier that week."
- **Re-express for atomicity where it helps** — every atom stays faithful to the source's claim; quote directly where precision carries the meaning.
- **Cite the source, not the errand.** The `## References` section names the authoritative origin (the work, its author, and the anchor) — never the conversation that asked for the import. The note stands on its own; absolutely no inline changelog, process, or irrelevant context.

## Large inputs — sub-agent fan-out

A long book or a many-note migration fans out — one sub-agent per chapter or per batch of foreign notes, top line model (CONFIG § Sub-agent execution), each running Stages 2–3 on its slice and returning candidate atoms; the main context dedups across slices and emits. Sub-agents inherit no tools: the spawning prompt must name `mcp__vault__vault_search` for the dedup lookups and state the origin format for the slice. Running inside a sub-agent already (no nested spawn) → run the slices serially in this context.

## Worked example

One input — a chapter of *Designing Data-Intensive Applications* pasted in, so **extract mode**.

1. **Intake & survey (§1).** Read the whole chapter first. Capture the bibliographic anchor: *Designing Data-Intensive Applications*, Kleppmann, O'Reilly 2017, ch. 7. The survey finds three vault-relevant ideas; the rest is narrative the relevance gate drops.
2. **Decompose (§2).** Run the atomicity tests. "Read committed prevents dirty reads" and "snapshot isolation uses MVCC" each pass the "and" and survival tests as separate atoms. The deadlock-detection point fails the survival test on its own — it only means something attached to two-phase locking — so it is held as a detail to attach, not a standalone atom.
3. **Classify & dedup (§3).** All three are evergreen → `type: reference`. `mcp__vault__vault_search("two-phase locking deadlock detection")` returns [[Two-Phase-Locking]] above 0.85 — the vault already covers that idea, so the deadlock detail becomes a `type: addendum` targeting that note. The other two return no match → emit as new.
4. **Emit (§4).** Container `<inbox>/Ddia-Ch7-Transactions-note-kit-processor/`:
   - `Read-Committed-Prevents-Dirty-Reads.md` — `type: reference`, the claim in three sentences, then a `## References` section citing *Designing Data-Intensive Applications*, ch. 7, p. 234.
   - `Snapshot-Isolation-Uses-MVCC.md` — `type: reference`, `## References` → ch. 7, p. 239.
   - The addendum file targets [[Two-Phase-Locking]], `## References` → ch. 7, p. 257.
   - `Ddia-Ch7-Transactions-Index.md` — `type: index`, a provenance block with the full citation, then this manifest table:

   | note                                    | type                          | gist                                   | source        | status                  |
   | --------------------------------------- | ----------------------------- | -------------------------------------- | ------------- | ----------------------- |
   | [[Read-Committed-Prevents-Dirty-Reads]] | reference                     | dirty reads, and how RC blocks them    | ch. 7, p. 234 | new                     |
   | [[Snapshot-Isolation-Uses-MVCC]]        | reference                     | SI keeps several committed versions    | ch. 7, p. 239 | new                     |
   | —                                       | addendum → [[Two-Phase-Locking]] | adds the deadlock-detection detail     | ch. 7, p. 257 | merged into [[Two-Phase-Locking]] |

5. **Compatibility report + summary (§5).** Close the index with counts (2 new reference, 1 merged) and any blank `parent` links or flagged uncertainties. Then post the chat summary: the container path, atom count by type, the merged/skipped count, and anything the user should resolve before the filing-agent runs.
