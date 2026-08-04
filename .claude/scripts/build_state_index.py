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
    invalidation cannot be mutual) are suppressed as noise. A design, voice, or
    format standard upstream never fires: standards evolve additively and their
    linkers cite the principle, not a spec. The janitor reads the note against
    the upstream change and confirms.
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
    the redundant copies, keeping the asset-home (`Assets/`) canonical.
  - `diverged-asset` — the same loose-asset filename carrying DIFFERENT content
    at >1 path under one owning root; a version ambiguity. The janitor picks the
    canonical copy (version token, date, or date of related use in a plan/log)
    and archives the rest, queueing only a genuine scope fork.
  - `status-coherence` — a `status`/`reviewed`/location combination CONFIG
    § Status does not allow: `status: draft` (unreviewed, in `<inbox>`) on a
    filed note, or `reviewed: false` outside the inbox. Detect-only; the counts
    and a bounded sample also render in ## Status coherence.
  - `claim-verified` / `claim-false` / `claim-unparseable` / `claim-deferred` —
    the claim check (CONFIG § Log files). A log line asserting a file's state
    carries `claim: <path> | <expected condition>` in its value cell; this run
    re-derives the condition FROM DISK. Conditions: `exists`, `absent`,
    `contains:<literal>`, `hash:<md5-prefix>` (full-file or body md5). A
    `claim-false` row is the retraction opener. Scope is the event-log HEADS
    under `<logs>` (one `<dir>/<dir>.md` per log folder — janitor, filing,
    action, analyst, and the orchestration ledger); rotated segments are
    history and stay out. An unparseable condition degrades one row
    (`claim-unparseable`), never the run, and a line stamped at or after this
    run defers (`claim-deferred`) — a claimant never certifies itself.
  - `near-duplicate` — the redundancy surface's pair flag. (Its two companion
    codes, `near-duplicate-skipped` and `near-duplicate-truncated`, report how
    the detector RAN and belong to ## Detector status, not here — see
    DETECTOR_STATUS_CODES.) Cosine similarity over the vault-search
    daemon's OWN chunk embeddings (read READ-ONLY out of its index.db
    `chunk_embeddings`, since the daemon's HTTP surface exposes no pair
    endpoint), file vector = L2-normalized mean of its chunk vectors, threshold
    0.85 (tier-2 calibration, 2026-07-25). Cost is bounded to the new/changed
    files of this run's content-hash delta against the whole corpus, and the
    report is capped at 20 pairs with a truncation row. When the daemon is not
    serving the detector SKIPS AND LOGS — no fallback similarity is ever
    substituted. The pairs also render in ## Near-duplicate as merge candidates
    for the analyst; nothing acts on them automatically.

Settled conventions
-------------------
`<logs>/Conventions.md` (CONFIG § Log files) is the structured suppression
store: one line per settled convention, `code | scope predicate | evidence |
date`. A finding matching a row's code AND scope predicate is suppressed at
detect time and counted in ## Convention store — recorded, never silently
dropped. A malformed store line is skipped with a warning row in the same
section. The store replaces the per-case hand patches this script used to
carry; a new settled convention lands as a store row, not a code edit.

A caller (audit.py) may hand additional, run-scoped findings to fold into the
same ## Open findings section via --findings, so the snapshot stays the one
work list and no second snapshot file is written.

NO history file is written: CONFIG § Log files forbids per-run files under
<logs>; the append-only ledgers carry the longitudinal record.

The ## Folder histogram feeds the analyst's cluster detection — per folder,
total note count, count-by-type, dominant tags, and a maturity split. This
script provides DATA only; the analyst owns the thresholds. A min-size floor
(FOLDER_HISTOGRAM_MIN_NOTES) keeps tiny folders out of the histogram.

TWO age series are emitted, labelled by their evidence. ## Folder histogram's
`maturity` cell reads frontmatter `date` — the date the note CLAIMS, which a
migration rewrites, so on a migrated corpus it measures migration age.
## Folder maturity (filesystem) reads the same folders' files from NTFS
creation time — each file's ARRIVAL in this vault, floored by mtime so a moved
file keeps the older of the two stamps — and reports days since the NEWEST
member arrived, because a folder is as settled as its youngest member. A
maturity gate that needs real dwell time reads that series. Both are present
every run; neither replaces the other.

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
import fnmatch
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

from functools import lru_cache

from config_variables import (
    FOLDER_ROUTING,
    TYPES,
    SKILL_SLUGS,
    CANONICAL_TYPE_KEYS,
    SCAN_EXCLUDE_DIRS,
    ASSET_HOME_DIRS,
    _folder_by_semantic,
    is_excluded_dir,
    is_asset_folder,
    normalize_tag,
    token_path,
)
from normalize_type import normalize_type
from wikilink_helpers import extract_wikilinks
from frontmatter_helpers import write_text

# ---------------------------------------------------------------------------
# Shared kit helpers (one implementation, imported by audit.py) — end the
# duplication the code review flagged (Kit-Code-Quality-Plan § Duplication).
# `is_hands_off`/`in_asset_folder` carry the janitor's richer semantics
# (asset-gate-first, vault-root loose-file exemption) so the snapshot builder
# and the janitor gate every path through the SAME rules; `scope_of` is the one
# top-level-folder rollup both use for their state-log lines.
# ---------------------------------------------------------------------------


def scope_of(target: str) -> str:
    """Top-level folder of a vault-relative path, for the count rollup."""
    t = target.strip()
    return t.split("/", 1)[0] if "/" in t else (t or "-")


_LEGACY_NUM_PREFIX = re.compile(r"^\d{2}-")


def is_folder_cover(path: Path) -> bool:
    """True when `path` is its folder's cover note — the folder-note whose stem
    equals the containing folder name, OR a legacy `NN-` numbered form of it
    (CONFIG § Numbering keeps a legacy `00-`/`01-` prefixed cover acting as the
    folder index). ONLY a folder cover carries the every-child index contract
    ("only a root carries one"); a curated sub-index living inside a folder does
    not. The one predicate for all three consumers (index-vs-disk diff, the
    cover-stale currency gate, and audit's folder-index lookup)."""
    folder = path.parent.name
    stem = path.stem
    if not folder:
        return False
    if stem == folder:
        return True
    stem_nonum = _LEGACY_NUM_PREFIX.sub("", stem)
    folder_nonum = _LEGACY_NUM_PREFIX.sub("", folder)
    return bool(stem_nonum) and stem_nonum in (folder, folder_nonum)


@lru_cache(maxsize=None)
def _dir_is_asset(dir_path_str: str) -> bool:
    return is_asset_folder(Path(dir_path_str))


def in_asset_folder(path: Path, vault_root: Path) -> bool:
    """True when any ancestor directory below the vault root is a classified
    asset folder (CONFIG § Asset folders). The FIRST gate on every stamp,
    normalize, rename, or index decision: nothing inside an asset folder is
    touched. The walk prunes asset folders on descent; this covers every other
    entry point (direct path handling, index registration, move targets)."""
    cur = path if path.is_dir() else path.parent
    while True:
        try:
            cur.relative_to(vault_root)
        except ValueError:
            return False
        if cur == vault_root:
            return False
        if _dir_is_asset(str(cur)):
            return True
        cur = cur.parent


# ---------------------------------------------------------------------------
# Settled-convention store (CONFIG § Log files) — `<logs>/Conventions.md`
#
# One line per settled convention: `code | scope predicate | evidence | date`.
# A finding whose code matches a row's code AND whose facts satisfy the row's
# scope predicate is suppressed at detect time and counted, so a class the
# analyst or janitor settled by evidence stops re-firing every run without a
# per-case patch in this file.
#
# Scope-predicate grammar — clauses joined by ` + ` (all must hold):
#   path:<glob>[,<glob>…]     the finding's target, matched case-sensitively
#                             with fnmatch (`*` spans `/`)
#   scope:<name>[,<name>…]    the target's top-level folder
#   type:<type>[,<type>…]     the target note's canonical type
#   cover:true|false          the target is its folder's cover note
#   in-asset-folder:true|false the target sits inside a classified asset folder
#   upstream-type:<type>,…    reviewed-stale only: the linked upstream's type
#   upstream-status:<v>,…     reviewed-stale only: the linked upstream's status
#   value~<regex>             the finding's value matches the regex
#   path~<regex> / target~<regex>   the target matches the regex
# A comma separates alternatives WITHIN a clause (any one satisfies it); the
# pipe stays the field separator, so a regex uses a character class in place of
# alternation.
# ---------------------------------------------------------------------------

_CONVENTION_KEYS = {
    "path", "target", "scope", "type", "cover", "in-asset-folder",
    "upstream-type", "upstream-status", "value",
}
_PATH_KEYS = {"path", "target"}

# ---------------------------------------------------------------------------
# Detector-status codes — informational, never an open finding.
#
# An open finding names a vault file and proposes a change to it: the janitor
# confirms and acts. A detector-status row names the SNAPSHOT ITSELF and
# reports how a detector ran — it skipped for a stated reason, or it truncated
# its report at a cap. There is nothing to act on and nothing to confirm, so
# these rows report in ## Detector status and stay out of ## Open findings.
#
# Why this matters beyond tidiness: `near-duplicate-skipped` fires on EVERY
# fresh install, because the detector reads the vault-search daemon's own
# embeddings and a just-scaffolded vault has no index.db by definition.
# Counting that row as an open finding made a pristine install report itself
# unclean, and its skip reason names the daemon's `.claude/vault-search/`
# index path — a diagnostic pointing at where the index would be, not a
# proposal to touch a kit file.
# ---------------------------------------------------------------------------
DETECTOR_STATUS_CODES = frozenset({
    "near-duplicate-skipped",
    "near-duplicate-truncated",
})


def parse_convention_predicate(pred: str):
    """Parse a scope predicate into clauses.

    Returns (clauses, error). `clauses` is a list of (key, op, value) where op
    is ':' (alternatives list) or '~' (compiled regex). `error` is None on
    success, or a one-line reason the caller reports as a store warning.
    """
    clauses: list[tuple[str, str, object]] = []
    for part in pred.split(" + "):
        part = part.strip()
        if not part:
            continue
        if "~" in part and part.split("~", 1)[0].strip().lower() in _CONVENTION_KEYS:
            key, raw = part.split("~", 1)
            op = "~"
        elif ":" in part:
            key, raw = part.split(":", 1)
            op = ":"
        else:
            return [], f"clause '{part[:40]}' is neither key:value nor key~regex"
        key = key.strip().lower()
        raw = raw.strip()
        if key not in _CONVENTION_KEYS:
            return [], f"unknown predicate key '{key}'"
        if not raw:
            return [], f"predicate key '{key}' has no value"
        if op == "~":
            try:
                clauses.append((key, "~", re.compile(raw)))
            except re.error as exc:
                return [], f"bad regex for '{key}': {exc}"
        else:
            alts = [a.strip() for a in raw.split(",") if a.strip()]
            if not alts:
                return [], f"predicate key '{key}' has no value"
            clauses.append((key, ":", alts))
    if not clauses:
        return [], "empty predicate"
    return clauses, None


_CONVENTION_LABELS = ["code", "scope predicate", "evidence", "date"]


def parse_convention_store(text: str) -> tuple[list[dict], list[str]]:
    """Parse `<logs>/Conventions.md` into rows + warnings.

    The store carries a legal header — prose describing the grammar, then the
    `code | scope predicate | evidence | date` label row, mirroring the shape of
    `<logs>/fix-verification.md`. That label row IS the start-of-data marker:
    everything above it is header and skipped silently, so documenting the store
    inside the store never costs a warning. A store with no label row is read
    from its first line, so a bare rows-only file still parses.

    Below the marker a malformed data line is skipped LOUDLY — it returns a
    warning the caller renders in the snapshot — and never raises, so a
    hand-edit typo degrades one row instead of the run.
    """
    rows: list[dict] = []
    warnings: list[str] = []
    lines = text.splitlines()
    start = 0
    for i, raw_line in enumerate(lines):
        if [f.strip().lower() for f in raw_line.split("|")] == _CONVENTION_LABELS:
            start = i + 1
            break
    for lineno, raw_line in enumerate(lines[start:], start + 1):
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith("<!--") or line.startswith(">"):
            continue  # comment
        if set(line) <= {"-", "|", " ", ":"}:
            continue  # markdown separator rule
        fields = [f.strip() for f in line.split("|")]
        if [f.lower() for f in fields] == _CONVENTION_LABELS:
            continue  # a repeated label row
        if len(fields) != 4 or not fields[0] or not fields[1]:
            warnings.append(
                f"line {lineno}: expected 4 pipe-separated fields "
                f"(code | scope predicate | evidence | date), got {len(fields)}: "
                f"{line[:80]}"
            )
            continue
        code, predicate, evidence, date = fields
        clauses, err = parse_convention_predicate(predicate)
        if err:
            warnings.append(f"line {lineno}: {err} — in `{predicate[:60]}`")
            continue
        rows.append({
            "code": code, "predicate": predicate, "clauses": clauses,
            "evidence": evidence, "date": date, "line": lineno,
        })
    return rows, warnings


def convention_matches(row: dict, code: str, target: str, value: str,
                       ctx: dict) -> bool:
    """True when a finding falls inside a store row's settled scope."""
    if row["code"] != code:
        return False
    for key, op, expected in row["clauses"]:
        if key in _PATH_KEYS:
            hay = target
        elif key == "scope":
            hay = scope_of(target)
        elif key == "value":
            hay = value or ""
        else:
            hay = str(ctx.get(key, "") or "")
        if op == "~":
            if not expected.search(hay):
                return False
        elif key in _PATH_KEYS:
            if not any(fnmatch.fnmatchcase(hay, g) for g in expected):
                return False
        else:
            if hay.strip().lower() not in {a.lower() for a in expected}:
                return False
    return True


def build_hands_off_patterns() -> list[tuple[str, "re.Pattern[str]"]]:
    """Build regex patterns from FOLDER_ROUTING hands_off_patterns, per folder.

    Inbox skill-container patterns (those using a `<name>` placeholder, e.g.
    `*-research/`) are expanded only for slugs that are real skill containers
    (SKILL_SLUGS with inbox_container=True), so a stray inbox subfolder that
    does not match a known skill slug reaches the unprocessed-content detector
    rather than being silently exempted.
    """
    _container_slugs: set[str] = {
        slug for slug, row in SKILL_SLUGS.items() if row.inbox_container
    }

    patterns: list[tuple[str, "re.Pattern[str]"]] = []
    inbox_folder = _folder_by_semantic("inbox")

    for folder_str, row in FOLDER_ROUTING.items():
        is_inbox_folder = (folder_str == inbox_folder)
        for raw_pattern in row.hands_off_patterns:
            raw = raw_pattern.strip()
            if not raw:
                continue

            if is_inbox_folder and "<" in raw and ">" in raw and raw.endswith("/"):
                folder_prefix = re.escape(folder_str)
                prefix_part, suffix_part = re.split(r"<[^>]+>", raw, maxsplit=1)
                suffix_part = suffix_part.rstrip("/")
                for slug in _container_slugs:
                    literal = prefix_part + slug + suffix_part
                    escaped_literal = re.escape(literal).rstrip("/")
                    regex = rf"^{folder_prefix}/{escaped_literal}/"
                    try:
                        patterns.append((raw, re.compile(regex)))
                    except re.error:
                        continue
                continue

            if "<" in raw and ">" in raw:
                parts = re.split(r"<[^>]+>", raw)
                escaped = "[^/]+".join(re.escape(p) for p in parts)
            else:
                escaped = re.escape(raw)
            folder_prefix = re.escape(folder_str)
            if raw == "*":
                regex = rf"^{folder_prefix}/"
            elif raw.endswith("/"):
                regex = rf"^{folder_prefix}/{escaped.rstrip('/')}/"
            else:
                regex = rf"^{folder_prefix}/{escaped}(?:/|$)"
            try:
                patterns.append((raw, re.compile(regex)))
            except re.error:
                continue
    return patterns


def is_hands_off(path: Path, vault_root: Path, patterns) -> bool:
    """Return True if the path is hands-off (CONFIG § Folders, § Asset folders).

    One predicate for the whole kit, carrying the janitor's richer semantics:
    - anything inside a classified asset folder — checked FIRST;
    - any dot-directory segment (`.claude`, `.git`, `.obsidian`, `.trash`);
    - any LOOSE FILE at the vault root — the user's draft space, moved/renamed/
      stamped by no pass;
    - any folder's compiled hands_off_patterns match;
    - `__pycache__` segments and `*.pyc`.
    """
    if in_asset_folder(path, vault_root):
        return True
    if any(is_excluded_dir(part) for part in path.parts):
        return True
    if path.parent == vault_root and not path.is_dir():
        return True
    try:
        rel = str(path.relative_to(vault_root)).replace("\\", "/")
    except ValueError:
        return False
    for _raw, pat in patterns:
        if pat.search(rel):
            return True
    for part in path.parts:
        if part == "__pycache__":
            return True
    if path.suffix == ".pyc":
        return True
    return False


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
    parser.add_argument(
        "--baseline",
        type=Path,
        default=None,
        help="Path to the change-evidence baseline snapshot (the PREVIOUS run's "
             "Vault-State-Index.md). reviewed-stale and ## Lifecycle events are "
             "computed against it. Defaults to the output snapshot itself. The "
             "janitor passes its carried pre-pass baseline here so the persisted "
             "end-of-run snapshot compares against the previous run, not the "
             "pre-pass write it just made (double-snapshot fix).",
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

    INBOX_FOLDER = _folder_by_semantic("inbox")    # e.g. "Inbox"
    ARCHIVE_FOLDER = _folder_by_semantic("archive")  # e.g. "Archive"

    def _semantic_or(needle: str, fallback: str) -> str:
        try:
            return _folder_by_semantic(needle)
        except Exception:
            return fallback

    PROJECTS_FOLDER = _semantic_or("projects", "Projects")
    AREAS_FOLDER = _semantic_or("areas", "Areas")
    REFERENCE_FOLDER = _semantic_or("reference", "References")
    SNIPPETS_FOLDER = _semantic_or("snippets", "Snippets")

    # ---------------------------------------------------------------------------
    # Output paths — the canonical <logs> root (NOT under an agent folder)
    # ---------------------------------------------------------------------------

    LOGS_REL = token_path("logs", f"{ARCHIVE_FOLDER}/Logs")
    output_dir = VAULT_ROOT / LOGS_REL
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "Vault-State-Index.md"

    # The change-evidence baseline for reviewed-stale and ## Lifecycle events:
    # the previous run's snapshot. Defaults to the output snapshot itself (the
    # standalone/analyst case), but the janitor passes --baseline pointing at
    # the snapshot it captured BEFORE its pre-pass overwrote output_path, so the
    # persisted end-of-run snapshot compares against the previous RUN, not the
    # seconds-old pre-pass write (double-snapshot fix).
    baseline_path = args.baseline if args.baseline is not None else output_path

    # ---------------------------------------------------------------------------
    # Control files
    # ---------------------------------------------------------------------------

    CONTROL_FILES = [
        VAULT_ROOT / LOGS_REL / "Vault-State-Index.md",
        VAULT_ROOT / token_path("user-queue", f"{INBOX_FOLDER}/User-Queue.md"),
        VAULT_ROOT / LOGS_REL / "Sync-Log.md",
    ]

    # ---------------------------------------------------------------------------
    # Hands-off filtering — bind the shared module-level predicate to this run's
    # vault root, so the snapshot builder and the janitor gate every path through
    # the SAME rules (one implementation; ends the audit.py / build_state_index.py
    # drift the code review flagged). The richer semantics (asset-gate-first,
    # vault-root loose-file exemption, full-rel-path pattern match) now apply here
    # too.
    # ---------------------------------------------------------------------------

    _HANDS_OFF_PATTERNS = build_hands_off_patterns()

    def _is_hands_off(path: Path) -> bool:
        return is_hands_off(path, VAULT_ROOT, _HANDS_OFF_PATTERNS)


    # ---------------------------------------------------------------------------
    # Frontmatter extraction
    # ---------------------------------------------------------------------------

    _FM_FENCE = re.compile(r"^---\s*$", re.MULTILINE)


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
    # Reference graph: the same links PLUS `![[…]]` embeds. The link graph reads
    # navigational edges; the inbound-link map reads REFERENCES, and an embedded
    # asset (a figure beside its note) is referenced. Kept separate so the graph
    # metrics (leaf-node, index children) stay on the plain-link reading.
    reference_links: dict[str, list[str]] = {}  # rel_path -> [target_basenames]

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
    # Creation time (Windows: st_ctime IS the creation timestamp) — each file's
    # ARRIVAL in this vault, and the evidence behind ## Folder maturity
    # (filesystem). mtime moves with every edit, so it reads a long-settled
    # folder as young; arrival never moves backwards under editing.
    file_ctimes: dict[str, float] = {}

    # Folder × type
    folder_type_counts: dict[tuple[str, str], int] = defaultdict(int)

    # Per-folder aggregates for the analyst's cluster detection. Folder is the
    # immediate containing folder relative to the vault root (e.g.
    # "Projects/Glass-Fracture/Sessions"). For each folder we track the total .md
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

            # Wikilinks (outbound) — the kit's one code-aware extractor (fenced
            # and inline code stripped), replacing the deleted legacy variant.
            links = extract_wikilinks(text)
            outbound_links[rel] = links
            reference_links[rel] = extract_wikilinks(text, include_embeds=True)

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
                _st = path.stat()
                _mt = _st.st_mtime
                file_mtimes[rel] = _mt
                mtime_counts[int(_mt)] += 1
                file_ctimes[rel] = _st.st_ctime
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
    # Second age series — filesystem evidence
    # ---------------------------------------------------------------------------
    # The ## Folder histogram maturity cell reads frontmatter `date`: the date a
    # note CLAIMS. A migration rewrites that field across the corpus, so on a
    # migrated vault the frontmatter series measures migration age and a maturity
    # gate keyed to it can never fire. This series measures the same folders from
    # NTFS CREATION time — the arrival date of each file in this vault. mtime is
    # the wrong evidence for maturity: every edit resets it, so a folder settled
    # for months reads days old and the gate can never fire on it either.
    #
    # Creation time carries one artifact of its own: a whole-tree MOVE recreates
    # the file, so a filed or reorganized note reads born-today while its mtime
    # still remembers the content. Arrival therefore takes the EARLIER of the two
    # stamps — the earliest moment the filesystem can attest the file existed.
    # That makes the series older-or-equal than the mtime it replaces in every
    # folder, by construction, instead of trading one young-reading artifact for
    # another.
    #
    # A folder's maturity is days since its NEWEST member arrived — a folder is
    # as settled as its youngest member. Both series ship every run, each
    # labelled by its evidence; the analyst picks the one its gate needs.
    folder_fs_ages_days: dict[str, list[int]] = defaultdict(list)
    for _rel, _ct in file_ctimes.items():
        _parts = _rel.split("/")
        _containing = "/".join(_parts[:-1]) if len(_parts) > 1 else (_parts[0] if _parts else "")
        _arrival_ts = min(_ct, file_mtimes.get(_rel, _ct))
        try:
            _seen = datetime.fromtimestamp(_arrival_ts, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        folder_fs_ages_days[_containing].append(max(0, (now_utc - _seen).days))

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

    # Built from the EMBED-AWARE reference graph: `![[img.png]]` is a reference
    # to img.png, so an embedded asset carries an inbound count instead of
    # reading as an orphan. Embeds of .md notes count the same way — a
    # transcluded note is referenced.
    inbound_counts: dict[str, int] = defaultdict(int)
    for rel, targets in reference_links.items():
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

    # One finding per multi-copy group; canonical hint = the asset-home copy
    # when present (the Centralize standard's home — CONFIG § Asset folders
    # `home` trigger, legacy `02-Assets` included), else the first path.
    asset_dedup_findings: list[tuple[str, str, str]] = []
    for (root, name), copies in sorted(_asset_groups.items()):
        if len(copies) < 2:
            continue
        copies = sorted(copies)
        canonical = next(
            (rel for rel, _h, _s, _m in copies
             if any(f"/{home}/" in f"/{rel}" for home in ASSET_HOME_DIRS)),
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

    previous_files = _read_previous_files(baseline_path)
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
            # Queue-file large check — a queue past 50 KiB needs a sweep.
            if "Queue" in cf.name and size > 50 * 1024:
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


    def _fmt_fs_maturity(arrival_ages: list[int]) -> str:
        """Render a folder's ARRIVAL profile for ## Folder maturity (filesystem).

        `maturity` leads the cell because it is the gate value: days since the
        newest member arrived — the smallest arrival age, since a folder is only
        as settled as its youngest member. `oldest` carries the first arrival for
        context. Returns '(unmeasured)' when no file in the folder yielded a
        creation timestamp.
        """
        if not arrival_ages:
            return "(unmeasured)"
        maturity = min(arrival_ages)
        oldest = max(arrival_ages)
        aged = sum(1 for a in arrival_ages if a > AGED_THRESHOLD_DAYS)
        recent = len(arrival_ages) - aged
        return (
            f"maturity:{maturity}d, oldest:{oldest}d, "
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

    # --- 5b. Folder maturity, filesystem evidence (the SECOND age series) ---
    # Same folders, same min-size floor — measured from NTFS creation time (each
    # file's arrival in this vault) instead of frontmatter `date`. A corpus whose
    # `date` fields were written by a migration reads uniformly young in
    # ## Folder histogram; this table reads how long the folder has actually held
    # its content. `maturity` is days since the NEWEST member arrived: a folder is
    # as settled as its youngest member, so one fresh drop makes the folder young
    # again. Neither series replaces the other: a maturity gate names the series
    # it reads.
    folder_fs_rows: list[list[str]] = []
    for folder in sorted(folder_total_counts.keys()):
        total = folder_total_counts[folder]
        if total < FOLDER_HISTOGRAM_MIN_NOTES:
            continue  # same floor as ## Folder histogram, so rows line up
        folder_fs_rows.append([
            folder, total, len(folder_fs_ages_days[folder]),
            _fmt_fs_maturity(folder_fs_ages_days[folder]),
        ])
    sections.append("## Folder maturity (filesystem)")
    sections.append(
        f"<!-- SECOND age series. Evidence: NTFS creation times; maturity = days "
        f"since newest arrival. A moved file's creation time is reset by the "
        f"move, so arrival takes the EARLIER of creation time and mtime — the "
        f"earliest the filesystem can attest the file existed, never younger "
        f"than the mtime this series replaces. NOT frontmatter `date` — read "
        f"this one when maturity means how long the folder has held its content, "
        f"and ## Folder histogram's `maturity` cell when it means the date a note "
        f"claims. `maturity` is the gate value (days since the youngest member "
        f"arrived); `oldest` is the first arrival. Same folder set and same "
        f"<{FOLDER_HISTOGRAM_MIN_NOTES}-note floor as ## Folder histogram; "
        f"'aged' = arrived more than {AGED_THRESHOLD_DAYS}d ago. Thresholds stay "
        f"the analyst's. -->"
    )
    sections.append(_md_table(
        ["folder", "total-notes", "files-measured", "fs-maturity"],
        folder_fs_rows,
    ))
    sections.append("")

    # --- 5c. Cluster and index candidates (analyst adjudicates rows) ---
    # Mechanizes the arithmetic half of the analyst's Cluster-detection method
    # so the model adjudicates candidate rows instead of recomputing
    # share-of-folder and age-span each run. Thresholds mirror the analyst
    # SKILL's stated defaults: Size >= 25 notes, Dominance >= 40% relative share
    # by dominant tag, Maturity oldest arrival >= 60 days (the filesystem
    # series). A row is a CANDIDATE — the judgment (split or not, index or not)
    # stays the analyst's; the janitor takes no action on these codes.
    CLUSTER_MIN_NOTES = 25
    CLUSTER_MIN_SHARE = 0.40
    CLUSTER_MIN_OLDEST_DAYS = 60
    INDEX_CANDIDATE_MIN_FOLDERS = 3
    INDEX_CANDIDATE_MIN_NOTES = 15
    # open_findings is minted later in this function; candidate rows collect
    # here and fold in at that definition.
    candidate_findings: list[tuple[str, str, str]] = []

    def _tag_titlecase(tag: str) -> str:
        return "-".join(w.capitalize() for w in str(tag).split("-") if w)

    cluster_rows: list[list[str]] = []
    for folder in sorted(folder_total_counts.keys()):
        if folder.startswith((ARCHIVE_FOLDER, INBOX_FOLDER)):
            continue
        total = folder_total_counts[folder]
        if total < CLUSTER_MIN_NOTES:
            continue
        tag_hist = folder_tag_hist.get(folder)
        if not tag_hist:
            continue
        top_tag, top_n = max(tag_hist.items(), key=lambda kv: kv[1], default=(None, 0))
        if not top_tag or (top_n / total) < CLUSTER_MIN_SHARE:
            continue
        # A sub-TOPIC is a topical tag: type names and provenance tags
        # (CONFIG § Tags) dominate their homes by construction and split nothing.
        from config_variables import CANONICAL_TAG_KEYS as _CC_PROVENANCE
        if str(top_tag) in _CC_PROVENANCE or normalize_type(str(top_tag)):
            continue
        arrivals = folder_fs_ages_days.get(folder) or []
        oldest = max(arrivals) if arrivals else 0
        if oldest < CLUSTER_MIN_OLDEST_DAYS:
            continue
        # A dominant tag that already has its subfolder is a settled split.
        if f"{folder}/{_tag_titlecase(top_tag)}" in folder_total_counts:
            continue
        share_pct = round(100.0 * top_n / total, 1)
        cluster_rows.append([folder, total, top_tag, f"{share_pct}%", f"{oldest}d"])
        candidate_findings.append((
            "cluster-candidate", folder,
            f"tag={top_tag} share={share_pct}% n={total} oldest={oldest}d",
        ))

    # Index candidates: a tag DOMINANT in several folders with no index note
    # named for it — the mechanical face of the analyst's cross-corpus
    # grouping test ("a shared dominant tag across projects and areas").
    # Presence alone is not kinship: only each folder's top tag counts, and
    # type-name and provenance tags are excluded as above. Existence of
    # `<Tag>.md` anywhere (cover or standalone index) settles the class.
    from config_variables import CANONICAL_TAG_KEYS as _IC_PROVENANCE
    index_basenames = {Path(r["path"]).stem.lower() for r in index_files}
    tag_folder_spread: dict[str, list[str]] = defaultdict(list)
    tag_note_totals: dict[str, int] = defaultdict(int)
    for folder, hist in folder_tag_hist.items():
        if folder.startswith((ARCHIVE_FOLDER, INBOX_FOLDER)):
            continue
        if not hist:
            continue
        _dom_tag, _dom_n = max(hist.items(), key=lambda kv: kv[1], default=(None, 0))
        if not _dom_tag:
            continue
        _t = str(_dom_tag)
        if _t in _IC_PROVENANCE or normalize_type(_t):
            continue
        tag_folder_spread[_t].append(folder)
        tag_note_totals[_t] += _dom_n
    index_candidate_rows: list[list[str]] = []
    for tag, folders in sorted(tag_folder_spread.items()):
        if len(folders) < INDEX_CANDIDATE_MIN_FOLDERS:
            continue
        if tag_note_totals[tag] < INDEX_CANDIDATE_MIN_NOTES:
            continue
        if _tag_titlecase(tag).lower() in index_basenames:
            continue
        index_candidate_rows.append([
            tag, len(folders), tag_note_totals[tag],
            "; ".join(sorted(folders)[:4]),
        ])
        candidate_findings.append((
            "index-candidate", f"tag:{tag}",
            f"folders={len(folders)} n={tag_note_totals[tag]}",
        ))

    sections.append("## Cluster and index candidates")
    sections.append(
        f"<!-- Candidate rows only — the analyst adjudicates; the janitor takes "
        f"no action on cluster-candidate/index-candidate codes. Cluster bar: "
        f">={CLUSTER_MIN_NOTES} notes, >={int(CLUSTER_MIN_SHARE * 100)}% dominant-tag "
        f"share, oldest arrival >={CLUSTER_MIN_OLDEST_DAYS}d (filesystem series); a "
        f"dominant tag with its subfolder already split is dropped. Index bar: tag "
        f"spanning >={INDEX_CANDIDATE_MIN_FOLDERS} folders with "
        f">={INDEX_CANDIDATE_MIN_NOTES} notes and no `<Tag>.md` index anywhere. -->"
    )
    sections.append(_md_table(
        ["folder", "total-notes", "dominant-tag", "share", "oldest-arrival"],
        cluster_rows,
    ))
    sections.append(_md_table(
        ["tag", "folders", "total-notes", "example-folders"],
        index_candidate_rows,
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


    # Settled-convention suppression (CONFIG § Log files). Every class an agent
    # settled by evidence lives as a row in `<logs>/Conventions.md` — code, scope
    # predicate, evidence, date — read here and applied at detect time, so a
    # settled class stops re-firing without a hand patch in this file. Each
    # suppression is COUNTED and reported in ## Convention store.
    conventions_path = VAULT_ROOT / LOGS_REL / "Conventions.md"
    convention_rows: list[dict] = []
    convention_warnings: list[str] = []
    if conventions_path.is_file():
        try:
            convention_rows, convention_warnings = parse_convention_store(
                conventions_path.read_text(encoding="utf-8", errors="replace")
            )
        except OSError as exc:
            convention_warnings = [f"store unreadable ({exc}); no suppression applied"]
    convention_hits: dict[int, int] = defaultdict(int)

    def _by_convention(code: str, target: str, value: str = "",
                       ctx: Optional[dict] = None) -> bool:
        """True when a settled convention covers this finding (and counts it)."""
        facts = ctx or {}
        for i, crow in enumerate(convention_rows):
            if convention_matches(crow, code, target, value, facts):
                convention_hits[i] += 1
                return True
        return False

    def _cover_flag(rel: str) -> str:
        """'true' when `rel` is its folder's cover note — the fact the store's
        `cover:` predicate reads. A vault-root loose file has no folder to cover."""
        return "true" if (len(rel.split("/")) >= 2 and is_folder_cover(Path(rel))) else "false"

    # Findings as (code, target, value) tuples.
    open_findings: list[tuple[str, str, str]] = []
    # Cluster/index candidate rows computed with the folder histogram above.
    open_findings.extend(candidate_findings)

    # status-coherence hits, case -> [rel]. Rendered as counts + a bounded sample
    # in ## Status coherence and folded into ## Open findings below.
    status_coherence: dict[str, list[str]] = defaultdict(list)
    STATUS_COHERENCE_SAMPLE = 8
    # The three cases, in the order the section reports them.
    _STATUS_CASES = [
        "reviewed-true-status-draft-filed",
        "status-draft-filed",
        "reviewed-false-filed",
    ]

    # reviewed-stale evidence baseline: the previous snapshot's content hashes.
    # On the first hashed run the baseline is empty, so no reviewed-stale can
    # fire — change evidence has to be observed across two runs.
    prev_hashes = _read_previous_hashes(baseline_path)
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
        # Inbox drafts are skipped in code (the user has not filed them yet); the
        # settled exemptions — a cover being the root of its own scope, a member
        # of a hands-off asset folder, the Areas type-home fallback folders —
        # live as rows in `<logs>/Conventions.md` and suppress here by predicate.
        if canonical_type in _UPLINK_BY_TYPE:
            field = _UPLINK_BY_TYPE[canonical_type]
            val = fm.get(field)
            if not (val is not None and str(val).strip()):
                if not rel.startswith(INBOX_FOLDER + "/"):
                    _pm_ctx = {
                        "type": canonical_type or "",
                        "cover": _cover_flag(rel),
                        "in-asset-folder": "true" if in_asset_folder(
                            record["abspath"], VAULT_ROOT) else "false",
                    }
                    if not _by_convention("parent-missing", rel, field, _pm_ctx):
                        open_findings.append(("parent-missing", rel, field))

        # status-coherence: a status/reviewed/location combination CONFIG
        # § Status does not allow. `draft` means unreviewed IN the inbox, so a
        # filed note carrying it is a filing that never restamped — the class
        # that let filed standards sit `status: draft` unseen. `active` covers a
        # filed standard still gaining weight, so it is coherent anywhere.
        # Detect-only: counts and a bounded sample render in ## Status coherence.
        _status = str(fm.get("status") or "").strip().lower()
        _reviewed_raw = str(fm.get("reviewed")).strip().lower()
        if not rel.startswith(INBOX_FOLDER + "/"):
            _case = ""
            _cover = _cover_flag(rel)
            if _status == "draft" and _reviewed_raw == "true":
                _case = "reviewed-true-status-draft-filed"
            elif _status == "draft":
                _case = "status-draft-filed"
            elif _reviewed_raw in ("false", "no", "0"):
                # CONFIG § Status exempts living state documents: reviewed
                # governs knowledge content, not live state. A folder cover and
                # a canonical plan are maintained in place and carry
                # `reviewed: false` by design, so neither is a coherence defect.
                if _cover != "true" and canonical_type != "plan":
                    _case = "reviewed-false-filed"
            if _case:
                _sc_ctx = {"type": canonical_type or "", "cover": _cover}
                if not _by_convention("status-coherence", rel, _case, _sc_ctx):
                    status_coherence[_case].append(rel)

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
                    t_type = normalize_type(str(t_fm.get("type") or "")) or ""
                    if t_type not in _UPSTREAM_TYPES:
                        continue
                    # Settled conventions about WHICH upstream can stale WHICH
                    # citer — standards evolve additively, a still-iterating plan
                    # does not stale a living document — live as store rows keyed
                    # on upstream-type / upstream-status / type. A suppressed pair
                    # is dropped alone, so any other changed upstream of the same
                    # note still fires.
                    _rs_ctx = {
                        "type": canonical_type or "",
                        "cover": _cover_flag(rel),
                        "upstream-type": t_type,
                        "upstream-status": str(t_fm.get("status") or "").strip().lower(),
                    }
                    if _by_convention("reviewed-stale", rel, target, _rs_ctx):
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
    # skipped.
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

    # Confirmed-knowledge sources surface first in the detail rows.
    _DANGLING_TYPE_RANK = {"reference": 0, "index": 1, "design": 1, "format": 1, "voice": 1}
    dangling_findings: list[tuple[int, str, str]] = []
    for record in fm_records:
        rel = record["rel_path"]
        if rel.startswith(INBOX_FOLDER + "/"):
            continue  # inbox drafts are expected to dangle until filed
        fm = record.get("fm") or {}
        ctype = normalize_type(str(fm.get("type") or "")) or ""
        rank = _DANGLING_TYPE_RANK.get(ctype, 5)
        for ghost in record.get("helper_links", []):
            if ghost.lower() in all_basenames_lower:
                continue
            # Settled conventions about which ghosts are citations rather than
            # defects — a memory slug in an immutable session log, a ghost inside
            # an append-only log's quoted evidence — live as store rows keyed on
            # type and a value regex over the ghost basename.
            if _by_convention("dangling-link", rel, ghost,
                              {"type": ctype, "cover": _cover_flag(rel)}):
                continue
            dangling_findings.append((rank, rel, ghost))

    dangling_findings.sort(key=lambda t: (t[0], t[1], t[2]))
    for _rank, rel, ghost in dangling_findings:
        open_findings.append(("dangling-link", rel, ghost))

    # Scattered loose-asset duplicates / divergences (computed above).
    open_findings.extend(asset_dedup_findings)

    # Fold in caller-supplied findings (e.g. audit.py's run-scoped inference and
    # queue findings), parsed from a `code | target | value`-per-line file.
    # These pass the same settled-convention gate as native detections: the
    # facts a store predicate reads (type / cover / in-asset-folder) are rebuilt
    # from this run's fm_records, so a row like the parent-finding pair can
    # suppress them. A target that is not a walked vault file gets empty facts,
    # so only path clauses can match it — no facts, no fact-based suppression.
    if args.findings is not None and args.findings.exists():
        _cf_by_rel = {r["rel_path"]: r for r in fm_records}
        try:
            for line in args.findings.read_text(encoding="utf-8").splitlines():
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 2 and parts[0]:
                    code = parts[0]
                    target = parts[1]
                    value = parts[2] if len(parts) >= 3 else ""
                    _cf_rec = _cf_by_rel.get(target)
                    _cf_raw = ((_cf_rec.get("fm") or {}).get("type")
                               if _cf_rec else None)
                    _cf_ctx = {
                        "type": (normalize_type(str(_cf_raw)) or "") if _cf_raw else "",
                        "cover": _cover_flag(target) if _cf_rec else "false",
                        "in-asset-folder": ("true" if _cf_rec is not None
                                            and in_asset_folder(_cf_rec["abspath"], VAULT_ROOT)
                                            else "false"),
                    }
                    if not _by_convention(code, target, value, _cf_ctx):
                        open_findings.append((code, target, value))
        except OSError:
            pass

    # =========================================================================
    # Stream A detector wave (Note-Kit-Structural-Integrity-Plan § Detector
    # build-out). Each emits findings the janitor repairs or queues. Read-only.
    # =========================================================================

    def _sa_read(p: Path) -> str:
        try:
            return Path(p).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""

    def _sa_body(text: str) -> str:
        if text.startswith("---"):
            ff = list(_FM_FENCE.finditer(text))
            if len(ff) >= 2 and ff[0].start() == 0:
                return text[ff[1].end():]
        return text

    def _sa_ymd(s):
        try:
            return datetime.strptime(str(s), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None

    _sa_today = now_utc.date()

    # --- Detector: deploy-drift (vault kit sources vs deployed ~/.claude copies) ---
    # Mechanizes the arithmetic half of the analyst's Deploy-drift method: hash
    # the vault-source scheduled-task and skill trees against their deployed
    # `<user-home>/.claude` twins and emit one `deploy-drift` row per file that
    # differs or is missing. The analyst adjudicates (report vs queue); the
    # janitor takes no action on this code. Raw byte hashes — deploys copy
    # bytes, so a newline-only difference is real drift on this pair.
    _dd_kit = VAULT_ROOT / ".claude"
    _dd_home = Path.home() / ".claude"

    def _dd_hash(p: Path):
        try:
            import hashlib as _hl
            return _hl.sha256(p.read_bytes()).hexdigest()
        except OSError:
            return None

    def _dd_tree_files(root: Path) -> list[Path]:
        out = []
        for p in root.rglob("*"):
            if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
                out.append(p)
        return out

    _dd_pairs: list[tuple[Path, Path, str]] = []
    _sched_src = _dd_kit / "scheduled-tasks"
    if _sched_src.is_dir():
        for _agent_dir in sorted(_sched_src.iterdir()):
            if _agent_dir.is_dir():
                _dd_pairs.append((
                    _agent_dir,
                    _dd_home / "scheduled-tasks" / f"note-kit-{_agent_dir.name}",
                    f"scheduled-tasks/{_agent_dir.name}",
                ))
    _skills_src = _dd_kit / "skills"
    if _skills_src.is_dir():
        for _skill_dir in sorted(_skills_src.glob("note-kit-*")):
            if _skill_dir.is_dir():
                _dd_pairs.append((
                    _skill_dir,
                    _dd_home / "skills" / _skill_dir.name,
                    f"skills/{_skill_dir.name}",
                ))
    for _src_dir, _dep_dir, _label in _dd_pairs:
        if not _dep_dir.is_dir():
            open_findings.append(("deploy-drift", _label, "deployed copy missing"))
            continue
        for _sf in _dd_tree_files(_src_dir):
            _rel_in = _sf.relative_to(_src_dir).as_posix()
            _df = _dep_dir / _rel_in
            if not _df.is_file():
                open_findings.append(
                    ("deploy-drift", f"{_label}/{_rel_in}", "absent from deployed copy"))
            elif _dd_hash(_sf) != _dd_hash(_df):
                open_findings.append(
                    ("deploy-drift", f"{_label}/{_rel_in}", "deployed differs from vault source"))

    # --- Detector: duplicate-run (same agent firing twice in one slot) ---
    # Reads each agent's live log head for run-start lines and flags two starts
    # within DUPLICATE_RUN_WINDOW_MIN minutes — same-slot double-fires land
    # seconds apart, so a 10-minute window bounds one slot without spanning two.
    DUPLICATE_RUN_WINDOW_MIN = 10
    _dr_ts_re = re.compile(
        r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?Z?\s*\|[^|]*\|\s*run-start\s*\|")
    for _agent in ("janitor-agent", "filing-agent", "analyst-agent", "action-agent"):
        _log_head = VAULT_ROOT / LOGS_REL / _agent / f"{_agent}.md"
        _starts: list[datetime] = []
        for _line in _sa_read(_log_head).splitlines():
            _m = _dr_ts_re.match(_line.strip())
            if _m:
                try:
                    _starts.append(datetime(
                        *map(int, _m.group(1).split("-")),
                        int(_m.group(2)), int(_m.group(3)), int(_m.group(4) or 0)))
                except ValueError:
                    continue
        _starts.sort()
        for _a, _b in zip(_starts, _starts[1:]):
            _gap = (_b - _a).total_seconds()
            if 0 <= _gap < DUPLICATE_RUN_WINDOW_MIN * 60:
                open_findings.append((
                    "duplicate-run", _agent,
                    f"run-starts {_a.isoformat()}Z + {_b.isoformat()}Z ({int(_gap)}s apart)"))

    # --- Detector 1: index-vs-disk diff (index-missing / index-drift) + coverage ---
    # Per index-type note: set-difference of its folder's on-disk direct .md
    # members against the index's body links. A member absent entirely emits
    # `index-missing`; a member linked only in the flat preamble (no `## section`)
    # emits `index-drift` (flat-only). The chapter-coverage % (linked members that
    # sit under a section) is reported in the ## Index coverage snapshot section, so
    # coverage is a measured number, never a claim.
    #
    # Chaptered means SUBDIVIDED. CONFIG § Types asks a cover to chapter its
    # children "under clear labels that track these subdivisions and their
    # purpose", and one catch-all `## Members` holding every child tracks no
    # subdivision at all — it is the flat list with a heading on top. So a cover
    # scores chaptered only when TWO OR MORE of its `## ` sections actually carry
    # members; a single member-bearing section reads flat however many links sit
    # inside it. Sections that link only outward (`## Related`) subdivide nothing
    # and are not counted.
    _H2_LINE_RE = re.compile(r"^## .*$", re.MULTILINE)

    def _links_by_region(body: str) -> tuple[set[str], list[tuple[str, set[str]]]]:
        """(links before the first `## `, [(heading, links) for each `## ` section])."""
        heads = list(_H2_LINE_RE.finditer(body))
        if not heads:
            return set(extract_wikilinks(body)), []
        preamble = set(extract_wikilinks(body[: heads[0].start()]))
        regions: list[tuple[str, set[str]]] = []
        for i, h in enumerate(heads):
            end = heads[i + 1].start() if i + 1 < len(heads) else len(body)
            regions.append((h.group(0).strip(),
                            set(extract_wikilinks(body[h.end():end]))))
        return preamble, regions

    index_coverage_rows: list[list] = []
    _cov_total_linked = 0
    _cov_total_chaptered = 0
    for record in fm_records:
        fm = record.get("fm") or {}
        if normalize_type(str(fm.get("type") or "")) != "index":
            continue
        idx_rel = record["rel_path"]
        if idx_rel.startswith(INBOX_FOLDER + "/"):
            continue  # inbox drafts are not yet filed; skip
        idx_abs = record["abspath"]
        # The every-child contract is on the FOLDER-NOTE COVER only (CONFIG:
        # "only a root carries one"). A curated sub-index living inside a folder
        # (type index but stem != folder name) is NOT diffed — flagging its
        # unlisted siblings would be a false positive the decided auto-repair
        # would then act on, destroying curation.
        if not is_folder_cover(idx_abs):
            continue
        idx_folder = idx_abs.parent
        # An unreadable cover would make _sa_read return "" and read EVERY member
        # as index-missing; report the read failure as its own finding and skip
        # the diff for this cover (an empty-but-readable cover still diffs).
        try:
            idx_raw = idx_abs.read_bytes()
        except OSError as exc:
            open_findings.append(("index-cover-unreadable", idx_rel,
                                  f"cover read failed ({exc}); diff skipped"))
            continue
        flat_links, section_regions = _links_by_region(
            _sa_body(idx_raw.decode("utf-8", errors="replace")))
        sectioned_links: set[str] = set()
        for _h, _links in section_regions:
            sectioned_links |= _links
        all_links = flat_links | sectioned_links
        members = [
            r for r in fm_records
            if r["abspath"].parent == idx_folder and r["abspath"] != idx_abs
        ]
        member_stems = {r["abspath"].stem for r in members}
        # The sections that carry members are the cover's real chapters.
        n_member_sections = sum(
            1 for _h, links in section_regions if links & member_stems
        )
        subdivided = n_member_sections >= 2
        n_linked = 0
        n_chaptered = 0
        for mrec in members:
            mstem = mrec["abspath"].stem
            if mstem not in all_links:
                open_findings.append(("index-missing", mrec["rel_path"],
                                      f"index:{Path(idx_rel).stem}"))
            else:
                n_linked += 1
                if mstem not in sectioned_links:
                    open_findings.append(("index-drift", mrec["rel_path"],
                                          f"index:{Path(idx_rel).stem} flat-only"))
                elif subdivided:
                    n_chaptered += 1
        cov = round(100.0 * n_chaptered / n_linked, 1) if n_linked else 100.0
        if not n_linked:
            cov_label = f"{cov}%"          # nothing linked — vacuously covered
        elif subdivided:
            cov_label = f"{cov}%"
        else:
            cov_label = f"{cov}% (flat)"   # one catch-all section is no subdivision
        index_coverage_rows.append([idx_rel, len(members), n_linked, n_chaptered,
                                    n_member_sections, cov_label])
        _cov_total_linked += n_linked
        _cov_total_chaptered += n_chaptered
    index_coverage_overall = (
        round(100.0 * _cov_total_chaptered / _cov_total_linked, 1)
        if _cov_total_linked else 100.0
    )

    # --- Detector 3: living-surface currency (cover-stale) ---
    # Living surface = the folder-note cover of each <projects>/<areas> root with
    # status: active. Parse the newest dated state block (both live patterns);
    # baseline = the newest session date-prefix in the root's Sessions/. Emit
    # `cover-stale` when the state block trails the newest session by more than the
    # threshold (default 4 days on an active project). An idle project retaining
    # "Status as of X" wording is flagged too.
    #
    # The date is half the reading. A state block that names a canonical plan,
    # session, or ledger which no longer exists is stale whatever its own date
    # says — a purged plan leaves a fresh-dated block citing nothing, and a
    # date comparison can never see it. So the block's OWN wikilinks are
    # resolved too, against the same whole-vault basename index the
    # dangling-link detector uses (Obsidian's case-insensitive resolution), and
    # an unresolved citation fires `cover-stale` on its own.
    _STATE_PATTERNS = [
        re.compile(r"^## State of play [—-] (\d{4}-\d{2}-\d{2})", re.MULTILINE),
        re.compile(r"\*\*State of play \((\d{4}-\d{2}-\d{2})\)"),
    ]
    _NEXT_H2_RE = re.compile(r"^## ", re.MULTILINE)
    _PARA_BREAK_RE = re.compile(r"\n[ \t]*\r?\n")
    _COVER_STALE_THRESHOLD_DAYS = 4

    def _newest_state_block(text: str) -> tuple[str, str]:
        """(date, text) of the NEWEST dated state-of-play block, or ("", "").

        A cover that stacks superseded state sections is read at its current one
        only. The heading form owns its whole `## ` section; the inline bold form
        owns its paragraph, so neither over-reads into unrelated prose.
        """
        best_date, best_block = "", ""
        for idx, pat in enumerate(_STATE_PATTERNS):
            for m in pat.finditer(text):
                if m.group(1) <= best_date:
                    continue
                nxt = _NEXT_H2_RE.search(text, m.end())
                end = nxt.start() if nxt else len(text)
                if idx == 1:  # inline bold lead-in — stop at the paragraph break
                    para = _PARA_BREAK_RE.search(text, m.end())
                    if para and para.start() < end:
                        end = para.start()
                best_date, best_block = m.group(1), text[m.start():end]
        return best_date, best_block

    for record in fm_records:
        rel = record["rel_path"]
        parts = rel.split("/")
        if len(parts) != 3 or parts[0] not in (PROJECTS_FOLDER, AREAS_FOLDER):
            continue
        abspath = record["abspath"]
        if not is_folder_cover(abspath):
            continue  # only the folder-note cover (incl. legacy NN- prefixed)
        fm = record.get("fm") or {}
        status = str(fm.get("status") or "").strip().lower()
        cover_text = _sa_read(abspath)
        cover_date, state_block = _newest_state_block(cover_text)
        # A dangling canonical citation IS staleness — checked on every cover
        # that carries a state block, active or not, before the date reading.
        if state_block:
            ghosts = sorted(
                g for g in extract_wikilinks(state_block)
                if g.lower() not in all_basenames_lower
            )
            if ghosts:
                shown = ", ".join(ghosts[:3]) + (" …" if len(ghosts) > 3 else "")
                open_findings.append((
                    "cover-stale", rel,
                    f"state block ({cover_date}) cites {len(ghosts)} unresolved "
                    f"link(s): {shown}",
                ))
        if status == "active":
            if not cover_date:
                continue
            sess_dir = abspath.parent / "Sessions"
            sess_dates = []
            if sess_dir.is_dir():
                for sp in sess_dir.glob("*.md"):
                    mm = re.match(r"(\d{4}-\d{2}-\d{2})", sp.stem)
                    if mm:
                        sess_dates.append(mm.group(1))
            if not sess_dates:
                continue
            baseline = max(sess_dates)
            cd, bd = _sa_ymd(cover_date), _sa_ymd(baseline)
            if cd is None or bd is None:
                continue
            gap = (bd - cd).days
            if gap > _COVER_STALE_THRESHOLD_DAYS:
                open_findings.append((
                    "cover-stale", rel,
                    f"state {cover_date} trails newest session {baseline} by {gap}d",
                ))
        else:
            if re.search(r"Status as of", cover_text):
                open_findings.append((
                    "cover-stale", rel,
                    "idle project retains 'Status as of' wording",
                ))

    # --- Detectors 4b + 5: run-liveness (missed-cadence) + log-timestamp lint ---
    # Read each agent ledger once. Detector 5 flags 00:00Z stamps and
    # out-of-order timestamps (`log-timestamp-suspect`, per-ledger aggregate).
    # Detector 4b emits `missed-cadence` for an agent participating in the
    # run-liveness protocol (has >=1 `run-start` line) whose latest run-start is
    # older than its cadence window. Agents with no run-start line are not yet
    # participating and are skipped (no false positive before adoption).
    logs_dir = VAULT_ROOT / LOGS_REL
    _CADENCE_DAYS = {"janitor-agent": 1, "filing-agent": 1,
                     "analyst-agent": 7, "action-agent": 1}
    _sa_ts_re = re.compile(r"^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?Z?$")
    if logs_dir.is_dir():
        for agent_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
            agent = agent_dir.name
            ledger = agent_dir / f"{agent}.md"
            if not ledger.is_file():
                continue
            led_rel = str(ledger.relative_to(VAULT_ROOT)).replace("\\", "/")
            zero_count = 0
            out_of_order = 0
            prev_dt = None
            run_start_dates: list[str] = []
            for ln in _sa_read(ledger).splitlines():
                fields = [p.strip() for p in ln.split("|")]
                if len(fields) < 3:
                    continue
                m = _sa_ts_re.match(fields[0])
                if not m:
                    continue
                ymd, hh, mm, ss = m.group(1), m.group(2), m.group(3), m.group(4) or "00"
                if hh == "00" and mm == "00" and ss == "00":
                    zero_count += 1
                try:
                    dt = datetime.strptime(f"{ymd}T{hh}:{mm}:{ss}", "%Y-%m-%dT%H:%M:%S")
                    if prev_dt is not None and dt < prev_dt:
                        out_of_order += 1
                    prev_dt = dt
                except ValueError:
                    pass
                if fields[2] == "run-start":
                    run_start_dates.append(ymd)
            if zero_count or out_of_order:
                _lt_value = f"zero-stamp:{zero_count} out-of-order:{out_of_order}"
                if not _by_convention("log-timestamp-suspect", led_rel, _lt_value):
                    open_findings.append(("log-timestamp-suspect", led_rel, _lt_value))
            if run_start_dates:
                latest = max(run_start_dates)
                latest_d = _sa_ymd(latest)
                cad = _CADENCE_DAYS.get(agent, 1)
                if latest_d is not None:
                    gap = (_sa_today - latest_d).days
                    if gap > cad:
                        open_findings.append((
                            "missed-cadence", led_rel,
                            f"{agent} last run-start {latest} ({gap}d ago, cadence {cad}d)",
                        ))

    # --- Status coherence rows (collected in the frontmatter loop above) ---
    # All three cases roll up the SAME way: one row per folder scope carrying the
    # case count and up to STATUS_COHERENCE_ROLLUP_SAMPLES sample paths. A
    # per-file row per hit made this one class most of ## Open findings — a
    # standing restamp backlog the janitor works in bulk, not file by file. The
    # scope row names the work and the samples name where to start; the full
    # counts and the wider sample sit in ## Status coherence.
    STATUS_COHERENCE_ROLLUP_SAMPLES = 3
    for _case in _STATUS_CASES:
        _sc_by_scope: dict[str, list[str]] = defaultdict(list)
        for _rel in status_coherence.get(_case, []):
            _sc_by_scope[scope_of(_rel)].append(_rel)
        for _scope_name, _rels in sorted(_sc_by_scope.items()):
            _samples = sorted(_rels)[:STATUS_COHERENCE_ROLLUP_SAMPLES]
            open_findings.append((
                "status-coherence", _scope_name,
                f"{_case}:{len(_rels)} (rollup) e.g. " + ", ".join(_samples),
            ))

    # --- Detector 6: fix-effect verification (fix-verified / fix-unverified) ---
    # An approved kit fix records its acceptance signal in `<logs>/fix-verification.md`
    # (one pipe line per fix: `<fix-id> | <signal-type> | <signal-target> | <spec>`).
    # signal-type: `log-line` (spec appears in the <logs>-relative ledger target),
    # `finding-absent` (spec finding-code is NOT open this run), or `block-present`
    # (spec appears in the vault-relative file target). The signal firing logs
    # `fix-verified`; an absent signal re-raises as `fix-unverified` — a fix that
    # executed and stayed silently inert is surfaced, not paid for twice. Inert
    # (no manifest) until the file exists.
    fixver_path = logs_dir / "fix-verification.md"
    if fixver_path.is_file():
        open_codes = {c for c, _t, _v in open_findings}
        for ln in _sa_read(fixver_path).splitlines():
            raw = ln.strip()
            if not raw:
                continue  # blank line
            fields = [p.strip() for p in raw.split("|")]
            # Header / markdown separator rows are not data — skip silently.
            if fields[0].lower() == "fix-id" or set(raw) <= {"-", "|", " ", ":"}:
                continue
            # A malformed data line (too few fields, or an unknown signal-type)
            # is NOT dropped silently — a silent drop reintroduces the exact
            # executed-but-inert class this detector exists to catch.
            if len(fields) < 4 or not fields[0]:
                open_findings.append((
                    "fix-manifest-malformed", "fix-verification.md",
                    f"line has <4 fields: {raw[:80]}",
                ))
                continue
            fix_id, sig_type, sig_target, sig_spec = fields[0], fields[1], fields[2], fields[3]
            if sig_type == "log-line":
                verified = sig_spec in _sa_read(logs_dir / sig_target)
            elif sig_type == "finding-absent":
                verified = sig_spec not in open_codes
            elif sig_type == "block-present":
                verified = sig_spec in _sa_read(VAULT_ROOT / sig_target)
            else:
                open_findings.append((
                    "fix-manifest-malformed", fix_id,
                    f"unknown signal-type: {sig_type}",
                ))
                continue
            if verified:
                open_findings.append(("fix-verified", fix_id, f"{sig_type}:{sig_spec}"))
            else:
                open_findings.append((
                    "fix-unverified", fix_id,
                    f"signal absent — {sig_type}:{sig_spec} (re-raise)",
                ))

    # --- Detector 6b: claim check (claim-verified / claim-false) ---
    # CONFIG § Log files, Claim check: a log line asserting a file's state carries
    # a machine-readable signal in its VALUE cell — `claim: <path> | <expected
    # condition>`. This run re-derives each such claim FROM DISK and emits
    # `claim-verified` or `claim-false`; a `claim-false` row is the retraction
    # opener.
    #
    # A claimant never certifies itself, and the boundary that decides this is
    # the producing RUN, not the clock. The janitor writes its claims and then
    # invokes this build seconds later within the same pass, so a wall-clock
    # window admits exactly the claim it was meant to exclude. A claim is
    # therefore checkable only once its run has CLOSED — a `run-end` (or a
    # `run-aborted`, which closes a run as truthfully: a crashed pass writes no
    # further lines) stands AFTER it in the same head — or once it predates the
    # head's last `run-start`, which settles it to an earlier run. A claim
    # sitting after the last `run-start` with no closing line is inside a run
    # still open, and emits `claim-deferred` for the next pass to read.
    #
    # A head that emits no `run-start` at all has no open-run region to sit in,
    # so the boundary rule stays silent there rather than deferring its claims
    # forever; the run-timestamp window remains as a second, weaker guard for
    # exactly that case.
    #
    # Scan scope: the agent event-log HEADS under <logs> — one `<dir>/<dir>.md`
    # per log folder, which covers janitor / filing / action / analyst and the
    # orchestration ledger. Rotated segments (`<agent>-YYYY-MM.md`) are excluded:
    # a closed segment is history, and re-deriving a months-old claim against
    # today's disk reports drift that the retraction protocol never asked for.
    #
    # Conditions (the prefix decides the read):
    #   exists              — the path is present on disk
    #   absent              — the path is not present
    #   contains:<literal>  — the file is present and its text carries the literal
    #   hash:<md5-prefix>   — the file's md5 starts with the prefix; both the
    #                         FULL-file md5 and the frontmatter-excluded BODY md5
    #                         (the `## Content hashes` column a claimant reads
    #                         off this very snapshot) satisfy it
    # An unparseable condition emits `claim-unparseable` and never raises — a
    # hand-typed claim degrades one row instead of the run.
    _CLAIM_MAX_PER_LEDGER = 200
    _CLAIM_HEX = re.compile(r"^[0-9a-f]{4,32}$")

    def _claim_condition_result(abs_path: Path, cond: str):
        """Re-derive one claim from disk.

        Returns (verdict, observed) where verdict is True (holds), False (does
        not hold), or None (the condition itself is unparseable — `observed`
        then carries the reason).
        """
        low = cond.strip()
        low_key = low.lower()
        if low_key in ("exists", "present"):
            return abs_path.exists(), ("present" if abs_path.exists() else "not on disk")
        if low_key in ("absent", "missing"):
            return (not abs_path.exists()), ("not on disk" if not abs_path.exists() else "present")
        if low_key.startswith("contains:"):
            literal = low[len("contains:"):].strip()
            if not literal:
                return None, "contains: has no literal"
            if not abs_path.is_file():
                return False, "file not on disk"
            return (literal in _sa_read(abs_path)), (
                "literal present" if literal in _sa_read(abs_path) else "literal absent"
            )
        if low_key.startswith("hash:"):
            prefix = low[len("hash:"):].strip().lower()
            if not _CLAIM_HEX.match(prefix):
                return None, f"hash: prefix is not 4-32 hex chars ({prefix[:16]!r})"
            if not abs_path.is_file():
                return False, "file not on disk"
            full = _file_md5(abs_path)
            body = None
            if abs_path.suffix.lower() == ".md":
                body = _body_md5(_sa_read(abs_path))
            hits = [h for h in (full, body) if h]
            if not hits:
                return False, "file unreadable or above the md5 size cap"
            ok = any(h.startswith(prefix) for h in hits)
            return ok, ("full=" + (full or "-")[:12] + " body=" + (body or "-")[:12])
        return None, f"unknown condition prefix in {cond[:40]!r}"

    if logs_dir.is_dir():
        for _log_dir in sorted(p for p in logs_dir.iterdir() if p.is_dir()):
            _head = _log_dir / f"{_log_dir.name}.md"
            if not _head.is_file():
                continue
            _head_rel = str(_head.relative_to(VAULT_ROOT)).replace("\\", "/")
            _head_lines = _sa_read(_head).splitlines()
            # Run-boundary map for this head, read once: where runs open and
            # where they close. `run-aborted` closes as truthfully as `run-end`.
            _run_open_at: list[int] = []
            _run_close_at: list[int] = []
            for _i, _rl in enumerate(_head_lines):
                _cells = _rl.split("|")
                if len(_cells) < 3:
                    continue
                _mark = _cells[2].strip().lower()
                if _mark == "run-start":
                    _run_open_at.append(_i)
                elif _mark in ("run-end", "run-aborted"):
                    _run_close_at.append(_i)
            _last_open = _run_open_at[-1] if _run_open_at else None
            _last_close = _run_close_at[-1] if _run_close_at else None
            _seen_claims = 0
            _deferred = 0
            _deferred_open = 0
            for _lineno, _raw in enumerate(_head_lines, 1):
                _line = _raw.strip()
                if "claim:" not in _line:
                    continue
                # `timestamp | actor | code | target | value` — the value cell is
                # everything past the fourth pipe, so the claim's own inner pipe
                # survives the split.
                _f = _line.split("|", 4)
                if len(_f) < 5:
                    continue
                _val = _f[4].strip()
                if not _val.lower().startswith("claim:"):
                    continue  # the token appears in prose, not as the signal
                _seen_claims += 1
                if _seen_claims > _CLAIM_MAX_PER_LEDGER:
                    open_findings.append((
                        "claim-unparseable", _head_rel,
                        f"more than {_CLAIM_MAX_PER_LEDGER} claim lines — "
                        f"remainder not checked this run",
                    ))
                    break
                # Self-certification guard, run-boundary first: the claim is
                # checkable once a closing line follows it (its run closed), or
                # once it precedes the head's last run-start (it belongs to an
                # earlier, settled run). Anything else sits inside an open run.
                _idx = _lineno - 1
                _closed_after = _last_close is not None and _last_close > _idx
                _predates_last_open = _last_open is not None and _idx < _last_open
                _in_open_run = (_last_open is not None
                                and not _closed_after
                                and not _predates_last_open)
                # Second guard, for a head that emits no run boundaries at all:
                # a line stamped at or after this run defers to the next pass.
                _stamped_ahead = False
                _cm = _sa_ts_re.match(_f[0].strip())
                if _cm:
                    _c_ts = (f"{_cm.group(1)}T{_cm.group(2)}:{_cm.group(3)}:"
                             f"{_cm.group(4) or '00'}")
                    _stamped_ahead = _c_ts >= ts.rstrip("Z")
                if _in_open_run or _stamped_ahead:
                    _deferred += 1
                    if _in_open_run:
                        _deferred_open += 1
                    continue
                _payload = _val[len("claim:"):].strip()
                if "|" not in _payload:
                    open_findings.append((
                        "claim-unparseable", _head_rel,
                        f"line {_lineno}: no `<path> | <condition>` separator — "
                        f"{_payload[:60]}",
                    ))
                    continue
                _cpath, _cond = (s.strip() for s in _payload.split("|", 1))
                _cpath = _cpath.strip("`").replace("\\", "/").strip()
                if not _cpath or not _cond:
                    open_findings.append((
                        "claim-unparseable", _head_rel,
                        f"line {_lineno}: empty path or condition — {_payload[:60]}",
                    ))
                    continue
                try:
                    _cabs = (VAULT_ROOT / _cpath).resolve()
                    _cabs.relative_to(VAULT_ROOT)
                except (ValueError, OSError):
                    open_findings.append((
                        "claim-unparseable", _head_rel,
                        f"line {_lineno}: path resolves outside the vault — {_cpath[:60]}",
                    ))
                    continue
                _verdict, _observed = _claim_condition_result(_cabs, _cond)
                if _verdict is None:
                    open_findings.append((
                        "claim-unparseable", _head_rel,
                        f"line {_lineno}: {_observed}",
                    ))
                elif _verdict:
                    _cv = f"{_cond} | {_head_rel}:{_lineno}"
                    if not _by_convention("claim-verified", _cpath, _cv):
                        open_findings.append(("claim-verified", _cpath, _cv))
                else:
                    _cf = (f"expected {_cond} — observed {_observed} | "
                           f"{_head_rel}:{_lineno}")
                    if not _by_convention("claim-false", _cpath, _cf):
                        open_findings.append(("claim-false", _cpath, _cf))
            if _deferred:
                _why = (f"{_deferred_open} inside an open run"
                        if _deferred_open == _deferred else
                        f"{_deferred_open} inside an open run, "
                        f"{_deferred - _deferred_open} stamped at/after this run")
                open_findings.append((
                    "claim-deferred", _head_rel,
                    f"{_deferred} claim(s) not yet checkable ({_why}) — "
                    f"a claimant never certifies itself; checked next pass",
                ))

    # --- Detector 7: near-duplicate pairs (vault-search embeddings) ---
    # Stream A redundancy surface, tier-2 calibration ruled 2026-07-25: a pair of
    # notes whose vault-search embedding cosine similarity reaches
    # NEAR_DUPLICATE_THRESHOLD is flagged for the analyst's merge call. The
    # embeddings are the daemon's own — there is no hand-rolled similarity here
    # and no fallback metric: when the daemon is down, the index is stale by
    # definition, so the detector SKIPS AND LOGS (`near-duplicate-skipped`)
    # instead of reporting a number nobody can trust.
    #
    # Embedding path: the daemon's HTTP surface (server.py) exposes only
    # /health, /api/session_brief, /api/topology_status and /shutdown — no pair
    # or raw-embedding endpoint — so the vectors are read straight out of the
    # daemon's own index.db `chunk_embeddings` (a sqlite-vec vec0 table), opened
    # READ-ONLY, with /health as the liveness gate. A file's vector is the
    # L2-normalized mean of its chunk vectors.
    #
    # Cost bound: only the files whose body hash is NEW or CHANGED against the
    # baseline snapshot are compared against the corpus, and the report is capped
    # at NEAR_DUPLICATE_MAX_PAIRS with an honest truncation line.
    NEAR_DUPLICATE_THRESHOLD = 0.85
    NEAR_DUPLICATE_MAX_PAIRS = 20
    nd_skip_reason: Optional[str] = None
    nd_pairs: list[tuple[float, str, str]] = []
    nd_stats: dict[str, object] = {}

    def _nd_daemon_ok(base_url: str) -> tuple[bool, str]:
        import json as _json
        import urllib.request as _urlreq
        try:
            with _urlreq.urlopen(base_url.rstrip("/") + "/health", timeout=4) as resp:
                payload = _json.loads(resp.read().decode("utf-8", errors="replace"))
        except Exception as exc:  # noqa: BLE001 — any failure is "daemon down"
            return False, f"{type(exc).__name__}: {exc}"
        status = str(payload.get("status") or "")
        if status != "ok":
            return False, f"health status={status!r}"
        return True, f"pid={payload.get('pid')} db={payload.get('db_size_mb')}MB"

    _vs_dir = VAULT_ROOT / ".claude" / "vault-search"
    _vs_config = _vs_dir / "config.yaml"
    if not _vs_config.is_file():
        nd_skip_reason = f"no vault-search config at `{_vs_config}`"
    else:
        try:
            _vs_cfg = yaml.safe_load(_vs_config.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            _vs_cfg = {}
            nd_skip_reason = f"vault-search config unreadable ({exc})"
        if nd_skip_reason is None:
            _cfg_vault = str(_vs_cfg.get("vault_path") or "").strip()
            try:
                _same_vault = (_cfg_vault
                               and Path(_cfg_vault).resolve() == VAULT_ROOT)
            except OSError:
                _same_vault = False
            if not _same_vault:
                nd_skip_reason = (f"daemon indexes `{_cfg_vault}`, this run is "
                                  f"`{VAULT_ROOT}` — corpus mismatch")
    if nd_skip_reason is None:
        _host = str(_vs_cfg.get("host") or "127.0.0.1")
        _port = _vs_cfg.get("port") or 8765
        _base = f"http://{_host}:{_port}"
        _ok, _detail = _nd_daemon_ok(_base)
        if not _ok:
            nd_skip_reason = f"daemon at {_base} not serving ({_detail})"
        else:
            nd_stats["daemon"] = f"{_base} {_detail}"
    if nd_skip_reason is None:
        _db_path = Path(str((_vs_cfg.get("index") or {}).get("path")
                            or (_vs_dir / "data" / "index.db")))
        if not _db_path.is_file():
            nd_skip_reason = f"index.db missing at `{_db_path}`"
        else:
            try:
                import sqlite3 as _sqlite3
                import sqlite_vec as _sqlite_vec
                import numpy as _np
            except ImportError as exc:
                nd_skip_reason = (f"embedding read needs sqlite-vec + numpy in the "
                                  f"running interpreter ({exc})")
    if nd_skip_reason is None:
        try:
            _conn = _sqlite3.connect(f"file:{_db_path.as_posix()}?mode=ro", uri=True)
            try:
                _conn.enable_load_extension(True)
                _sqlite_vec.load(_conn)
                _conn.enable_load_extension(False)
                _sums: dict[str, object] = {}
                _counts: dict[str, int] = defaultdict(int)
                for _p, _blob in _conn.execute(
                    "SELECT f.path, e.embedding FROM chunk_embeddings e "
                    "JOIN chunks ch ON ch.chunk_id = e.chunk_id "
                    "JOIN files f ON f.file_id = ch.file_id"
                ):
                    _rel_p = str(_p).replace("\\", "/")
                    # A stale index row (file since deleted or renamed) is not a
                    # twin — the corpus is this run's walked .md set.
                    if _rel_p not in content_hashes:
                        continue
                    _v = _np.frombuffer(_blob, dtype=_np.float32)
                    _acc = _sums.get(_rel_p)
                    _sums[_rel_p] = _v.copy() if _acc is None else (_acc + _v)
                    _counts[_rel_p] += 1
            finally:
                _conn.close()
        except Exception as exc:  # noqa: BLE001 — a bad read is a skip, not a crash
            nd_skip_reason = f"index read failed ({type(exc).__name__}: {exc})"
    if nd_skip_reason is None:
        _paths = sorted(_sums)
        if len(_paths) < 2:
            nd_skip_reason = (f"corpus too small — {len(_paths)} indexed file(s) "
                              f"present on disk")
    if nd_skip_reason is None:
        _M = _np.stack([_sums[p] / max(_counts[p], 1) for p in _paths]).astype(_np.float32)
        _M /= (_np.linalg.norm(_M, axis=1, keepdims=True) + 1e-12)
        _pos = {p: i for i, p in enumerate(_paths)}
        # The delta the snapshot already tracks: NEW (absent from the baseline)
        # or CHANGED (body hash moved). With no baseline every file is new, so
        # the first run compares the whole corpus once and says so.
        _delta = [p for p in _paths
                  if prev_hashes.get(p) != content_hashes.get(p)]
        _seen_pairs: set[tuple[str, str]] = set()
        for _p in _delta:
            _sims = _M @ _M[_pos[_p]]
            for _j in _np.nonzero(_sims >= NEAR_DUPLICATE_THRESHOLD)[0]:
                _q = _paths[int(_j)]
                if _q == _p:
                    continue
                _key = (_p, _q) if _p < _q else (_q, _p)
                if _key in _seen_pairs:
                    continue
                _seen_pairs.add(_key)
                nd_pairs.append((float(_sims[int(_j)]), _key[0], _key[1]))
        nd_pairs.sort(key=lambda t: (-t[0], t[1], t[2]))
        nd_stats.update({
            "corpus": len(_paths),
            "delta": len(_delta),
            "found": len(nd_pairs),
            "baseline": "present" if prev_hashes else "absent (whole corpus is the delta)",
        })
        _nd_total = len(nd_pairs)
        for _sim, _a, _b in nd_pairs[:NEAR_DUPLICATE_MAX_PAIRS]:
            _ndv = f"sim {_sim:.3f} | twin: {_b}"
            if not _by_convention("near-duplicate", _a, _ndv):
                open_findings.append(("near-duplicate", _a, _ndv))
        if _nd_total > NEAR_DUPLICATE_MAX_PAIRS:
            open_findings.append((
                "near-duplicate-truncated", LOGS_REL + "/Vault-State-Index.md",
                f"{_nd_total} pairs at >= {NEAR_DUPLICATE_THRESHOLD}; "
                f"{NEAR_DUPLICATE_MAX_PAIRS} reported this run",
            ))
    if nd_skip_reason is not None:
        open_findings.append((
            "near-duplicate-skipped", LOGS_REL + "/Vault-State-Index.md",
            f"{nd_skip_reason} — no fallback similarity was substituted",
        ))

    # Split the detector-status rows out before the rollup: they report how a
    # detector RAN, not a change to propose, so they carry their own section and
    # never inflate the open-findings count (DETECTOR_STATUS_CODES).
    detector_status = [row for row in open_findings
                       if row[0] in DETECTOR_STATUS_CODES]
    open_findings = [row for row in open_findings
                     if row[0] not in DETECTOR_STATUS_CODES]

    detector_status_lines = [
        f"{ts} | {ACTOR} | {code} | {target} | {value or '-'}"
        for code, target, value in detector_status
    ]

    # Count rollup (code | scope | count) then per-file detail rows, in the kit's
    # state-log line shape. scope_of is the shared module-level helper.
    finding_counts: dict[tuple[str, str], int] = defaultdict(int)
    for code, target, _v in open_findings:
        finding_counts[(code, scope_of(target))] += 1

    finding_count_lines = [
        f"{ts} | {ACTOR} | {code} | {scope} | {n}"
        for (code, scope), n in sorted(finding_counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    finding_detail_lines = [
        f"{ts} | {ACTOR} | {code} | {target} | {value or '-'}"
        for code, target, value in open_findings
    ]

    # --- Index coverage (Stream A detector 1) — measured, never a claim ---
    sections.append("## Index coverage")
    sections.append(
        f"<!-- Folder-note COVERS only (stem == folder name, incl. legacy NN- "
        f"prefixed; curated sub-indexes are not diffed). This measures DIRECT .md "
        f"FILE coverage of each cover — how many of its folder's direct .md members "
        f"the cover links, and how many of those are chaptered (under a `## ` "
        f"section). It is NOT the recursive file-and-folder CONFIG index model "
        f"(sub-folder members and nested trees are out of scope here — recursive "
        f"coverage is a recorded later item). Overall chaptered coverage: "
        f"{index_coverage_overall}% of {_cov_total_linked} linked direct members. "
        f"CHAPTERED MEANS SUBDIVIDED: a member counts as chaptered only when its "
        f"cover carries two or more `## ` sections that actually hold members, so a "
        f"single catch-all section scores 0% and its row reads `(flat)` with "
        f"member-sections 1. Sections that link only outward (`## Related`) hold no "
        f"members and are not counted. "
        f"The index-missing / index-drift findings below carry the per-member detail. -->"
    )
    sections.append(_md_table(
        ["index", "disk-members", "linked", "chaptered", "member-sections",
         "chapter-coverage"],
        index_coverage_rows,
    ))
    sections.append("")

    # --- Near-duplicate (Stream A redundancy surface) ---
    sections.append("## Near-duplicate")
    if nd_skip_reason is not None:
        sections.append(
            f"<!-- SKIPPED this run: {nd_skip_reason}. The pair flag reads the "
            f"vault-search daemon's own embeddings; with the daemon unavailable "
            f"the index is stale by definition, so the detector logs the skip "
            f"rather than substituting a fallback similarity. -->"
        )
        sections.append(f"(skipped — {nd_skip_reason})")
    else:
        _nd_shown = min(len(nd_pairs), NEAR_DUPLICATE_MAX_PAIRS)
        sections.append(
            f"<!-- Cosine similarity over the vault-search daemon's chunk "
            f"embeddings (index.db `chunk_embeddings`, read-only; a file's vector "
            f"is the L2-normalized mean of its chunk vectors). Threshold "
            f">= {NEAR_DUPLICATE_THRESHOLD} (tier-2 calibration, 2026-07-25). "
            f"Compared: {nd_stats.get('delta')} new/changed file(s) against a "
            f"{nd_stats.get('corpus')}-file corpus; baseline "
            f"{nd_stats.get('baseline')}. Daemon: {nd_stats.get('daemon')}. "
            f"Found {len(nd_pairs)} pair(s), showing {_nd_shown} "
            f"(cap {NEAR_DUPLICATE_MAX_PAIRS}). A pair is a MERGE CANDIDATE for "
            f"the analyst, never an automatic action. -->"
        )
        if nd_pairs:
            sections.append(_md_table(
                ["similarity", "file", "twin"],
                [[f"{s:.3f}", f"`{a}`", f"`{b}`"]
                 for s, a, b in nd_pairs[:NEAR_DUPLICATE_MAX_PAIRS]],
            ))
            if len(nd_pairs) > NEAR_DUPLICATE_MAX_PAIRS:
                sections.append("")
                sections.append(
                    f"Truncated: {len(nd_pairs) - NEAR_DUPLICATE_MAX_PAIRS} further "
                    f"pair(s) at or above the threshold are not listed this run."
                )
        else:
            sections.append("(no pair reached the threshold)")
    sections.append("")

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

    # --- Status coherence (CONFIG § Status) — counts + a bounded sample ---
    sections.append("## Status coherence")
    sections.append(
        f"<!-- CONFIG § Status: `draft` means unreviewed IN the inbox, so a filed "
        f"note carrying it is a filing that never restamped; `active` covers a "
        f"filed standard still gaining weight and is coherent anywhere. Living "
        f"state documents — folder covers and canonical plans — are exempt from "
        f"the `reviewed: false` case: reviewed governs knowledge content, not "
        f"live state (CONFIG § Status). Detect-only — the janitor restamps after "
        f"reading. TOTAL this run: "
        f"{sum(len(status_coherence.get(c, [])) for c in _STATUS_CASES)} across "
        f"{len(_STATUS_CASES)} cases. Sample capped at {STATUS_COHERENCE_SAMPLE} "
        f"paths per case; ## Open findings carries one rollup row per folder "
        f"scope per case. -->"
    )
    sections.append(_md_table(
        ["case", "count", f"sample (<={STATUS_COHERENCE_SAMPLE})"],
        [
            [case, len(status_coherence.get(case, [])),
             " \\| ".join(sorted(status_coherence.get(case, []))[:STATUS_COHERENCE_SAMPLE])]
            for case in _STATUS_CASES
        ],
    ))
    sections.append("")

    # --- Convention store — what the settled conventions suppressed this run ---
    sections.append("## Convention store")
    sections.append(
        "<!-- Settled conventions read from <logs>/Conventions.md (CONFIG § Log "
        "files): a finding matching a row's code AND scope predicate is suppressed "
        "at detect time and counted here — recorded, never silently dropped. The "
        "count is suppressions at the gate, which for reviewed-stale runs per "
        "citer-and-upstream PAIR, so it reads higher than the findings it would "
        "have produced (one per stale note). A row whose count stays 0 across runs "
        "has outlived its evidence; a class that keeps recurring beyond its row "
        "graduates to CONFIG via the queue. Malformed store lines are skipped and "
        "listed below the table. -->"
    )
    if conventions_path.is_file():
        sections.append(_md_table(
            ["code", "scope-predicate", "date", "suppressed-this-run"],
            [
                [crow["code"], crow["predicate"], crow["date"], convention_hits.get(i, 0)]
                for i, crow in enumerate(convention_rows)
            ],
        ))
        sections.append("")
        sections.append(
            f"Total suppressed by convention: {sum(convention_hits.values())} "
            f"across {len(convention_rows)} store rows."
        )
    else:
        sections.append(
            f"(no store at `{LOGS_REL}/Conventions.md` — suppression inert, "
            f"every detection reports)"
        )
    if convention_warnings:
        sections.append("")
        sections.append("\n".join(
            f"{ts} | {ACTOR} | convention-store-malformed | "
            f"{LOGS_REL}/Conventions.md | {w}"
            for w in convention_warnings
        ))
    sections.append("")

    sections.append("## Detector status")
    sections.append(
        "<!-- State rows (CONFIG § Log files): code | target | value. How a "
        "detector RAN this pass — it skipped for a stated reason, or truncated "
        "its report at a cap. These rows name the snapshot itself, propose no "
        "change, and are NOT open findings: there is nothing for the janitor to "
        "confirm or act on. A path under .claude/ in a skip reason is a "
        "diagnostic naming where an index would live, never a proposal to touch "
        "a kit file. -->"
    )
    if detector_status_lines:
        sections.append("\n".join(detector_status_lines))
    else:
        sections.append("(every detector ran)")
    sections.append("")

    sections.append("## Open findings")
    sections.append(
        "<!-- State rows (CONFIG § Log files): code | target | count. The count "
        "rollup is first; the per-file detail rows follow. Findings are "
        "deterministic detections; the janitor confirms before acting. "
        "Detector-status rows report in ## Detector status above, not here. -->"
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
    # LF-only via the safe-write primitive (never re-expand to CRLF on Windows) —
    # the snapshot is the highest-visibility generated artifact and the kit's
    # newline canon is LF everywhere; the atomic os.replace stays.
    write_text(tmp, content)
    os.replace(tmp, output_path)

    print(f"Vault-State-Index.md written: {output_path}")
    print(f"  Run #{run_number} at {ts}")
    print(f"  Files walked: {len(md_files_all)} .md + {len(non_md_files)} non-md")
    print(f"  Open findings: {len(open_findings)} "
          f"({len(finding_counts)} distinct code/scope rollups)")
    print(f"  Detector status: {len(detector_status)} row(s)"
          + (f" — {', '.join(sorted({c for c, _t, _v in detector_status}))}"
             if detector_status else ""))
    print(f"  Suppressed by convention: {sum(convention_hits.values())} "
          f"across {len(convention_rows)} store rows"
          + (f", {len(convention_warnings)} malformed store line(s)"
             if convention_warnings else ""))

    # No history file (CONFIG § Log files: two artifacts under <logs>, no per-run
    # files). The append-only event ledgers carry the longitudinal record; the
    # snapshot above is overwritten each run.

    print("Done.")



if __name__ == "__main__":
    main()
