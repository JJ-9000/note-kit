---
name: note-kit-processor
description: Convert information that already exists into kit-compatible atomic notes. Two input modes — migrate a foreign note collection (Notion / Evernote / Roam / Logseq / Obsidian / plain-markdown export) into the kit's types, frontmatter, and naming; or mine one body of written media (a book, article, web page, PDF, long document) into separate atomic vault notes, each carrying its source. This is note-kit-handoff's atomic-extraction applied to material that already exists instead of to a live session. Trigger on "import these notes", "migrate my <system> notes", "convert this export to the vault", "extract notes from this book/article/PDF", "atomize this source", "make vault notes from this". NOT a single readable transcript (note-kit-transcription), NOT a YouTube link (note-kit-youtube-to-note), NOT a research question to investigate (note-kit-research), NOT a live-session wrap-up (note-kit-handoff).
---

# note-kit-processor

Existing information in, kit-compatible atomic notes out. The input is static and external — a foreign note pile, a book, a page. Decompose it into atomic notes that satisfy the kit's contract, deduped against what the vault already holds, each carrying the source it came from.

**A note earns its place by being useful.** Emit an atomic note only because a future reader would retrieve it.

## Modes

Detected from the input, not from a flag. Both converge on the same emit stage.

| mode    | input                                                            | the work                                                               |
| ------- | ---------------------------------------------------------------- | ----------------------------------------------------------------------- |
| migrate | a collection of existing notes — an export folder, another vault | reshape each note into kit format; split compound notes into atoms     |
| extract | one body of media — a book, article, web page, PDF, long text    | mine the individual ideas relevant to this vault, each as its own note |

## 1 — Intake & survey

Get the full text, then read the whole of it before writing a single note.

| source                         | how to read it                                                                                                                                                                                                            |
| ------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| web page / URL                 | a web-clean / markdown-extraction skill if one is installed (clean markdown, no chrome or nav); else read the source directly                                                                                             |
| PDF                            | a PDF-extraction skill if one is installed; else read the source directly                                                                                                                                                 |
| EPUB / book text / pasted text | read in full                                                                                                                                                                                                              |
| foreign note export            | enumerate the files; detect the origin format — Notion (per-note `.md` + CSV props), Evernote (`.enex`), Roam/Logseq (JSON or `.md`), Apple Notes (HTML/text), plain markdown, older Note-Kit version, Old Obsidian vault |

Capture provenance up front — the bibliographic anchor every emitted note will cite: author, title, publisher or site, date, URL or ISBN. For migration also capture, per note, the origin system, the original path, the created date, and the original tags. For a large input, this survey decides the chunking for fan-out.

## 2 — Decompose into candidate atoms

An atom is one self-contained, context-free, reusable idea — the smallest claim that is independently true, reusable, and linkable. Apply the six atomicity tests in order and act on the first that fails (CONFIG § Atomicity tests).

The two failure modes resolve to an emit decision: too **big** (compound) splits; too **small or too tied** is not emitted.

- **migrate** — a foreign note is either already atomic (convert 1:1) or compound (split into N atoms by the same tests). The original is kept intact in Stage 4.
- **extract** — walk the material and pull each idea, definition, procedure, claim, or fact relevant to this vault's domains. Relevance gate: skip the narrative filler the vault would never retrieve; atomize the parts that earn a note, not the whole book.

Each candidate is the idea — concise and faithful — plus its precise source anchor: page, chapter, section heading, URL fragment, or original file path.

## 3 — Classify & dedup

Assign `type` per CONFIG § Types:

| the atom is                                                | type      |
| ---------------------------------------------------------- | --------- |
| evergreen knowledge, true with or without its context      | reference |
| runnable code, ready to paste                              | snippet   |
| a procedure or fact that isn't quite evergreen             | note      |
| a spark or proposal, not yet established                   | idea      |
| the preserved original, transcript, cited file or document | source    |

Default to `note` when torn.

**Dedup against the vault before emitting.** `mcp__vault__vault_search(<the atom's gist>)`; if the tool is unavailable, fall back to Glob/Grep over the candidate destination folders for the atom's key terms. A match above 0.85 that genuinely covers the atom → do not duplicate: emit a `type: addendum` targeting the existing note so the new information or correction merges in, or — if the atom adds no new information — record it in the run report (§5) as "already covered by [[X]]" and emit no file. `type: reference` notes and `standard`-tagged notes stay generic; keep session context out.

**Resolve `parent` / `project` by vault_search for the owning note** (CONFIG § Rules: stamp only a link that resolves).

## 4 — Emit into one container

Container folder per the processor row in CONFIG § Skill slugs. The folder is the unit of review.

- **One atom, one file.** The naming pattern for its type (CONFIG § Types), full inbox frontmatter (`type`, `tags`, `date`, `reviewed: false`, `status: draft`, plus `parent`/`project` where resolved), and a **`## References` section at the bottom** carrying the citation — full source plus the precise anchor. The citation lives in that bottom section, never as a loose inline provenance line in the body. One source → one reference entry; an atom drawing on two passages of the same work lists both anchors under the one source.
- **Preserve the original.** migrate → keep each original foreign note as a `type: source` file under the container's `<sources>/` subfolder, carrying its original title, created date, and tags in frontmatter; the atoms derived from it cite it. extract → the source itself stays outside the vault; each atom cites it in its own `## References` section. Nothing the user holds is moved or destroyed.
- **Gate file at the container root** — `<source-slug>-Index.md`, `type: index`, `reviewed: false`. It is the container's table of every emitted atom — existing wikilink, type, one-line gist. It briefly cites and describes the input source, and concisely maps the ingestion. It lists only what the batch holds without metacontextual information about the run, reserved for `<logs>` (CONFIG § Rules: artifacts stand alone). The index is the gate; the container's single approval point — **every file, atoms and gate alike, is emitted `reviewed: false`.** (CONFIG § Group approval, § Inbox output convention).

## 5 — Report the run

Silently deliver the ingested content, and report the run in `<logs>` (CONFIG § Rules: artifacts stand alone).

- **Counts** — atoms emitted by type; how many merged into existing notes or were skipped as duplicates.
- **Flagged uncertainty** — garbled passages, uncertain proper nouns, ambiguously stated claims; each is flagged inline on its own atom (§ Fidelity) and named here so the user can confirm it.
- **Unmapped metadata** — foreign fields with no kit equivalent, parked in the source note's frontmatter rather than dropped.

## Fidelity

- **Never fabricate.** An ambiguous, truncated, or garbled source gets flagged inline and left — do not fill the gap. Flag uncertain proper nouns for the user to confirm before they harden into the record.
- **Preserve specifics verbatim** — names, numbers, dates, quoted lines, technical terms. "Tuesday" does not become "earlier that week."
- **Re-express for atomicity where it helps** — every atom stays faithful to the source's claim; quote directly where precision carries the meaning.
- **Cite the source, not the errand.** The `## References` section names the authoritative origin (the work, its author, and the anchor) — never the conversation that asked for the import (CONFIG § Rules: artifacts stand alone).

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
   - `Ddia-Ch7-Transactions-Index.md` — `type: index`, `reviewed: false`: a table of the atoms the container holds, with a brief citation of the source and a one-line map of the ingestion (the deadlock detail merged onto [[Two-Phase-Locking]], so it is reported in `<logs>`, not listed here):

   | note                                    | type      | gist                                |
   | --------------------------------------- | --------- | ----------------------------------- |
   | [[Read-Committed-Prevents-Dirty-Reads]] | reference | dirty reads, and how RC blocks them |
   | [[Snapshot-Isolation-Uses-MVCC]]        | reference | SI keeps several committed versions |

5. **Report the run (§5).** Deliver the references silently; report the run — container path, 2 new references, the deadlock detail merged into [[Two-Phase-Locking]], any flagged uncertainty — in `<logs>`, never inside the gate file.
