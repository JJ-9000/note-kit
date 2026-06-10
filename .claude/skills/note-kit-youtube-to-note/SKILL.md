---
name: note-kit-youtube-to-note
description: Turn a YouTube link into a cited vault note built from its captions — lectures, talks, conference sessions, tutorials, interviews. The link alone is enough; pull captions, metadata, and chapters via yt-dlp (never WebFetch — a watch URL returns only the page title) and never ask the user for a transcript. Trigger on a bare YouTube link plus any capture intent ("make a note from this", "summarize this talk", "add this lecture to the vault", "convert this into a reference").
---

# youtube-to-note

Capture a YouTube video as a cited note from its captions. The whole run starts from the link — fetch the captions, never ask for them.

## 1 — Pull captions, metadata, chapters

`yt-dlp` runs as a Python module (`python -m yt_dlp`); if a run reports it missing, `python -m pip install -U yt-dlp` and retry. Extract the 11-character video id (the `v=` param or the `youtu.be/` path segment), then:

```powershell
$id = "<VIDEO_ID>"
$tmp = "$env:TEMP\yt-$id"
New-Item -ItemType Directory -Force -Path $tmp | Out-Null
python -m yt_dlp --skip-download --write-auto-sub --write-sub --sub-lang en `
  --sub-format vtt --write-info-json -o "$tmp/%(id)s.%(ext)s" "<URL>"
```

`--write-sub` grabs human captions where they exist (far cleaner); `--write-auto-sub` is the auto fallback. The "No JS runtime" warning is harmless — captions and the info JSON still download.

## 2 — Dedup and bucket by chapter

YouTube captions are a scrolling window that triplicates text. `dedup_vtt.py` removes the overlap, buckets the words into the video's chapters, and writes `transcript_clean.md`:

```powershell
python "<SKILL_DIR>/scripts/dedup_vtt.py" --dir "$tmp"
```

Its stdout is an ASCII-safe JSON summary — `title`, `channel`, `duration`, `upload_date`, `webpage_url`, the chapter list, `word_count`, `description`. Build frontmatter and the citation block from that JSON; read `transcript_clean.md` for the prose. Do not re-implement the dedup — the script is the canonical implementation and routes around Windows console Unicode crashes by writing prose to the file and keeping stdout ASCII-only.

## 3 — Type and frontmatter

The output is always `type: source` (CONFIG § Types). Required frontmatter is the global `type`, `tags`, `date` plus `source`'s `parent`; set `parent` to the active project the video feeds when its vocabulary resolves to one, otherwise leave it for the downstream agents. Stamp `parent:` only after confirming the project exists — one vault search or project listing check; no match → leave the field empty, never invent a name. Add `source_url`, `source_channel`, `speaker`, `event`, `duration`. Filename `Title-Case-Hyphens.md` (CONFIG § Types).

**Output is a container** (CONFIG § Inbox output convention), matching research and verify-claims: `<inbox>/<Title>-youtube-to-note/`, the cited human-facing note at the root as the **gate file**, transient working material in `<notes>/`. Approving the gate note auto-approves the rest (CONFIG § Group approval), and the filing-agent files the container as a unit.

## 4 — Write the note

First write the raw transcript to the container's `<notes>/` sub-folder: copy `transcript_clean.md` verbatim — the un-corrected, pre-digest captions, no scrub, no distillation — with its own `type: source` frontmatter, `parent` pointing at the gate note. It is kept once and only here, the un-corrected source of truth.

Then hand the deduped captions to the **note-kit-transcription** skill for the chapter-by-chapter digest — preserve the speaker's words, cluster by meaning, never editorialize. The gate note carries the digest, never a re-paste of the raw transcript; around that digest, this skill adds:

1. **Attribution callout** under the H1 — credit the author and event, name the channel, link the URL, state these are derivative notes from published captions for personal study; all ideas belong to the author.
2. **Abstract** — the official description verbatim from the JSON summary if present.
3. **Distillation** — the structured knowledge that makes the source usable, leading the note: a TL;DR, a tools/claims table where it applies, key technical claims as bullets, a workflow/steps list.
4. **Chapter-by-chapter digest** — the digested prose from the transcription pass, chapter timestamps as the section spine. Keep every chapter. No chapters → infer structure from the content.
5. **Citation block** at the end — a paste-ready attribution line (author, title, event, channel, date, URL).

## Gotchas

- **Auto-captions mangle proper nouns** spoken fast ("GSOPs" → "G subs", "Natsura" → "Nat Sura"). Flag uncertain proper nouns for the user to confirm rather than guessing; once confirmed, scrub every occurrence in the gate note's digest so it reads as if the captions were right all along — the `<notes>/` transcript stays un-corrected.
- The download log line "Downloading subtitles: en" (vs "automatic captions") tells you which you got. Human subs barely need the dedup; auto-subs need it badly.
- Don't fabricate content the captions lack. A `[music]` stretch with no speech gets said, not invented.
- The temp dir under `$env:TEMP` is outside the vault — leave it for the user; never write yt-dlp's scratch (the VTTs, the info JSON) into the vault. The only vault writes are the gate note and the transcript copy in `<notes>/`.

After writing, tell the user the container path and surface any flagged proper-noun uncertainties so they can confirm before the guesses harden into the record.
