#!/usr/bin/env python3
"""build_state_index.py
============================

Catalogs the vault's current state in one pass and writes the single shared
state snapshot to <logs>/Vault-State-Index.md — at the <logs> root (NOT under an
agent folder), overwritten each run, read by the janitor and the analyst as
their work list (CONFIG § Log files: two artifacts under <logs>, no per-run
files). The append-only event ledger is the other artifact, owned by each agent.

This script is the SOLE owner of the snapshot. It carries:
  - The analyst's macro sections: ## Folder histogram, ## Types in use,
    ## Indexes, ## Uplink coverage, and the other descriptive tables.
  - A ## Open findings section in the kit's state-log shape
    (`timestamp | actor | code | target | count`, CONFIG § Log files): one
    counted row per open finding the janitor and analyst reason over.

Open findings emitted here (snapshot-derivable, deterministic detections):
  - `reviewed-stale`  — a `reviewed: true` note whose linked plan or spec
    CONTENT actually changed after the note's review: the upstream's body
    md5 differs from the hash recorded in the previous snapshot's ## Content
    hashes section AND its mtime is newer than the note's `date`. Pure mtime
    drift (bulk re-touches, audit --apply runs) no longer fires: a finding is
    suppressed when >= 10 files share the upstream's mtime (bulk-touch
    signature) and reciprocal pairs (A flags B while B flags A — a real
    invalidation cannot be mutual) are suppressed as noise. The janitor reads
    the note against the upstream change and confirms.
  - `tag-resolution`  — a `tags:` value `normalize_tag` could neither map to a
    canonical tag nor accept as a plain open-vocabulary tag (a near-miss that is
    not exact, alias, plural, or Levenshtein-1). The janitor routes it.
  - `type-resolution` — a `type:` value that does not normalize to a canonical
    type.
  - `parent-missing`  — a typed note that requires an uplink but carries none.
  - `duplicate-asset` — a loose (un-contained) non-md asset whose identical
    bytes appear at >1 path under one owning root (project/area/domain/snippets);
    pure redundancy. Asset folders (git/.hg/.svn, .keep-whole) are pruned from
    the walk, and `complete` (deployed) projects are skipped. Janitor archives
    the redundant copies, keeping the 02-Assets canonical.
  - `diverged-asset` — the same loose-asset filename carrying DIFFERENT content
    at >1 path under one owning root; a version ambiguity. The janitor picks the
    canonical copy (version token, date, or date of related use in a plan/log)
    and archives the rest, queueing only a genuine scope fork.

A caller (audit.py) may hand additional, run-scoped findings to fold into the
same ## Open findings section via --findings, so the snapshot stays the one
work list and no second snapshot file is written.

NO history file is written: CONFIG § Log files forbids per-run files under
<logs>; the append-only ledgers carry the longitudinal record.

The ## Folder histogram feeds the analyst's cluster detection — per folder,
total note count, count-by-type, dominant tags, and a maturity split. This
script provides DATA only; the analyst owns the thresholds. A min-size floor
(FOLDER_HISTOGRAM_MIN_NOTES) keeps tiny folders out of the histogram.

The snapshot also carries a `## Content hashes` section — one `path | md5`
row per vault .md (md5 of the body, frontmatter excluded) — read back on the
next run as the change-evidence baseline for `reviewed-stale`.

Usage
-----
    JANITOR_VAULT_ROOT=/path/to/vault python build_state_index.py \
        [--run-log PATH] [--findings PATH]

Arguments
---------
--run-log PATH   Optional. Path to an agent run-log file. When provided, the
                 ## File change log section is populated from any
                 '## Auto-fixes applied' section found in that log.
--findings PATH  Optional. Path to a findings file: lines of
                 `code | target | value`. Each is folded into ## Open findings
                 alongside this script's own detections.

Environment
-----------
JANITOR_VAULT_ROOT   Required. Absolute path to the vault root.
"""

from __future__ import annotations

import sys
import os
import re
import hashlib
from pathlib import Path
from datetime import datetime, timezone
from collections import defaultdict
from typing import Optional

import yaml

# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import (
    FOLDER_ROUTING,
    TYPES,
    CANONICAL_TYPE_KEYS,
    SCAN_EXCLUDE_DIRS,
    _folder_by_semantic,
    is_excluded_dir,
    is_asset_folder,
    normalize_tag,
)
from normalize_type import normalize_type
from wikilink_helpers import extract_wikilinks

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry — runs only when invoked as a script, never on import."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Build a structured snapshot of vault state."
    )
    parser.add_argument(
        "--run-log",
        type=Path,
        default=None,
        help="Path to an agent run-log; populates ## File change log section.",
    )
    parser.add_argument(
        "--findings",
        type=Path,
        default=None,
        help="Path to a `code | target | value`-per-line findings file; folded "
             "into the ## Open findings section.",
    )
    args = parser.parse_args()

    # ---------------------------------------------------------------------------
    # Vault root resolution
    # ---------------------------------------------------------------------------

    vault_root_str = os.environ.get("JANITOR_VAULT_ROOT")
    if not vault_root_str:
        sys.exit("Error: JANITOR_VAULT_ROOT environment variable is not set.")

    VAULT_ROOT = Path(vault_root_str).resolve()
    if not VAULT_ROOT.is_dir():
        sys.exit(f"Error: JANITOR_VAULT_ROOT does not point to a directory: {VAULT_ROOT}")

    # ---------------------------------------------------------------------------
    # Semantic folder names
    # ---------------------------------------------------------------------------

    INBOX_FOLDER = _folder_by_semantic("inbox")    # e.g. "00-Inbox"
    ARCHIVE_FOLDER = _folder_by_semantic("archive")  # e.g. "99-Archive"

    def _semantic_or(needle: str, fallback: str) -> str:
        try:
            return _folder_by_semantic(needle)
        except Exception:
            return fallback

    PROJECTS_FOLDER = _semantic_or("projects", "01-Projects")
    AREAS_FOLDER = _semantic_or("areas", "01-Areas")
    REFERENCE_FOLDER = _semantic_or("reference", "01-References")
    SNIPPETS_FOLDER = _semantic_or("snippets", "01-Snippets")

    # ---------------------------------------------------------------------------
    # Output paths — canonical 99-Logs root (NOT under an agent folder)
    # ---------------------------------------------------------------------------

    output_dir = VAULT_ROOT / ARCHIVE_FOLDER / "99-Logs"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "Vault-State-Index.md"

    # ---------------------------------------------------------------------------
    # Control files
    # ---------------------------------------------------------------------------

    CONTROL_FILES = [
        VAULT_ROOT / ARCHIVE_FOLDER / "99-Logs" / "Vault-State-Index.md",
        VAULT_ROOT / INBOX_FOLDER / "00-Action-Queue.md",
        VAULT_ROOT / ARCHIVE_FOLDER / "99-Logs" / "Sync-Log.md",
    ]

    # ---------------------------------------------------------------------------
    # Hands-off filtering
    # ---------------------------------------------------------------------------

    # Compiled patterns to skip unconditionally (non-content system dirs)
    _ALWAYS_SKIP_PARTS = frozenset(["__pycache__", ".git", ".venv"])


    def _is_hands_off(path: Path) -> bool:
        """Return True if path should be skipped per FOLDER_ROUTING hands_off_patterns.

        Pattern semantics (from CONFIG.md):
          - '*'           matches any path under the folder (skip everything)
          - '<name>'      matches any single path segment (any folder name)
          - 'Literal.md'  matches an exact filename
          - 'prefix/'     matches a subfolder prefix

        Also skips __pycache__, .git, .venv, *.pyc, and every dot-directory
        (CONFIG § Folders / Scan exclusions — `.claude`, `.obsidian`, `.trash`, …).
        """
        # Scan-exclusion: any dot-directory segment (the kit's own `.claude/` install
        # dir included) is tooling/config, never vault content.
        if any(is_excluded_dir(part) for part in path.parts):
            return True
        # Always-skip system directories
        for part in path.parts:
            if part in _ALWAYS_SKIP_PARTS:
                return True
        if path.suffix == ".pyc":
            return True

        # Determine which top-level folder this file lives under
        try:
            rel = path.relative_to(VAULT_ROOT)
        except ValueError:
            return False

        if not rel.parts:
            return False

        top_level = rel.parts[0]

        # Look up routing row for this top-level folder
        routing_row = FOLDER_ROUTING.get(top_level)
        if routing_row is None:
            return False

        for pattern in routing_row.hands_off_patterns:
            pattern = pattern.strip()
            if not pattern:
                continue

            # '*' — skip everything under this folder
            if pattern == "*":
                return True

            # '<name>' — skip any single segment (any folder name)
            if pattern == "<name>" or (pattern.startswith("<") and pattern.endswith(">")):
                # matches a direct child folder
                if len(rel.parts) >= 2:
                    return True

            # Exact filename match (e.g. '00-Action-Queue.md')
            if path.name == pattern:
                return True

            # Subfolder prefix match (e.g. 'Sessions/')
            if pattern.endswith("/"):
                folder_name = pattern.rstrip("/")
                if folder_name in rel.parts[1:]:
                    return True

        return False


    # ---------------------------------------------------------------------------
    # Frontmatter extraction
    # ---------------------------------------------------------------------------

    _FM_FENCE = re.compile(r"^---\s*$", re.MULTILINE)
    _WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\[\]]+)\]\]")


    def _parse_frontmatter(text: str) -> Optional[dict]:
        """Return parsed frontmatter dict, or None if absent / unparseable."""
        fences = list(_FM_FENCE.finditer(text))
        if len(fences) < 2:
            return None
        fm_text = text[fences[0].end(): fences[1].start()]
        try:
            data = yaml.safe_load(fm_text)
            return data if isinstance(data, dict) else None
        except yaml.YAMLError:
            return None


    def _extract_wikilinks_from_text(text: str) -> list[str]:
        """Return list of bare wikilink basenames (no path/alias/anchor, no embeds)."""
        results = []
        seen: set[str] = set()
        for m in _WIKILINK_RE.finditer(text):
            interior = m.group(1)
            # strip path prefix
            if "/" in interior:
                interior = interior.rsplit("/", 1)[-1]
            # strip alias
            if "|" in interior:
                interior = interior.split("|", 1)[0]
            # strip heading anchor
            if "#" in interior:
                interior = interior.split("#", 1)[0]
            interior = interior.strip()
            if interior.lower().endswith(".md"):
                interior = interior[:-3]
            interior = interior.strip()
            if interior and interior not in seen:
                seen.add(interior)
                results.append(interior)
        return results


    # ---------------------------------------------------------------------------
    # Previous-run comparison helpers
    # ---------------------------------------------------------------------------

    _PREV_FILE_RE = re.compile(r"^\| `([^`]+)` \|", re.MULTILINE)


    def _read_previous_files(index_path: Path) -> Optional[set[str]]:
        """Extract file paths listed in a previous State Index.

        Returns None if no previous index exists. Paths are vault-relative strings.
        We scan any table cell that looks like a relative path (contains '/' or ends .md).
        """
        if not index_path.exists():
            return None
        try:
            content = index_path.read_text(encoding="utf-8")
        except OSError:
            return None

        found: set[str] = set()
        # Look for backtick-quoted paths in table cells across all sections
        for m in re.finditer(r"`([^`]+\.md)`", content):
            candidate = m.group(1)
            # Only vault-relative paths (no leading slash, not absolute)
            if not candidate.startswith("/") and "\\" not in candidate:
                found.add(candidate)
        return found if found else None


    def _body_md5(text: str) -> str:
        """md5 of a note's BODY (frontmatter excluded), so a frontmatter-only
        touch — a reviewed flip, a tag stamp, an audit --apply pass — does not
        register as a content change."""
        body = text
        if text.startswith("---"):
            fences = list(_FM_FENCE.finditer(text))
            if len(fences) >= 2 and fences[0].start() == 0:
                body = text[fences[1].end():]
        return hashlib.md5(body.encode("utf-8", errors="replace")).hexdigest()


    def _read_previous_hashes(index_path: Path) -> dict[str, str]:
        """Read the prior snapshot's ## Content hashes rows: rel-path -> md5.

        This is the change-evidence baseline for reviewed-stale: an upstream
        only counts as changed when its current body hash differs from the
        hash recorded here. Empty dict when no prior snapshot or section."""
        if not index_path.exists():
            return {}
        try:
            content = index_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        m = re.search(r"^## Content hashes\s*$", content, re.MULTILINE)
        if not m:
            return {}
        after = content[m.end():]
        nxt = re.search(r"^## ", after, re.MULTILINE)
        section = after[: nxt.start()] if nxt else after
        out: dict[str, str] = {}
        for line in section.splitlines():
            mm = re.match(r"^\|\s*`([^`]+)`\s*\|\s*([0-9a-f]{32})\s*\|$", line.strip())
            if mm:
                out[mm.group(1)] = mm.group(2)
        return out


    # ---------------------------------------------------------------------------
    # Run-number tracking (read from the snapshot's own header — no history file)
    # ---------------------------------------------------------------------------

    def _get_run_number(index_path: Path) -> int:
        """Return the next run number, read from the prior snapshot's header comment.

        The snapshot is overwritten each run and there is no history file
        (CONFIG § Log files), so the run counter lives in the snapshot's own
        `Run #N` header marker; absent a prior snapshot, the run is #1.
        """
        if not index_path.exists():
            return 1
        try:
            content = index_path.read_text(encoding="utf-8")
            m = re.search(r"Run #(\d+)", content)
            return int(m.group(1)) + 1 if m else 1
        except OSError:
            return 1


    def _UNUSED_old_run_number(history_path: Path) -> int:
        """Return the next run number based on lines in history file."""
        if not history_path.exists():
            return 1
        try:
            content = history_path.read_text(encoding="utf-8")
            # Count data rows (skip header and separator)
            data_rows = [
                line for line in content.splitlines()
                if line.startswith("|") and not line.startswith("| timestamp") and "---" not in line
            ]
            return len(data_rows) + 1
        except OSError:
            return 1


    # ---------------------------------------------------------------------------
    # Quarter bucket
    # ---------------------------------------------------------------------------

    def _quarter_bucket(date_val) -> str:
        """Convert a date frontmatter value to 'YYYY-Q#' string."""
        if date_val is None:
            return "no-date"
        try:
            if hasattr(date_val, "year"):
                year = date_val.year
                month = date_val.month if hasattr(date_val, "month") else 1
            else:
                s = str(date_val).strip()
                year = int(s[:4])
                month = int(s[5:7]) if len(s) >= 7 else 1
            q = (month - 1) // 3 + 1
            return f"{year}-Q{q}"
        except Exception:
            return "no-date"


    # A file is "aged" once it is older than this many days. The split feeds the
    # analyst's maturity signal; the cut point itself is a reporting convenience,
    # not a cluster threshold.
    AGED_THRESHOLD_DAYS = 180


    def _age_in_days(date_val) -> Optional[int]:
        """Days between a file's `date` frontmatter and now (UTC). None if undated
        or unparseable. Negative ages (future dates) are clamped to 0."""
        if date_val is None:
            return None
        try:
            if hasattr(date_val, "year"):
                year = date_val.year
                month = date_val.month if hasattr(date_val, "month") else 1
                day = date_val.day if hasattr(date_val, "day") else 1
            else:
                s = str(date_val).strip()
                year = int(s[:4])
                month = int(s[5:7]) if len(s) >= 7 else 1
                day = int(s[8:10]) if len(s) >= 10 else 1
            d = datetime(year, month, day, tzinfo=timezone.utc)
            return max(0, (now_utc - d).days)
        except Exception:
            return None


    # ---------------------------------------------------------------------------
    # Run-log parsing
    # ---------------------------------------------------------------------------

    def _parse_run_log_fixes(run_log_path: Path) -> list[tuple[str, str, str]]:
        """Parse '## Auto-fixes applied' section from a run log.

        Returns list of (path, action, detail) tuples.
        """
        if run_log_path is None or not run_log_path.exists():
            return []
        try:
            content = run_log_path.read_text(encoding="utf-8")
        except OSError:
            return []

        # Find the section
        m = re.search(r"^## Auto-fixes applied\s*$", content, re.MULTILINE)
        if not m:
            return []

        after = content[m.end():]
        next_h2 = re.search(r"^## ", after, re.MULTILINE)
        section = after[: next_h2.start()] if next_h2 else after

        results = []
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                continue
            if re.match(r"^\|[-| ]+\|$", stripped):
                continue
            parts = [c.strip() for c in stripped.strip("|").split("|")]
            if len(parts) >= 3 and parts[0] and "path" not in parts[0].lower():
                results.append((parts[0], parts[1] if len(parts) > 1 else "", parts[2] if len(parts) > 2 else ""))

        return results


    # ---------------------------------------------------------------------------
    # Markdown table helpers
    # ---------------------------------------------------------------------------

    def _md_table(headers: list[str], rows: list[list[str]]) -> str:
        """Render a markdown table. All values coerced to str."""
        sep = "| " + " | ".join("---" for _ in headers) + " |"
        header = "| " + " | ".join(headers) + " |"
        lines = [header, sep]
        for row in rows:
            cells = [str(c).replace("|", "\\|") for c in row]
            lines.append("| " + " | ".join(cells) + " |")
        return "\n".join(lines)


    def _example_paths(paths: list[str], limit: int = 3) -> str:
        """Join up to `limit` paths with ' | ' separator."""
        return " \\| ".join(paths[:limit])


    def _fmt_counter(counter: dict[str, int], limit: int = 5) -> str:
        """Render a count dict as 'key:count, key:count' descending, top `limit`."""
        ordered = sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))
        return ", ".join(f"{k}:{c}" for k, c in ordered[:limit])


    # ---------------------------------------------------------------------------
    # Main walk
    # ---------------------------------------------------------------------------

    now_utc = datetime.now(timezone.utc)
    run_number = _get_run_number(output_path)

    # Accumulators
    type_counts: dict[str, int] = defaultdict(int)
    type_examples: dict[str, list[str]] = defaultdict(list)

    tag_counts: dict[str, int] = defaultdict(int)
    tag_examples: dict[str, list[str]] = defaultdict(list)

    ext_counts: dict[str, int] = defaultdict(int)
    ext_examples: dict[str, list[str]] = defaultdict(list)

    index_files: list[dict] = []          # {path, parent, children_count}
    md_files_all: list[str] = []          # vault-relative path strings
    non_md_files: list[Path] = []

    # Link graph: outbound wikilinks per file
    outbound_links: dict[str, list[str]] = {}   # rel_path -> [target_basenames]

    # Frontmatter field presence: per-file record
    fm_records: list[dict] = []   # {rel_path, abspath, fm: dict|None, links: list}

    # Basename -> absolute Path, for resolving a note's wikilinked plan/spec when
    # detecting reviewed-stale notes.
    basename_to_abspath: dict[str, Path] = {}

    # Per-file body hash (rel -> md5) — written to ## Content hashes and read
    # back next run as the reviewed-stale change-evidence baseline.
    content_hashes: dict[str, str] = {}
    # mtime histogram (whole-second buckets) — >= 10 files sharing one mtime is
    # a bulk-touch signature; reviewed-stale findings against such an upstream
    # are suppressed.
    mtime_counts: dict[int, int] = defaultdict(int)
    file_mtimes: dict[str, float] = {}

    # Folder × type
    folder_type_counts: dict[tuple[str, str], int] = defaultdict(int)

    # Per-folder aggregates for the analyst's cluster detection. Folder is the
    # immediate containing folder relative to the vault root (e.g.
    # "01-Projects/Glass-Fracture/Sessions"). For each folder we track the total .md
    # count, a per-type histogram, a per-tag histogram, and the per-file ages (days
    # since each file's `date` frontmatter). The analyst reads these columns and
    # applies its own relative-dominance + maturity thresholds — this script encodes
    # none. Folders below FOLDER_HISTOGRAM_MIN_NOTES are dropped from the histogram
    # so tiny folders do not bloat it.
    FOLDER_HISTOGRAM_MIN_NOTES = 3
    folder_total_counts: dict[str, int] = defaultdict(int)
    folder_type_hist: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    folder_tag_hist: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    folder_ages_days: dict[str, list[int]] = defaultdict(list)  # days-old per dated file

    # Review backlog: folder -> reviewed:false count
    review_backlog: dict[str, int] = defaultdict(int)

    # Age distribution
    age_buckets: dict[str, int] = defaultdict(int)

    # Catch-all candidate folders (containing note or journal in type_defaults, no wildcard)
    catch_all_folders: list[str] = []
    for folder_name, routing_row in FOLDER_ROUTING.items():
        has_catchall_type = any(t in ("note", "journal") for t in routing_row.type_defaults)
        has_wildcard = "*" in routing_row.hands_off_patterns
        if has_catchall_type and not has_wildcard:
            catch_all_folders.append(folder_name)


    def _iter_vault_paths():
        """Yield every file under the vault, pruning excluded directories on descent.

        os.walk lets us drop excluded directories (CONFIG § Folders / Scan
        exclusions — any dot-directory, e.g. the kit's own `.claude/`) from
        `dirnames` in place so the walk never enters them; a file inside one is
        never enumerated. Results are sorted to preserve the prior deterministic
        ordering (example-path selection depends on it).
        """
        collected: list[Path] = []
        for dirpath, dirnames, filenames in os.walk(VAULT_ROOT):
            # Prune excluded and asset directories — a classified asset folder
            # (CONFIG § Asset folders) is opaque; its interior is never indexed.
            dirnames[:] = [
                d for d in dirnames
                if not is_excluded_dir(d) and not is_asset_folder(Path(dirpath) / d)
            ]
            for fname in filenames:
                collected.append(Path(dirpath) / fname)
        return sorted(collected)


    def _walk():
        for path in _iter_vault_paths():
            if not path.is_file():
                continue
            if _is_hands_off(path):
                continue

            try:
                rel = str(path.relative_to(VAULT_ROOT)).replace("\\", "/")
            except ValueError:
                continue

            ext = path.suffix.lower() or "(none)"

            if path.suffix.lower() != ".md":
                non_md_files.append(path)
                ext_counts[ext] += 1
                if len(ext_examples[ext]) < 3:
                    ext_examples[ext].append(rel)
                continue

            # It's a markdown file
            md_files_all.append(rel)
            ext_counts[".md"] += 1
            if len(ext_examples[".md"]) < 3:
                ext_examples[".md"].append(rel)

            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""

            fm = _parse_frontmatter(text)

            # Wikilinks (outbound)
            links = _extract_wikilinks_from_text(text)
            outbound_links[rel] = links

            # Record the absolute path and outbound links alongside the frontmatter
            # so the open-findings detections (reviewed-stale) can stat linked files.
            # `links` (above) is the raw extractor for the legacy graph metrics;
            # `helper_links` uses the kit's fenced- and inline-code-aware
            # extract_wikilinks on the BODY ONLY (frontmatter uplinks are parent-
            # resolution's job, not dangling-link's) so template/example links
            # inside code never count as ghosts.
            _body = text
            if text.startswith("---"):
                _ff = list(_FM_FENCE.finditer(text))
                if len(_ff) >= 2 and _ff[0].start() == 0:
                    _body = text[_ff[1].end():]
            fm_records.append({
                "rel_path": rel, "abspath": path, "fm": fm,
                "links": links, "helper_links": extract_wikilinks(_body),
            })
            basename_to_abspath.setdefault(path.stem, path)

            # Content hash + mtime evidence for reviewed-stale.
            content_hashes[rel] = _body_md5(text)
            try:
                _mt = path.stat().st_mtime
                file_mtimes[rel] = _mt
                mtime_counts[int(_mt)] += 1
            except OSError:
                pass

            # Top-level folder
            parts = rel.split("/")
            top_folder = parts[0] if parts else ""
            # Immediate containing folder (for per-folder histogram).
            containing_folder = "/".join(parts[:-1]) if len(parts) > 1 else top_folder

            # Type counting
            raw_type = fm.get("type") if fm else None
            canonical = normalize_type(raw_type) if raw_type else None
            type_key = canonical if canonical else (str(raw_type).lower().strip() if raw_type else "(none)")
            type_counts[type_key] += 1
            if len(type_examples[type_key]) < 3:
                type_examples[type_key].append(rel)

            # Per-folder aggregates
            folder_total_counts[containing_folder] += 1
            folder_type_hist[containing_folder][type_key] += 1
            _age_days = _age_in_days((fm or {}).get("date"))
            if _age_days is not None:
                folder_ages_days[containing_folder].append(_age_days)

            # Tags
            if fm:
                tags_raw = fm.get("tags") or []
                if isinstance(tags_raw, str):
                    tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]
                elif not isinstance(tags_raw, list):
                    tags_raw = []
                for tag in tags_raw:
                    tag_str = str(tag).strip().lower()
                    if tag_str:
                        tag_counts[tag_str] += 1
                        folder_tag_hist[containing_folder][tag_str] += 1
                        if len(tag_examples[tag_str]) < 3:
                            tag_examples[tag_str].append(rel)

            # Index files
            if canonical == "index":
                children = [lk for lk in links]
                index_files.append({
                    "path": rel,
                    "parent": (fm or {}).get("parent") or (fm or {}).get("Parent", ""),
                    "children_count": len(children),
                })

            # Folder × type
            folder_type_counts[(top_folder, type_key)] += 1

            # Review backlog
            if fm:
                reviewed = fm.get("reviewed")
                if str(reviewed).lower() in ("false", "no", "0"):
                    review_backlog[top_folder] += 1

            # Age distribution
            date_val = (fm or {}).get("date")
            bucket = _quarter_bucket(date_val)
            age_buckets[bucket] += 1


    _walk()

    # ---------------------------------------------------------------------------
    # Lifecycle types — count ARCHIVED members. Types whose completed home is
    # the archive (default-home references <archive>: addendum, revision,
    # session, log — plus the journal/addendum/revision lifecycle set) keep
    # their members in <archive>, which the walk skips as hands-off. Counting
    # archived members into ## Types in use means a healthy lifecycle no longer
    # reads as a dead type (`type-unused` false positive) — here and in the
    # audit's pass-13 drift read of the same table.
    # ---------------------------------------------------------------------------

    _LIFECYCLE_TYPES = {
        key for key, trow in TYPES.items()
        if "<archive>" in (getattr(trow, "default_home", "") or "")
    } | {"addendum", "revision", "journal"}
    _archive_root = VAULT_ROOT / ARCHIVE_FOLDER
    if _archive_root.is_dir():
        for ap in sorted(_archive_root.rglob("*.md")):
            if any(is_excluded_dir(part) for part in ap.parts):
                continue
            try:
                a_text = ap.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            a_fm = _parse_frontmatter(a_text)
            a_type = normalize_type(str((a_fm or {}).get("type") or "")) if a_fm else None
            if a_type in _LIFECYCLE_TYPES:
                try:
                    a_rel = str(ap.relative_to(VAULT_ROOT)).replace("\\", "/")
                except ValueError:
                    a_rel = str(ap)
                type_counts[a_type] += 1
                if len(type_examples[a_type]) < 3:
                    type_examples[a_type].append(a_rel)

    # ---------------------------------------------------------------------------
    # Inbound link counts
    # ---------------------------------------------------------------------------

    inbound_counts: dict[str, int] = defaultdict(int)
    for rel, targets in outbound_links.items():
        for target in targets:
            inbound_counts[target] += 1

    # ---------------------------------------------------------------------------
    # Compute inbound count for each md file by its stem
    # ---------------------------------------------------------------------------

    def _file_stem(rel: str) -> str:
        return Path(rel).stem


    def _inbound_for(rel: str) -> int:
        stem = _file_stem(rel)
        return inbound_counts.get(stem, 0)


    # ---------------------------------------------------------------------------
    # Orphan assets (non-md with no inbound wikilink)
    # ---------------------------------------------------------------------------

    orphan_asset_rows: list[tuple[str, int, str]] = []
    for npath in non_md_files:
        try:
            rel = str(npath.relative_to(VAULT_ROOT)).replace("\\", "/")
        except ValueError:
            continue
        stem = npath.stem
        if inbound_counts.get(stem, 0) == 0 and inbound_counts.get(npath.name, 0) == 0:
            try:
                size = npath.stat().st_size
                mtime = datetime.fromtimestamp(npath.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
            except OSError:
                size = 0
                mtime = "unknown"
            orphan_asset_rows.append((rel, size, mtime))

    # ---------------------------------------------------------------------------
    # Asset duplication / divergence (scattered non-md copies)
    # ---------------------------------------------------------------------------
    # The walk has already pruned every asset folder — git/.hg/.svn working trees
    # and .keep-whole units (CONFIG § Asset folders) — so only LOOSE (un-contained)
    # assets reach here. A loose asset appearing more than once under the same
    # owning root is clutter: identical bytes in N places (`duplicate-asset`) or
    # one filename carrying different content (`diverged-asset`, a version
    # ambiguity). Grouping is scoped to a single owning root, so a like-named file
    # in two unrelated projects is never a false positive. A `complete` (deployed)
    # project is hands-off and skipped; the janitor reconciles the rest.

    def _file_md5(path: Path, cap: int = 100 * 1024 * 1024) -> Optional[str]:
        """Full-file md5, or None if unreadable or above the size cap."""
        try:
            if path.stat().st_size > cap:
                return None
            h = hashlib.md5()
            with path.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            return h.hexdigest()
        except OSError:
            return None

    def _owning_root(rel: str) -> Optional[str]:
        """The subtree a dedup group is scoped to: a project, area, or reference
        domain (`<top>/<member>`), or the snippets root. None for a bare file."""
        parts = rel.split("/")
        if not parts:
            return None
        top = parts[0]
        if top in (PROJECTS_FOLDER, AREAS_FOLDER, REFERENCE_FOLDER) and len(parts) >= 3:
            return f"{parts[0]}/{parts[1]}"
        if top == SNIPPETS_FOLDER and len(parts) >= 2:
            return top
        return None

    # Project status — a `complete` project is deployed → hands-off for dedup.
    _project_status: dict[str, str] = {}
    for _r in fm_records:
        _parts = _r["rel_path"].split("/")
        if (len(_parts) == 3 and _parts[0] == PROJECTS_FOLDER
                and _parts[2].startswith("00-")):
            _st = str((_r["fm"] or {}).get("status", "")).strip().lower()
            if _st:
                _project_status[f"{_parts[0]}/{_parts[1]}"] = _st

    # Group loose non-md by (owning-root, basename).
    _asset_groups: dict[tuple[str, str], list[tuple[str, str, int, str]]] = defaultdict(list)
    for npath in non_md_files:
        try:
            rel = str(npath.relative_to(VAULT_ROOT)).replace("\\", "/")
        except ValueError:
            continue
        root = _owning_root(rel)
        if root is None:
            continue
        if _project_status.get(root) == "complete":
            continue  # deployed project — hands-off
        md5 = _file_md5(npath)
        if md5 is None:
            continue
        try:
            size = npath.stat().st_size
            mtime = datetime.fromtimestamp(
                npath.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        except OSError:
            size, mtime = 0, "unknown"
        _asset_groups[(root, npath.name)].append((rel, md5, size, mtime))

    # One finding per multi-copy group; canonical hint = the 02-Assets copy when
    # present (the Centralize standard's home), else the first path.
    asset_dedup_findings: list[tuple[str, str, str]] = []
    for (root, name), copies in sorted(_asset_groups.items()):
        if len(copies) < 2:
            continue
        copies = sorted(copies)
        canonical = next(
            (rel for rel, _h, _s, _m in copies if "/02-Assets/" in f"/{rel}"),
            copies[0][0],
        )
        if len({c[1] for c in copies}) == 1:
            redundant = [c[0] for c in copies if c[0] != canonical]
            value = (f"{len(copies)} identical copies (md5 {copies[0][1][:8]}); "
                     f"canonical={canonical}; redundant={', '.join(redundant)}")
            asset_dedup_findings.append(("duplicate-asset", canonical, value))
        else:
            detail = "; ".join(f"{rel}({h[:8]},{s}B,{m})" for rel, h, s, m in copies)
            value = f"{len(copies)} divergent copies of {name} under {root}: {detail}"
            asset_dedup_findings.append(("diverged-asset", canonical, value))

    # ---------------------------------------------------------------------------
    # Link graph summary
    # ---------------------------------------------------------------------------

    inbox_folder_prefix = INBOX_FOLDER + "/"

    graph_orphans: list[str] = []
    leaf_nodes: list[str] = []
    well_connected: list[str] = []

    for rel in md_files_all:
        # Skip inbox for orphan detection
        in_excluded = rel.startswith(inbox_folder_prefix)
        inbound = _inbound_for(rel)
        outbound = len(outbound_links.get(rel, []))

        if inbound >= 1 and outbound >= 1:
            well_connected.append(rel)
        elif outbound == 0:
            leaf_nodes.append(rel)

        if not in_excluded and inbound == 0:
            graph_orphans.append(rel)

    # ---------------------------------------------------------------------------
    # Drift summary
    # ---------------------------------------------------------------------------

    # type-not-canonical: files where raw type doesn't normalize
    type_not_canonical_examples: list[str] = []
    for record in fm_records:
        fm = record.get("fm")
        raw_type = fm.get("type") if fm else None
        if raw_type is not None:
            if normalize_type(str(raw_type)) is None:
                type_not_canonical_examples.append(record["rel_path"])

    # type-unused: canonical keys with zero occurrences
    type_unused: list[str] = [k for k in CANONICAL_TYPE_KEYS if type_counts.get(k, 0) == 0]

    # ---------------------------------------------------------------------------
    # Catch-all status
    # ---------------------------------------------------------------------------

    catch_all_status_rows: list[tuple[str, int, int]] = []
    for folder_name in catch_all_folders:
        folder_path = VAULT_ROOT / folder_name
        stamped = 0
        unstamped = 0
        if folder_path.is_dir():
            for md_path in folder_path.glob("*.md"):
                text = ""
                try:
                    text = md_path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    pass
                fm = _parse_frontmatter(text)
                if fm and fm.get("type"):
                    stamped += 1
                else:
                    unstamped += 1
        catch_all_status_rows.append((folder_name, stamped, unstamped))

    # ---------------------------------------------------------------------------
    # Frontmatter coverage
    # ---------------------------------------------------------------------------

    # Fields to check: type, date, tags, reviewed, uplink
    UPLINK_FIELD_MAP: dict[str, str] = {
        "project": "parent",
        "area": "parent",
        "reference": "parent",
        "plan": "parent",
        "note": "parent",
        "journal": "parent",
        "idea": "parent",
        "snippet": "parent",
        "source": "parent",
        "design": "parent",
        "format": "parent",
        "voice": "parent",
        "session": "project",
        "research": "project",
        "addendum": "target",
        "revision": "target",
        "checkpoint": "target",
        "index": None,  # index files don't need an uplink
        "log": None,    # logs don't need an uplink
    }

    fm_field_present: dict[str, int] = defaultdict(int)
    fm_field_missing: dict[str, int] = defaultdict(int)
    fm_field_missing_examples: dict[str, list[str]] = defaultdict(list)

    uplink_type_has: dict[str, int] = defaultdict(int)
    uplink_type_no: dict[str, int] = defaultdict(int)

    total_md = len(fm_records)

    for record in fm_records:
        rel = record["rel_path"]
        fm = record.get("fm") or {}

        raw_type = fm.get("type") if fm else None
        canonical_type = normalize_type(str(raw_type)) if raw_type else None

        for field in ("type", "date", "tags"):
            if fm and field in fm and fm[field] is not None:
                fm_field_present[field] += 1
            else:
                fm_field_missing[field] += 1
                if len(fm_field_missing_examples[field]) < 3:
                    fm_field_missing_examples[field].append(rel)

        # reviewed field
        if fm and "reviewed" in fm:
            fm_field_present["reviewed"] += 1
        else:
            fm_field_missing["reviewed"] += 1
            if len(fm_field_missing_examples["reviewed"]) < 3:
                fm_field_missing_examples["reviewed"].append(rel)

        # uplink field: passes if type needs no uplink (index/log), or if the
        # appropriate uplink field is present.
        needs_no_uplink = canonical_type in ("index", "log")
        uplink_field = UPLINK_FIELD_MAP.get(canonical_type) if canonical_type else None

        if needs_no_uplink:
            fm_field_present["uplink"] += 1
        elif uplink_field and fm and uplink_field in fm:
            fm_field_present["uplink"] += 1
        else:
            fm_field_missing["uplink"] += 1
            if len(fm_field_missing_examples["uplink"]) < 3:
                fm_field_missing_examples["uplink"].append(rel)

        # Uplink coverage per type
        if canonical_type and not needs_no_uplink:
            uplink_field_for_type = UPLINK_FIELD_MAP.get(canonical_type)
            if uplink_field_for_type and fm and uplink_field_for_type in fm:
                uplink_type_has[canonical_type] += 1
            else:
                uplink_type_no[canonical_type] += 1

    # ---------------------------------------------------------------------------
    # Previous-run lifecycle events
    # ---------------------------------------------------------------------------

    previous_files = _read_previous_files(output_path)
    new_files: list[str] = []
    gone_files: list[str] = []

    if previous_files is not None:
        current_set = set(md_files_all)
        new_files = sorted(current_set - previous_files)
        gone_files = sorted(previous_files - current_set)

    # ---------------------------------------------------------------------------
    # Control file status
    # ---------------------------------------------------------------------------

    control_rows: list[list[str]] = []
    for cf in CONTROL_FILES:
        try:
            rel_cf = str(cf.relative_to(VAULT_ROOT)).replace("\\", "/")
        except ValueError:
            rel_cf = str(cf)
        if cf.exists():
            try:
                stat = cf.stat()
                size = stat.st_size
                mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except OSError:
                size = 0
                mtime = "unknown"
            notes = ""
            # Action-queue large check
            if cf.name == "00-Action-Queue.md" and size > 50 * 1024:
                notes = "large"
            control_rows.append([rel_cf, "yes", str(size), mtime, notes])
        else:
            control_rows.append([rel_cf, "no", "-", "-", "missing"])

    # ---------------------------------------------------------------------------
    # Assemble output
    # ---------------------------------------------------------------------------

    ts = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    sections: list[str] = []
    sections.append(f"<!-- Generated by scripts/build_state_index.py at {ts}. Run #{run_number}. -->")
    sections.append("")

    # --- 1. Types in use ---
    type_rows = []
    for t_key in sorted(type_counts.keys()):
        type_rows.append([t_key, type_counts[t_key], _example_paths(type_examples[t_key])])
    sections.append("## Types in use")
    sections.append(_md_table(["type", "count", "example-paths"], type_rows))
    sections.append("")

    # --- 2. Tags in use ---
    tag_rows = sorted(tag_counts.items(), key=lambda x: -x[1])
    tag_table_rows = [[t, c, _example_paths(tag_examples[t])] for t, c in tag_rows]
    sections.append("## Tags in use")
    if tag_table_rows:
        sections.append(_md_table(["tag", "count", "example-paths"], tag_table_rows))
    else:
        sections.append(_md_table(["tag", "count", "example-paths"], []))
    sections.append("")

    # --- 3. File extensions in use ---
    ext_rows = sorted(ext_counts.items(), key=lambda x: -x[1])
    ext_table_rows = [[e, c, _example_paths(ext_examples[e])] for e, c in ext_rows]
    sections.append("## File extensions in use")
    sections.append(_md_table(["extension", "count", "example-paths"], ext_table_rows))
    sections.append("")

    # --- 4. Indexes ---
    idx_rows = [[r["path"], str(r["parent"]), r["children_count"], r["path"]] for r in index_files]
    sections.append("## Indexes")
    sections.append(_md_table(["index", "parent", "children-count", "path"], idx_rows))
    sections.append("")

    # --- 5. Folder histogram (analyst cluster + maturity detection) ---
    # Per folder: total .md count, dominant types, dominant tags/subjects, and a
    # maturity cell (newest/oldest age in days + aged-vs-recent split). The analyst
    # owns the thresholds — it reads the count-by-type, top-tags, and maturity
    # columns and applies its own relative-dominance + maturity model. Folders below
    # FOLDER_HISTOGRAM_MIN_NOTES are dropped so tiny folders do not bloat the table.


    def _fmt_maturity(ages: list[int]) -> str:
        """Render a folder's age profile: newest/oldest days + aged-vs-recent count.

        'aged' = older than AGED_THRESHOLD_DAYS. Returns '(undated)' when no file in
        the folder carries a parseable `date`.
        """
        if not ages:
            return "(undated)"
        newest = min(ages)
        oldest = max(ages)
        aged = sum(1 for a in ages if a > AGED_THRESHOLD_DAYS)
        recent = len(ages) - aged
        return (
            f"newest:{newest}d, oldest:{oldest}d, "
            f"aged(>{AGED_THRESHOLD_DAYS}d):{aged}, recent:{recent}"
        )


    folder_hist_rows: list[list[str]] = []
    for folder in sorted(folder_total_counts.keys()):
        total = folder_total_counts[folder]
        if total < FOLDER_HISTOGRAM_MIN_NOTES:
            continue  # min-size floor — skip tiny folders
        types_str = _fmt_counter(folder_type_hist[folder])
        tags_str = _fmt_counter(folder_tag_hist[folder]) or "(none)"
        maturity_str = _fmt_maturity(folder_ages_days[folder])
        folder_hist_rows.append([folder, total, types_str, tags_str, maturity_str])
    sections.append("## Folder histogram")
    sections.append(
        f"<!-- Folders with <{FOLDER_HISTOGRAM_MIN_NOTES} notes omitted. "
        f"'aged' = older than {AGED_THRESHOLD_DAYS}d. Thresholds for clustering "
        f"are owned by the analyst, not this snapshot. -->"
    )
    sections.append(_md_table(
        ["folder", "total-notes", "count-by-type", "top-tags", "maturity"],
        folder_hist_rows,
    ))
    sections.append("")

    # --- 6. Catch-all status ---
    sections.append("## Catch-all status")
    sections.append(_md_table(
        ["catch-all-folder", "stamped-count", "unstamped-count"],
        [[f, s, u] for f, s, u in catch_all_status_rows],
    ))
    sections.append("")

    # --- 7. Drift summary ---
    drift_rows = [
        ["type-not-canonical", len(type_not_canonical_examples), _example_paths(type_not_canonical_examples)],
        ["type-unused", len(type_unused), _example_paths(type_unused)],
    ]
    sections.append("## Drift summary")
    sections.append(_md_table(["concern", "count", "examples"], drift_rows))
    sections.append("")

    # --- 8. Orphan assets ---
    orphan_rows = [[r, s, m] for r, s, m in orphan_asset_rows]
    sections.append("## Orphan assets")
    sections.append(_md_table(["path", "size", "last-modified"], orphan_rows))
    sections.append("")

    # --- 9. Link graph summary ---
    link_graph_rows = [
        ["graph-orphan", len(graph_orphans), _example_paths(graph_orphans)],
        ["leaf-node", len(leaf_nodes), _example_paths(leaf_nodes)],
        ["well-connected", len(well_connected), _example_paths(well_connected)],
    ]
    sections.append("## Link graph summary")
    sections.append(_md_table(["category", "count", "example-paths"], link_graph_rows))
    sections.append("")

    # --- 10. Frontmatter coverage ---
    fm_cov_rows = []
    for field in ("type", "date", "tags", "reviewed", "uplink"):
        present = fm_field_present[field]
        missing = fm_field_missing[field]
        examples = _example_paths(fm_field_missing_examples[field])
        fm_cov_rows.append([field, present, missing, examples])
    sections.append("## Frontmatter coverage")
    sections.append(_md_table(["field", "present", "missing", "missing-examples"], fm_cov_rows))
    sections.append("")

    # --- 11. Uplink coverage ---
    uplink_rows = []
    for ctype in sorted(CANONICAL_TYPE_KEYS - {"index", "log"}):
        uplink_field = UPLINK_FIELD_MAP.get(ctype, "parent")
        has = uplink_type_has[ctype]
        no = uplink_type_no[ctype]
        uplink_rows.append([ctype, uplink_field or "(none)", has, no])
    sections.append("## Uplink coverage")
    sections.append(_md_table(["type", "uplink-field", "has-uplink", "no-uplink"], uplink_rows))
    sections.append("")

    # --- 12. Type × folder distribution ---
    dist_rows = sorted(
        [[folder, ttype, cnt] for (folder, ttype), cnt in folder_type_counts.items()],
        key=lambda r: (r[0], r[1]),
    )
    sections.append("## Type x folder distribution")
    sections.append(_md_table(["folder", "type", "count"], dist_rows))
    sections.append("")

    # --- 13. Review backlog ---
    backlog_rows = sorted(
        [[folder, cnt] for folder, cnt in review_backlog.items()],
        key=lambda r: -r[1],
    )
    sections.append("## Review backlog")
    sections.append(_md_table(["folder", "reviewed-false-count"], backlog_rows))
    sections.append("")

    # --- 14. Age distribution ---
    age_rows = sorted(age_buckets.items(), key=lambda x: (x[0] == "no-date", x[0]))
    sections.append("## Age distribution")
    sections.append(_md_table(["period", "file-count"], [[p, c] for p, c in age_rows]))
    sections.append("")

    # --- 15. File change log (only with --run-log) ---
    if args.run_log is not None:
        fix_rows = _parse_run_log_fixes(args.run_log)
        sections.append("## File change log")
        sections.append(_md_table(
            ["path", "action", "detail"],
            fix_rows if fix_rows else [],
        ))
        sections.append("")

    # --- 16. Lifecycle events ---
    if previous_files is not None:
        lifecycle_rows = (
            [["new-file", rel, ""] for rel in new_files] +
            [["gone-file", rel, ""] for rel in gone_files]
        )
        sections.append("## Lifecycle events")
        sections.append(_md_table(["event", "path", "notes"], lifecycle_rows))
        sections.append("")

    # --- 17. Control files ---
    sections.append("## Control files")
    sections.append(_md_table(
        ["file", "exists", "size-bytes", "last-modified", "notes"],
        control_rows,
    ))
    sections.append("")

    # ---------------------------------------------------------------------------
    # Open findings (state rows per CONFIG § Log files: code | target | count, here
    # rendered as the full `timestamp | actor | code | target | count` line). One
    # row per open finding the janitor/analyst reason over. Detections are
    # deterministic and snapshot-derivable; the janitor confirms before acting.
    # ---------------------------------------------------------------------------

    ACTOR = "build-state-index"


    def _types_needing_uplink() -> dict[str, str]:
        """type -> required uplink field, for any type whose CONFIG row lists one of
        parent/project/target in additional-frontmatter. Read from CONFIG, not
        hardcoded, so a new uplink-bearing type is covered without a code edit."""
        out: dict[str, str] = {}
        for key, trow in TYPES.items():
            for field in ("parent", "project", "target"):
                if field in trow.additional_frontmatter:
                    out[key] = field
                    break
        return out


    _UPLINK_BY_TYPE = _types_needing_uplink()

    # A reviewed note's plan/spec is stale when the linked file was modified after
    # the note's own `date`. Restrict the "upstream" link set to plan/spec/design/
    # format targets — the documents a review is taken against.
    _UPSTREAM_TYPES = {"plan", "design", "format", "reference", "research"}


    def _note_date_dt(fm: dict) -> Optional[datetime]:
        raw = (fm or {}).get("date")
        if raw is None:
            return None
        try:
            if hasattr(raw, "year"):
                return datetime(raw.year, getattr(raw, "month", 1),
                                getattr(raw, "day", 1), tzinfo=timezone.utc)
            s = str(raw).strip()
            return datetime(int(s[:4]), int(s[5:7]) if len(s) >= 7 else 1,
                            int(s[8:10]) if len(s) >= 10 else 1, tzinfo=timezone.utc)
        except Exception:
            return None


    # Findings as (code, target, value) tuples.
    open_findings: list[tuple[str, str, str]] = []

    # reviewed-stale evidence baseline: the previous snapshot's content hashes.
    # On the first hashed run the baseline is empty, so no reviewed-stale can
    # fire — change evidence has to be observed across two runs.
    prev_hashes = _read_previous_hashes(output_path)
    # Candidates collected as (note_rel, note_stem, target_stem, target_rel);
    # reciprocal pairs are suppressed after the loop.
    stale_candidates: list[tuple[str, str, str, str]] = []
    BULK_TOUCH_THRESHOLD = 10

    for record in fm_records:
        rel = record["rel_path"]
        fm = record.get("fm") or {}
        raw_type = fm.get("type")
        canonical_type = normalize_type(str(raw_type)) if raw_type else None

        # type-resolution: a type that does not normalize.
        if raw_type is not None and canonical_type is None:
            open_findings.append(("type-resolution", rel, str(raw_type)))

        # tag-resolution: a tag normalize_tag cannot map to ONE canonical candidate.
        # The kit's tag vocabulary is open (CONFIG § Tags), so a tag that is far from
        # every canonical tag is a legitimate new domain tag, NOT a finding, and a
        # tag within edit-distance 1 of exactly one canonical tag is auto-normalized
        # by audit's deterministic pass. The genuine adjudication case is an
        # AMBIGUOUS tag: within edit-distance 1 of two or more different canonical
        # tags, where normalize_tag's first-match resolution is a guess the user
        # should confirm (rename / add-alias / add-to-vocabulary / remove).
        tags_raw = fm.get("tags") or []
        if isinstance(tags_raw, str):
            tags_raw = [t.strip() for t in tags_raw.split(",") if t.strip()]
        if isinstance(tags_raw, list):
            from config_variables import levenshtein_le1, CANONICAL_TAG_KEYS
            for tag in tags_raw:
                tag_str = str(tag).strip().lower()
                if not tag_str or tag_str in CANONICAL_TAG_KEYS:
                    continue
                near = [c for c in CANONICAL_TAG_KEYS if levenshtein_le1(tag_str, c)]
                if len(near) >= 2:
                    open_findings.append(
                        ("tag-resolution", rel, f"{tag_str} ~ {'/'.join(sorted(near))}")
                    )

        # parent-missing: a typed note that requires an uplink but carries none.
        if canonical_type in _UPLINK_BY_TYPE:
            field = _UPLINK_BY_TYPE[canonical_type]
            val = fm.get(field)
            if not (val is not None and str(val).strip()):
                # Skip inbox drafts (the user has not filed them yet).
                if not rel.startswith(INBOX_FOLDER + "/"):
                    open_findings.append(("parent-missing", rel, field))

        # reviewed-stale: a reviewed:true note whose linked upstream (plan/spec)
        # CONTENT changed after the note's review. Three evidence gates, so
        # bulk re-touches and audit --apply runs no longer flood the finding:
        #   1. the upstream's body md5 differs from the previous snapshot's
        #      recorded hash (mtime alone is not change evidence);
        #   2. the upstream's mtime is newer than the note's `date`;
        #   3. the upstream's mtime is not shared by >= BULK_TOUCH_THRESHOLD
        #      files (a bulk-touch signature, not an edit).
        # Reciprocal pairs (A flags B and B flags A) are suppressed after the
        # loop — a real invalidation cannot be mutual.
        reviewed = fm.get("reviewed")
        if str(reviewed).lower() == "true":
            note_date = _note_date_dt(fm)
            if note_date is not None:
                for target in record.get("links", []):
                    tgt_path = basename_to_abspath.get(target)
                    if tgt_path is None or not tgt_path.exists():
                        continue
                    try:
                        t_rel = str(tgt_path.relative_to(VAULT_ROOT)).replace("\\", "/")
                    except ValueError:
                        continue
                    try:
                        t_text = tgt_path.read_text(encoding="utf-8", errors="replace")
                    except OSError:
                        continue
                    t_fm = _parse_frontmatter(t_text) or {}
                    if normalize_type(str(t_fm.get("type") or "")) not in _UPSTREAM_TYPES:
                        continue
                    # Gate 1 — content actually changed since the last run.
                    prev_hash = prev_hashes.get(t_rel)
                    cur_hash = content_hashes.get(t_rel)
                    if prev_hash is None or cur_hash is None or prev_hash == cur_hash:
                        continue
                    # Gate 2 — modified after the note's review date.
                    raw_mtime = file_mtimes.get(t_rel)
                    if raw_mtime is None:
                        continue
                    mtime = datetime.fromtimestamp(raw_mtime, tz=timezone.utc)
                    if mtime <= note_date:
                        continue
                    # Gate 3 — not part of a bulk touch.
                    if mtime_counts.get(int(raw_mtime), 0) >= BULK_TOUCH_THRESHOLD:
                        continue
                    stale_candidates.append((rel, Path(rel).stem, target, t_rel))

    # Reciprocal-pair suppression: A flags B while B flags A — a real upstream
    # invalidation cannot be mutual, so both are bulk-churn noise. Then keep
    # one finding per stale note (first surviving target).
    _flagged_pairs = {(n_stem, t_stem) for _r, n_stem, t_stem, _tr in stale_candidates}
    _noted: set[str] = set()
    for n_rel, n_stem, t_stem, _t_rel in stale_candidates:
        if (t_stem, n_stem) in _flagged_pairs:
            continue  # reciprocal pair — suppress both directions
        if n_rel in _noted:
            continue  # one finding per stale note
        _noted.add(n_rel)
        open_findings.append(("reviewed-stale", n_rel, t_stem))

    # ---------------------------------------------------------------------------
    # dangling-link: an active-source note's wikilink whose basename resolves to
    # no file anywhere in the vault. Detection is deterministic and owned here so
    # the snapshot work-list carries a standing count the janitor drives to zero;
    # the janitor performs the repair by reading the line (janitor SKILL § 2).
    # Uses the kit's fenced- and inline-code-aware extract_wikilinks (helper_links)
    # so template and example links never count. Inbox drafts and the archive are
    # skipped; a file opts out with compliance_exceptions: [body-wikilink-resolution].
    # ---------------------------------------------------------------------------

    # Existing-file basename index over the WHOLE vault, matching Obsidian's
    # case-insensitive basename resolution: a link resolving to any file — an
    # archived copy, a differently-cased name, or a file inside an asset folder —
    # is not a ghost. Only dot-dirs (tooling/config) are excluded; asset folders
    # are kept here because Obsidian still resolves links into them.
    all_basenames_lower: set[str] = set()
    for _dp, _dn, _fn in os.walk(VAULT_ROOT):
        _dn[:] = [d for d in _dn if not is_excluded_dir(d)]
        for _f in _fn:
            all_basenames_lower.add(Path(_f).stem.lower())

    _DANGLING_EXEMPT = {"body-wikilink-resolution", "dangling-link"}
    # Confirmed-knowledge sources surface first in the detail rows.
    _DANGLING_TYPE_RANK = {"reference": 0, "index": 1, "design": 1, "format": 1, "voice": 1}
    dangling_findings: list[tuple[int, str, str]] = []
    for record in fm_records:
        rel = record["rel_path"]
        if rel.startswith(INBOX_FOLDER + "/"):
            continue  # inbox drafts are expected to dangle until filed
        fm = record.get("fm") or {}
        exempt = fm.get("compliance_exceptions") or []
        if isinstance(exempt, str):
            exempt = [exempt]
        if any(str(e).strip() in _DANGLING_EXEMPT for e in exempt):
            continue
        ctype = normalize_type(str(fm.get("type") or "")) or ""
        rank = _DANGLING_TYPE_RANK.get(ctype, 5)
        for ghost in record.get("helper_links", []):
            if ghost.lower() in all_basenames_lower:
                continue
            dangling_findings.append((rank, rel, ghost))

    dangling_findings.sort(key=lambda t: (t[0], t[1], t[2]))
    for _rank, rel, ghost in dangling_findings:
        open_findings.append(("dangling-link", rel, ghost))

    # Scattered loose-asset duplicates / divergences (computed above).
    open_findings.extend(asset_dedup_findings)

    # Fold in caller-supplied findings (e.g. audit.py's run-scoped inference and
    # queue findings), parsed from a `code | target | value`-per-line file.
    if args.findings is not None and args.findings.exists():
        try:
            for line in args.findings.read_text(encoding="utf-8").splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2 and parts[0]:
                    code = parts[0]
                    target = parts[1]
                    value = parts[2] if len(parts) >= 3 else ""
                    open_findings.append((code, target, value))
        except OSError:
            pass

    # Count rollup (code | scope | count) then per-file detail rows, in the kit's
    # state-log line shape.
    def _scope_of(target: str) -> str:
        t = target.strip()
        return t.split("/", 1)[0] if "/" in t else (t or "-")


    finding_counts: dict[tuple[str, str], int] = defaultdict(int)
    for code, target, _v in open_findings:
        finding_counts[(code, _scope_of(target))] += 1

    finding_count_lines = [
        f"{ts} | {ACTOR} | {code} | {scope} | {n}"
        for (code, scope), n in sorted(finding_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    finding_detail_lines = [
        f"{ts} | {ACTOR} | {code} | {target} | {value or '-'}"
        for code, target, value in open_findings
    ]

    # --- 18. Content hashes — the reviewed-stale change-evidence baseline ---
    # One row per vault .md: body md5 (frontmatter excluded). Read back by the
    # next run; an upstream counts as changed only when its hash moved.
    sections.append("## Content hashes")
    sections.append(
        "<!-- Body md5 per file (frontmatter excluded). Baseline for the "
        "reviewed-stale detection; do not hand-edit. -->"
    )
    sections.append(_md_table(
        ["path", "body-md5"],
        [[f"`{rel}`", content_hashes[rel]] for rel in sorted(content_hashes)],
    ))
    sections.append("")

    sections.append("## Open findings")
    sections.append(
        "<!-- State rows (CONFIG § Log files): code | target | count. The count "
        "rollup is first; the per-file detail rows follow. Findings are "
        "deterministic detections; the janitor confirms before acting. -->"
    )
    if finding_count_lines:
        sections.append("\n".join(finding_count_lines))
        if finding_detail_lines:
            sections.append("")
            sections.append("\n".join(finding_detail_lines))
    else:
        sections.append("(no open findings)")
    sections.append("")

    content = "\n".join(sections)

    # ---------------------------------------------------------------------------
    # Atomic write
    # ---------------------------------------------------------------------------

    tmp = output_path.with_suffix(".md.tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, output_path)

    print(f"Vault-State-Index.md written: {output_path}")
    print(f"  Run #{run_number} at {ts}")
    print(f"  Files walked: {len(md_files_all)} .md + {len(non_md_files)} non-md")
    print(f"  Open findings: {len(open_findings)} "
          f"({len(finding_counts)} distinct code/scope rollups)")

    # No history file (CONFIG § Log files: two artifacts under <logs>, no per-run
    # files). The append-only event ledgers carry the longitudinal record; the
    # snapshot above is overwritten each run.

    print("Done.")



if __name__ == "__main__":
    main()
