---
name: note-kit-transcription
description: Turn an existing block of text into a clean, digestible note that preserves the source's own words — meeting and call transcripts, pasted chat logs, voice-memo dumps, raw brain-dumps. Reads the whole block, clusters it under headers drawn from the source's own language, keeps each speaker's exact phrasing, and surfaces decisions, action items, and open questions only where the text contains them. Use whenever someone pastes or points at a chunk of text and wants it cleaned up, captured, structured, or made readable. Trigger on "transcribe this", "meeting notes", "notes from this", "clean up these notes", "make notes from this", "digest this", "turn this into notes", "transcription mode on". NOT a YouTube link (note-kit-youtube-to-note), a web page (defuddle), or an end-of-session log (note-kit-handoff).
---

# transcription

Text in, digestible note out. The input already exists; the job is transcription. The output preserves the source's vocabulary and the directive or emotion inside it, clustered.

## 1 — Read the whole block first

Read it all before writing a word. While reading, work out under the hood — none of this appears in the output:

- **Voices.** One or several? Name prefixes (`<name>:`), turn-taking, register shifts — a single-voice dump and a multi-party meeting get attributed differently.
- **Semantic clusters.** Group by meaning. Tag each passage with the topic it is *about*, merge passages sharing a topic even when minutes apart, name each cluster from a phrase the source itself used, and keep a cluster only if it survives the register pass — a pure-throwaway topic dissolves to nothing. These clusters become the sections.
- **Tone registers.** Tag each segment silently per the table below.

If the block is truncated or garbled, say so, tag the uncertainty inline, and move on. Do not invent the missing parts.

## 2 — Tone registers

Tag silently while reading. Read-side tags drive how much space and fidelity a segment gets; output-side tags name registers the note must never produce.

| Register                  | Operational features                                                                                                                                                                       | Treatment                                                                                                                                                    |
| ------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Emphatic (read)           | Point repeated across sentences; intensity words ("absolutely", "the real thing is"); returning to a topic; explicit flags ("listen", "here's the thing", "the key point")                 | HIGH — promote, more space, header candidate, full phrasing. Compress only if explicitly retracted later.                                                    |
| Hedged (read)             | Qualifiers ("maybe", "I think", "not sure"); conditionals ("if that holds"); trailing off; self-contradiction within a thought                                                             | Preserve as-is — never flatten into certainty.                                                                                                               |
| Throwaway (read)          | Tangents the speaker abandons; "anyway", "but whatever"; restating with no new information; social filler                                                                                  | LOW — compress or drop. Keep if it buries a specific (name/date/number) found nowhere else.                                                                  |
| Emphatic-throwaway (read) | Strong statement followed by "but I don't know", "whatever", "doesn't matter"; raising and dropping a topic within seconds                                                                 | FLAG — preserve the emphatic content; note the undercut. Honor "scratch that" / "forget that" retractions.                                                   |
| Specific (read)           | Proper nouns; numbers; dates; quoted speech; named tools/people/places; technical terms used precisely; step-by-step narration                                                             | HIGH — preserve verbatim. Names, numbers, sequences survive exactly. "Tuesday" does not become "earlier this week". Wins over any other tag on preservation. |
| Folksy (read)             | Idioms ("the long and short of it"); everyday-life analogies; loose grammar ("gonna", dropped subjects); understatement; storytelling rhythm                                               | Preserve the register.                                                                                                                                       |
| Cute (read)               | Diminutives; exaggeration for humor; pet names for concepts; rhetorical non-questions ("who does that?"); mock-drama                                                                       | Keep enough to reconstruct tone; trim padding.                                                                                                               |
| Clinical (output)         | Latinate where Anglo-Saxon was used; passive where active was written; nominalizations ("the implementation of" vs "implementing")                                                         | PROHIBITED.                                                                                                                                                  |
| Editorial (output)        | "Importantly", "notably", "it's worth noting"; sentences telling the reader how to feel; causal claims the speaker did not make ("this led to", "consequently" unless they drew that line) | PROHIBITED.                                                                                                                                                  |
| Literary (output)         | Balanced clauses; parallelism for effect; metaphors the speaker did not use; elegant variation; crafted rhythm                                                                             | PROHIBITED.                                                                                                                                                  |

When read-side tags conflict (emphatic + throwaway), the emphatic-throwaway rule applies. When Specific co-occurs with any other tag, Specific wins on preservation.

## 3 — Synthesize: intelligent transcription

The output removes filler and false starts, preserves the speaker's exact vocabulary and sentence structure, groups by semantic cluster, and never editorializes. Every sentence must pass one test:

> **Could a professional transcriptionist have produced this sentence using only the source's own words?**

If no, it does not belong. Three prohibitions:

- **No emotional inference.** If the source did not state a feeling, it does not appear — no "seems frustrated", "underlying tension".
- **No grammar correction.** Non-standard structures stand. Contractions only where the source used them.
- **No fabrication.** Never add content the block lacks. A garbled, `[inaudible]`, or `[crosstalk]` passage gets flagged, not filled. Flag uncertain proper nouns for the user to confirm.

**Attribution.** Single voice → no labels, render in that voice. Multiple voices → attribute where it matters (a decision, a commitment, a disagreement) using the source's own names or handles; ambient agreement need not be. Never merge several speakers into one composite, never invent a name; use the source's label (`Speaker 2`) if that is all there is.

## 4 — Output shape

Body is semantic clusters, each under a **bold header drawn from the source's own language** — not an editorial label. Order by meaning, emphatic clusters floating up; within a cluster, separate sub-thoughts with a **blank line** (or lead each with `→`). One thought per block. Meetings and calls produce three recurring clusters — include each **only when the block contains it**:

- **Decisions** — points the participants settled, in their words, attributed where someone owns the call.
- **Action items** — commitments. Attribute the owner and preserve any deadline verbatim; no owner named → record without one.
- **Open questions** — what was raised and left unresolved, kept as questions in the source's phrasing.

Everything else stays in topic clusters. No editorial TL;DR or headline.

**Frontmatter inference.** A source whose title or frontmatter declares a type keeps it (CONFIG § Types) — a drop titled "Journal" produces a `type: journal` note regardless of topic. Otherwise infer `type` from CONFIG § Types — for a transcript, one of `journal`, `idea`, `note`, or `source`, defaulting to `note` when unsure. Name the file by that type's naming pattern (CONFIG § Types), from the source's own subject — name what it is, no clever title. Set `project:` and the `parent:` backlink only when the transcript's distinctive vocabulary resolves to a single active project; otherwise leave them for the downstream agents. The note lands loose in the `<inbox>` root, no container folder and no version token (CONFIG § Inbox output convention).

## Focus mode

Trigger "focus this" / "action items only" — a second pass that converts a transcribed note into the version you would paste to a colleague; it does not replace the note. Extract the implied action from each cluster (every bullet traces to the source — no invented actions), as a flat bullet list with no headers and no frontmatter; strip transcript voice but keep domain terms exact ("tighten displacement noise", not "I think maybe we should tighten the noise"); keep owners and deadlines attached ("B — send the cut by Thursday"); turn open questions into requests; never editorialize priority or soften a directive. Format: `* [action or observation] — [brief context if needed]`.

## Worked example

**Input block (pasted):**

> A: ok so the skybox loop, I think the seam at frame 240 is the blocker.
> B: yeah it pops. We talked about a crossfade but honestly I'm not sure that fixes the cloud drift, might just smear it.
> A: right. Let's just re-time the last 30 frames to ease back into frame 0, that's cleaner.
> B: works. I'll re-render tonight, should have it by tomorrow AM.
> A: and we still need to decide on the resolution bump but whatever, that's later.
> B: anyway the VDB is 0.5 voxels right now.

**Output note body:**

> **Seam at frame 240**
>
> A: the seam at frame 240 is the blocker. B: it pops.
>
> Crossfade was discussed — B is not sure it fixes the cloud drift, *might just smear it*.
>
> **Decisions**
>
> Re-time the last 30 frames to ease back into frame 0 instead of a crossfade. A's call, "that's cleaner." B: works.
>
> **Action items**
>
> → B — re-render tonight, expects it by tomorrow AM.
>
> **Open questions**
>
> Resolution bump still undecided. Flagged as later.
>
> VDB is 0.5 voxel right now.

**Focus-mode version of the same block:**

> * Re-time last 30 frames to ease into frame 0 — chosen over crossfade (crossfade may smear cloud drift)
> * B — re-render tonight, deliver by tomorrow AM
> * Decide resolution bump (deferred) — current VDB is 0.5 voxel
