#!/usr/bin/env python3
"""Dedup a YouTube VTT caption file and bucket it into the video's chapters.

YouTube auto-captions are a scrolling two-line window: each spoken phrase is
emitted many times as it "types in" and scrolls up, so a raw VTT triplicates
its text. This script removes that overlap, buckets the surviving words into the
chapters from the .info.json, and writes `transcript_clean.md` next to the VTT.

stdout is an ASCII-safe JSON summary (title, channel, duration, upload_date,
webpage_url, chapters, word_count, description). Prose is written to the file,
never to stdout, to dodge Windows console Unicode crashes.

Usage:
    python dedup_vtt.py --dir <folder containing one *.vtt and one *.info.json>
"""

import argparse
import glob
import json
import os
import re
import sys

TS_RE = re.compile(r"^(\d{2}):(\d{2}):(\d{2})\.(\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})\.(\d{3})")
TAG_RE = re.compile(r"<[^>]+>")
INLINE_TS_RE = re.compile(r"<\d{2}:\d{2}:\d{2}\.\d{3}>")


def to_seconds(h, m, s, ms):
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def fmt_ts(total_seconds):
    total = int(total_seconds)
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def parse_cues(vtt_text):
    """Return [(start_seconds, text)] for each cue, tags stripped, blanks dropped."""
    cues = []
    cur_start = None
    cur_lines = []

    def flush():
        nonlocal cur_start, cur_lines
        if cur_start is not None and cur_lines:
            text = " ".join(cur_lines).strip()
            text = re.sub(r"\s+", " ", text)
            if text:
                cues.append((cur_start, text))
        cur_start = None
        cur_lines = []

    for raw in vtt_text.splitlines():
        line = raw.rstrip("\n")
        m = TS_RE.match(line.strip())
        if m:
            flush()
            cur_start = to_seconds(*m.groups()[:4])
            continue
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if stripped.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        if stripped.isdigit():  # sequence number
            continue
        # strip inline <timestamp> and <c> tags
        cleaned = TAG_RE.sub("", line)
        cleaned = cleaned.strip()
        if cleaned:
            cur_lines.append(cleaned)
    flush()
    return cues


def dedup(cues):
    """Collapse the scrolling-window duplication into a clean (start, text) list.

    Handles the dominant YouTube patterns:
      - exact consecutive duplicates (finalized line emitted repeatedly)
      - growth ("hello", "hello there", "hello there world") -> keep longest
      - the two-line scroll, where a line reappears as the head of the next cue
        -> append only the non-overlapping tail.
    """
    out = []  # list of [start, text]
    for start, text in cues:
        if not out:
            out.append([start, text])
            continue
        prev = out[-1][1]
        if text == prev:
            continue
        if text.startswith(prev):  # the line grew word-by-word
            out[-1][1] = text
            continue
        if prev.startswith(text):  # shrank / partial repeat
            continue
        # suffix/prefix overlap from the scroll: prev ends with the head of text
        pw = prev.split()
        tw = text.split()
        max_ov = min(len(pw), len(tw))
        overlap = 0
        for k in range(max_ov, 0, -1):
            if pw[-k:] == tw[:k]:
                overlap = k
                break
        if overlap:
            tail = tw[overlap:]
            if tail:
                out.append([start, " ".join(tail)])
            continue
        out.append([start, text])
    return out


def bucket_by_chapter(segments, chapters):
    """Group (start, text) segments under chapters. Returns [(title, start, text)]."""
    if not chapters:
        joined = " ".join(t for _, t in segments).strip()
        return [("Transcript", 0.0, joined)]

    bounds = []
    for ch in chapters:
        bounds.append((float(ch.get("start_time", 0.0)), ch.get("title", "Chapter")))
    bounds.sort(key=lambda b: b[0])

    result = []
    for i, (start_time, title) in enumerate(bounds):
        end_time = bounds[i + 1][0] if i + 1 < len(bounds) else float("inf")
        words = [t for s, t in segments if start_time <= s < end_time]
        result.append((title, start_time, " ".join(words).strip()))
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    args = ap.parse_args()

    vtts = sorted(glob.glob(os.path.join(args.dir, "*.vtt")))
    infos = sorted(glob.glob(os.path.join(args.dir, "*.info.json")))
    if not vtts:
        print(json.dumps({"error": "no .vtt file found in dir"}))
        sys.exit(1)

    # prefer human (.en.vtt) over auto (.en.*.vtt is auto in some cases); just take first
    vtt_path = vtts[0]
    with open(vtt_path, "r", encoding="utf-8", errors="replace") as f:
        vtt_text = f.read()

    info = {}
    if infos:
        with open(infos[0], "r", encoding="utf-8", errors="replace") as f:
            info = json.load(f)

    chapters = info.get("chapters") or []
    cues = parse_cues(vtt_text)
    segments = dedup(cues)
    buckets = bucket_by_chapter(segments, chapters)

    word_count = sum(len(t.split()) for _, _, t in buckets)

    # write transcript_clean.md
    out_path = os.path.join(args.dir, "transcript_clean.md")
    with open(out_path, "w", encoding="utf-8") as f:
        for title, start, text in buckets:
            if not text:
                continue
            if title == "Transcript" and len(buckets) == 1:
                f.write("## Transcript\n\n")
            else:
                f.write(f"## [{fmt_ts(start)}] {title}\n\n")
            f.write(text + "\n\n")

    summary = {
        "title": info.get("title", ""),
        "channel": info.get("channel") or info.get("uploader", ""),
        "duration": info.get("duration_string") or info.get("duration", ""),
        "upload_date": info.get("upload_date", ""),
        "webpage_url": info.get("webpage_url", ""),
        "chapters": [c.get("title", "") for c in chapters],
        "word_count": word_count,
        "description": info.get("description", ""),
        "transcript_path": out_path,
        "sub_type": "human" if vtt_path.endswith(".en.vtt") else "auto",
    }
    sys.stdout.write(json.dumps(summary, ensure_ascii=True))
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
