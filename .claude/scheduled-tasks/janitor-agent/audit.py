"""Vault janitor audit.py.

Walks the vault, applies deterministic fixes, completes missing frontmatter via
inference, appends completed actions to the append-only event ledger, and hands
its open findings to build_state_index, which folds them into the one shared
state snapshot.

Never writes the action queue. Reads no rule file directly. All vocabulary comes
from CONFIG.md via the note-kit scripts layer.

Logging model (CONFIG § Log files — two artifacts under <logs>, no per-run
files; no second snapshot):
  - Event ledger `<logs>/janitor-agent/janitor-agent.md`, append-only: one
    `timestamp | actor | code | target | value` line per completed action. A
    dry run completes nothing, so nothing is appended.
  - State snapshot `<logs>/Vault-State-Index.md`, OWNED by build_state_index and
    overwritten each run. This script does not write the snapshot; it hands its
    run-scoped findings (inference needs, queue candidates, detections, and any
    pending/would/collision fix) to build_state_index via --findings, which
    merges them into the snapshot's ## Open findings section. The janitor reads
    that one snapshot as its work list.

build_state_index runs at the START of the pass (per the SKILL) so pass 13
drift-detection reads a fresh snapshot; it is refreshed once at the end with the
run's findings attached. BOTH refreshes are gated behind --apply: a detect-only
run writes NOTHING (no ledger, no snapshot, no directories, no mtime touches)
and prints its findings to stdout instead.

Safety rails:
  - --apply refuses to run against a root that lacks the kit's seven top-level
    folders (a snapshot/kit container, not a real vault).
  - Vault-root loose files (the user's draft space) are excluded from every pass.
  - is_asset_folder gates every decision as the first check; nothing inside an
    asset folder is typed, parented, renamed, or normalized.
  - Mutating passes run over fixed file-list snapshots, never live iterators.

Honors `compliance_exceptions: [<slug>]` per file — listed checks are skipped
and matching queue candidates are suppressed.

Environment:
    JANITOR_VAULT_ROOT — absolute path to vault root (required)

CLI flags:
    --dry-run   log-only; no filesystem writes
"""
from __future__ import annotations

import sys
import os
import re
import json
import subprocess
import tempfile
from pathlib import Path
from datetime import datetime, timezone
import yaml

# ---------------------------------------------------------------------------
# Path setup — locate kit root and inject scripts/ onto sys.path
# ---------------------------------------------------------------------------

_VAULT_JANITOR_DIR = Path(__file__).resolve().parent
# Installed layout: <kit>/scheduled-tasks/janitor-agent/audit.py -> scripts two
# levels up. The draft also adds its own dir so the v002 modules beside it
# resolve when the whole set is run from the review container.
_KIT_ROOT = _VAULT_JANITOR_DIR.parent.parent
sys.path.insert(0, str(_KIT_ROOT / "scripts"))
sys.path.insert(0, str(_VAULT_JANITOR_DIR))

from config_variables import (
    FILE_HANDLING, TYPES, FOLDER_ROUTING, SUBFOLDERS, TAGS, ACTIONS, SKILL_SLUGS,
    CANONICAL_TYPE_KEYS, CANONICAL_TAG_KEYS, SCAN_EXCLUDE_DIRS,
    _folder_by_semantic, is_excluded_dir, is_asset_folder, ASSET_HOME_DIRS,
    HISTORY_DIRNAME, token_path,
)
from wikilink_helpers import (
    normalize_link_target, extract_wikilinks, rewrite_wikilink_interior,
)
from normalize_type import normalize_type
from normalize_tag import normalize_tag
from rename_with_link_integrity import (
    rename_with_links, _find_inbound, _rewrite_inbound,
)
from index_helpers import add_child_link_to_index
from functools import lru_cache

# ---------------------------------------------------------------------------
# Vault root — required env var
# ---------------------------------------------------------------------------

vault_root_str = os.environ.get("JANITOR_VAULT_ROOT")
if not vault_root_str:
    sys.exit("JANITOR_VAULT_ROOT not set")
VAULT_ROOT = Path(vault_root_str).resolve()

# ---------------------------------------------------------------------------
# Folder resolution via config (no hardcoded folder strings)
# ---------------------------------------------------------------------------

INBOX_FOLDER = VAULT_ROOT / _folder_by_semantic("inbox")
ARCHIVE_FOLDER = VAULT_ROOT / _folder_by_semantic("archive")
# Token-table paths (CONFIG § Folders): the inbox asset-staging folder and the
# logs root, with their legacy literals as fallback for an older CONFIG.
INBOX_ASSETS_REL = token_path("inbox-assets", "00-Inbox/00-Assets")
LOGS_REL = token_path("logs", "99-Archive/99-Logs")
_ASSETS_DIR_NAME = Path(INBOX_ASSETS_REL).name   # e.g. "Assets"
_LOGS_DIR_NAME = Path(LOGS_REL).name             # e.g. "Logs"

# Directories under which a non-markdown file is a *placed* asset, not loose: the
# inbox staging (<inbox-assets>), plus the configured asset homes (`Assets`,
# legacy `02-Assets`/`99-Assets`/`<catchall>`). CONFIG § Asset folders.
_PLACED_ASSET_DIRS = frozenset({_ASSETS_DIR_NAME, "00-Assets"}) | ASSET_HOME_DIRS
PROJECTS_FOLDER = VAULT_ROOT / _folder_by_semantic("projects")

# ---------------------------------------------------------------------------
# Dry-run flag
# ---------------------------------------------------------------------------

import argparse
parser = argparse.ArgumentParser(
    description="Vault janitor — deterministic compliance pass + inference."
)
parser.add_argument("--apply", action="store_true",
                    help="Perform filesystem writes. Without this flag (or "
                         "JANITOR_APPLY=1) the run is detect-only.")
parser.add_argument("--dry-run", action="store_true",
                    help="Force detect-only; no filesystem writes. The default, "
                         "and always overrides --apply.")
args = parser.parse_args()

# Writes are gated behind an explicit opt-in: a bare `python audit.py` performs
# NO filesystem writes. Apply mode requires --apply or JANITOR_APPLY=1; --dry-run
# always wins and forces detect-only.
_apply = args.apply or os.environ.get("JANITOR_APPLY", "").strip().lower() not in ("", "0", "false")
DRY_RUN: bool = args.dry_run or not _apply

if DRY_RUN:
    print("DRY-RUN mode - no file writes will occur. "
          "Pass --apply or set JANITOR_APPLY=1 to enable writes.", file=sys.stderr)

# ---------------------------------------------------------------------------
# Kit-as-vault guard — --apply refuses to mutate anything that is not a real
# vault. A real vault carries every top-level root CONFIG § Folders declares
# (the seven roots). A folder missing them is a snapshot, a kit container, or a
# review copy — pointing --apply at one has corrupted staged files before.
# ---------------------------------------------------------------------------

_REQUIRED_ROOTS = [f for f in FOLDER_ROUTING if "*" not in f]
if not DRY_RUN:
    _missing_roots = [
        f for f in _REQUIRED_ROOTS if not (VAULT_ROOT / f).is_dir()
    ]
    if _missing_roots:
        sys.exit(
            "REFUSING --apply: JANITOR_VAULT_ROOT does not look like a real "
            f"vault — missing top-level root(s) {_missing_roots} under "
            f"{VAULT_ROOT}. The kit's roots ({', '.join(_REQUIRED_ROOTS)}) must "
            "all exist. This is probably a snapshot or kit container, not a "
            "vault. Run detect-only (no --apply) or point at the real vault."
        )

# ---------------------------------------------------------------------------
# Run log setup — canonical per-agent subfolder + YYYY-MM-DD-HHMM naming
# ---------------------------------------------------------------------------

AGENT_NAME = "janitor-agent"
log_dir = VAULT_ROOT / LOGS_REL / AGENT_NAME
# Created lazily in _write_logs, only when an event line is actually appended —
# a detect-only run writes NOTHING, directories included.

# Accumulate log lines per section
_auto_fixes_log: list[dict] = []      # keys: action, path, detail, dry
_inference_rows: list[dict] = []      # keys: inference_type, path, observed_value, candidates, context_hint
_queue_candidate_rows: list[dict] = []  # keys: rule, path, summary, suggested_options, cluster_key
_detect_log: list[dict] = []          # keys: action, path, detail


def _log_fix(rel_path: str, action: str, detail: str = "") -> None:
    # Record structured; the split into event (completed) vs finding
    # (pending/would/collision) and the formatting happen at write time.
    _auto_fixes_log.append({
        "action": action,
        "path": rel_path,
        "detail": detail,
        "dry": DRY_RUN,
    })


def _log_detect(reason: str, path: str, detail: str = "") -> None:
    """Record an unprocessed-content detection (a finding)."""
    _detect_log.append({"action": reason, "path": path, "detail": detail})


def _log_inference(inference_type: str, path: str, observed_value: str = "",
                   candidates: str = "", context_hint: str = "") -> None:
    _inference_rows.append({
        "inference_type": inference_type,
        "path": path,
        "observed_value": observed_value,
        "candidates": candidates,
        "context_hint": context_hint,
    })


def _log_queue(rule: str, path: str, summary: str,
               suggested_options: str = "", cluster_key: str = "") -> None:
    _queue_candidate_rows.append({
        "rule": rule,
        "path": path,
        "summary": summary,
        "suggested_options": suggested_options,
        "cluster_key": cluster_key,
    })


# ---------------------------------------------------------------------------
# compliance_exceptions — per-file check suppression
# ---------------------------------------------------------------------------

def _exceptions_for(fm: dict | None) -> set[str]:
    """Return the set of compliance-check slugs this file opts out of.

    Honors `compliance_exceptions: [<slug>]` (list) or a single string value.
    """
    if not fm:
        return set()
    raw = fm.get("compliance_exceptions")
    if raw is None:
        return set()
    if isinstance(raw, str):
        return {s.strip() for s in re.split(r"[,\s]+", raw) if s.strip()}
    if isinstance(raw, (list, tuple)):
        return {str(s).strip() for s in raw if str(s).strip()}
    return set()


def _excepted(slug: str, exceptions: set[str]) -> bool:
    return slug in exceptions


# ---------------------------------------------------------------------------
# Hands-off filtering
# ---------------------------------------------------------------------------

def _build_hands_off_patterns() -> list[tuple[str, re.Pattern[str]]]:
    """Build regex patterns from FOLDER_ROUTING hands_off_patterns, per folder.

    Inbox skill-container patterns (those using a `<name>` placeholder, e.g.
    `*-research/`) are expanded only for slugs that are real skill containers
    (SKILL_SLUGS with inbox_container=True), so a stray inbox subfolder that
    does not match a known skill slug reaches the unprocessed-content detector
    rather than being silently exempted.
    """
    # Collect the set of container slug suffixes for inbox pattern expansion.
    _container_slugs: set[str] = {
        slug for slug, row in SKILL_SLUGS.items() if row.inbox_container
    }

    patterns: list[tuple[str, re.Pattern[str]]] = []
    inbox_folder = _folder_by_semantic("inbox")

    for folder_str, row in FOLDER_ROUTING.items():
        is_inbox_folder = (folder_str == inbox_folder)
        for raw_pattern in row.hands_off_patterns:
            raw = raw_pattern.strip()
            if not raw:
                continue

            # For inbox skill-container globs (patterns containing a <name>
            # placeholder, e.g. `*-research/`), build one literal pattern per
            # known container slug rather than a blanket [^/]+ wildcard.
            if is_inbox_folder and "<" in raw and ">" in raw and raw.endswith("/"):
                folder_prefix = re.escape(folder_str)
                # Extract the prefix/suffix around the placeholder
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
                continue  # do not fall through to the generic path for this pattern

            # All other patterns: replace <name> placeholders with a single-
            # segment wildcard (the general case for non-inbox folders).
            if "<" in raw and ">" in raw:
                parts = re.split(r"<[^>]+>", raw)
                escaped = "[^/]+".join(re.escape(p) for p in parts)
            else:
                escaped = re.escape(raw)
            # Anchor under the folder's path. A trailing slash means "anything
            # inside this folder"; otherwise match the named child exactly.
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


_HANDS_OFF_PATTERNS = _build_hands_off_patterns()

# Vault-root README — operational document, never swept (CONFIG § File handling
# frontmatter exceptions, § Operational documents).
_README_FILENAME = "README.md"


@lru_cache(maxsize=None)
def _dir_is_asset(dir_path_str: str) -> bool:
    return is_asset_folder(Path(dir_path_str))


def _in_asset_folder(path: Path) -> bool:
    """True when any ancestor directory (below the vault root) is a classified
    asset folder (CONFIG § Asset folders). This is the FIRST gate on every
    stamp/normalize/rename decision: nothing inside an asset folder is typed,
    parented, renamed, or normalized. The walk already prunes asset folders on
    descent; this per-path check covers every other entry point (direct path
    handling, index registration, move targets)."""
    cur = path if path.is_dir() else path.parent
    while True:
        try:
            cur.relative_to(VAULT_ROOT)
        except ValueError:
            return False
        if cur == VAULT_ROOT:
            return False
        if _dir_is_asset(str(cur)):
            return True
        cur = cur.parent


def _is_hands_off(path: Path) -> bool:
    """Return True if the path matches any folder's hands_off_patterns.

    Also returns True for:
    - any LOOSE FILE at the vault root — the root is the user's draft space
      (CONFIG § Folders, § File handling `<vault-root>/*`); no pass may move,
      rename, stamp, or flag a root-level file (a prior incident moved an
      in-use root draft to the inbox);
    - anything inside a classified asset folder (CONFIG § Asset folders) —
      checked first, before any other decision;
    - the vault-root README.md — an operational document (CONFIG § File
      handling frontmatter exceptions, § Operational documents).
    """
    # Asset-folder gate FIRST (CONFIG § Asset folders: the predicate is the
    # first check, before any inference, stamp, or in-folder action).
    if _in_asset_folder(path):
        return True
    # Scan-exclusion (CONFIG § Folders / Scan exclusions): any dot-directory
    # segment — `.claude` (the kit's own install dir), `.git`, `.obsidian`,
    # `.trash` — takes the path out of the walk entirely.
    if any(is_excluded_dir(part) for part in path.parts):
        return True
    # Vault-root loose files are the user's draft space — excluded from EVERY
    # pass. (Directories at the root are still walked; only files are exempt.)
    if path.parent == VAULT_ROOT and not path.is_dir():
        return True
    try:
        rel = str(path.relative_to(VAULT_ROOT)).replace("\\", "/")
    except ValueError:
        return False
    for _raw, pat in _HANDS_OFF_PATTERNS:
        if pat.search(rel):
            return True
    # Also skip system paths
    parts = path.parts
    for part in parts:
        if part == "__pycache__":
            return True
    if path.suffix == ".pyc":
        return True
    return False


# ---------------------------------------------------------------------------
# Frontmatter parsing helpers
# ---------------------------------------------------------------------------

def _split_frontmatter_text(text: str) -> tuple[str | None, str]:
    """Return (frontmatter_block, body). frontmatter_block is None if absent."""
    if not text.startswith("---"):
        return None, text
    lines = text.split("\n")
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_block = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1:])
            return fm_block, body
    return None, text


def _try_yaml_repairs(fm_block: str) -> dict | None:
    """Attempt four YAML repairs in order. Return parsed dict or None."""
    repairs = [
        # Repair 1: bare wikilinks not quoted
        lambda s: re.sub(
            r'(\w[\w\-]*)\s*:\s*(\[\[[^\]]+\]\])\s*$',
            lambda m: f'{m.group(1)}: "{m.group(2)}"',
            s, flags=re.MULTILINE
        ),
        # Repair 2: tab indents → two spaces
        lambda s: re.sub(r'^\t', '  ', s, flags=re.MULTILINE),
        # Repair 3: unclosed quotes — close at end of offending line
        lambda s: re.sub(
            r'^(\s*\w[\w\-]*\s*:\s*"[^"\n]*)$',
            lambda m: m.group(1) + '"',
            s, flags=re.MULTILINE
        ),
        # Repair 4: multi-word bare string on type field
        lambda s: re.sub(
            r'^(type\s*:\s*)(.+)$',
            lambda m: (
                f"{m.group(1)}{normalize_type(w) or w}"
                if (
                    (words := m.group(2).strip().split())
                    and len(words) >= 1
                    and (w := words[0])
                    and normalize_type(w)
                )
                else m.group(0)
            ),
            s, flags=re.MULTILINE
        ),
    ]
    current = fm_block
    for repair_fn in repairs:
        current = repair_fn(current)
        try:
            result = yaml.safe_load(current)
            if isinstance(result, dict):
                return result
        except yaml.YAMLError:
            continue
    return None


def _parse_frontmatter(text: str) -> tuple[dict | None, str, str | None]:
    """
    Parse frontmatter from text.
    Returns (fm_dict, body, error_msg).
    fm_dict is None if parsing failed; error_msg is None if successful.
    """
    fm_block, body = _split_frontmatter_text(text)
    if fm_block is None:
        return {}, body, None  # no frontmatter block at all; treat as empty dict

    try:
        result = yaml.safe_load(fm_block)
        if isinstance(result, dict):
            return result, body, None
        if result is None:
            return {}, body, None
        return None, body, f"frontmatter is not a mapping (got {type(result).__name__})"
    except yaml.YAMLError as exc:
        # Try repairs
        repaired = _try_yaml_repairs(fm_block)
        if repaired is not None:
            return repaired, body, None
        mark = getattr(exc, "problem_mark", None)
        loc = f" at line {mark.line + 1}, col {mark.column + 1}" if mark else ""
        problem = getattr(exc, "problem", str(exc))
        return None, body, f"{problem}{loc}"


# ---------------------------------------------------------------------------
# Frontmatter write helper
# ---------------------------------------------------------------------------

def _write_frontmatter(file_path: Path, fm: dict, body: str) -> None:
    """Write updated frontmatter + body to file. Preserves body exactly."""
    content = "---\n" + yaml.dump(fm, allow_unicode=True, default_flow_style=False) + "---\n" + body
    if not DRY_RUN:
        file_path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Relative path helper
# ---------------------------------------------------------------------------

def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(VAULT_ROOT)).replace("\\", "/")
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# Title-case-hyphens helper (for naming pass)
# ---------------------------------------------------------------------------

_ACRONYMS = {"VEX", "RBD", "FX", "CVF", "LLM", "API", "SDK", "CLI",
             "URL", "JSON", "HTTP", "AI", "ML", "UI", "UX", "MCP",
             "GPU", "CPU", "RAM", "OS"}
_VERSION_RE = re.compile(r"^v\d+$")

# Headline-case minor words: lowercased when they fall between the first and
# last word of a name, capitalized when first or last. Articles, coordinating
# conjunctions, and short prepositions. Keeping them lowercase mid-name lets a
# sentence-like title read as a title, e.g. "How-to-Configure-the-Build". A
# short structural name like "Action-Queue" has no interior minor word, so
# every word stays capitalized.
_MINOR_WORDS = {
    "a", "an", "and", "as", "at", "but", "by", "en", "for", "if", "in",
    "nor", "of", "on", "or", "per", "so", "the", "to", "via", "vs", "yet",
}


def _title_case(stem: str) -> str:
    """Headline-case a hyphen/space/underscore-delimited stem.

    Capitalize every word except interior minor words (`_MINOR_WORDS`), which
    stay lowercase. The first and last word are always capitalized. Known
    acronyms keep their all-caps form and `vNNN` version tokens stay lowercase,
    at any position. Pure and deterministic: the same stem always maps to the
    same result, so the janitor's naming pass enforces it identically every run.
    """
    tokens = [tok for tok in re.split(r"[-\s_]+", stem) if tok]
    last = len(tokens) - 1
    result: list[str] = []
    for i, tok in enumerate(tokens):
        if _VERSION_RE.match(tok):
            result.append(tok.lower())
        elif tok.upper() in _ACRONYMS:
            result.append(tok.upper())
        elif 0 < i < last and tok.lower() in _MINOR_WORDS:
            result.append(tok.lower())
        else:
            result.append(tok[0].upper() + tok[1:] if len(tok) > 1 else tok.upper())
    return "-".join(result)


def _levenshtein_le1(a: str, b: str) -> bool:
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(ca != cb for ca, cb in zip(a, b)) <= 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(shorter) and shorter[i] == longer[i]:
        i += 1
    return shorter[i:] == longer[i + 1:]


# ---------------------------------------------------------------------------
# Build the full basename index and inbound-link graph
# ---------------------------------------------------------------------------

def _build_indices(
    all_files: list[Path],
) -> tuple[dict[str, list[Path]], dict[str, set[str]]]:
    """
    Returns:
        basename_index: stem -> list[Path]
        inbound_index: stem -> set of posix_rel of referencing files
    """
    basename_index: dict[str, list[Path]] = {}
    for p in all_files:
        basename_index.setdefault(p.stem, []).append(p)

    inbound_index: dict[str, set[str]] = {}
    for p in all_files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        rel = _rel(p)
        for link in extract_wikilinks(text):
            inbound_index.setdefault(link, set()).add(rel)

    return basename_index, inbound_index


# ---------------------------------------------------------------------------
# Vault walk
# ---------------------------------------------------------------------------

def _iter_vault_files():
    """Yield every file under the vault, pruning excluded directories on descent.

    Uses os.walk so a directory whose name is excluded (CONFIG § Folders / Scan
    exclusions — any dot-directory, e.g. `.claude`, `.git`, `.obsidian`,
    `.trash`) is removed from `dirnames` in place and never entered. A file
    inside such a directory is therefore never enumerated at all.
    """
    for dirpath, dirnames, filenames in os.walk(VAULT_ROOT):
        # Prune excluded and asset directories in place so the walk does not
        # descend (CONFIG § Asset folders: a classified asset's interior is never
        # enumerated, linted, or flagged).
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded_dir(d) and not is_asset_folder(Path(dirpath) / d)
        ]
        for fname in filenames:
            yield Path(dirpath) / fname


def _walk_vault() -> list[Path]:
    """Collect all .md files in the vault (excluding hands-off paths)."""
    results: list[Path] = []
    for p in _iter_vault_files():
        if p.suffix.lower() != ".md":
            continue
        if _is_hands_off(p):
            continue
        results.append(p)
    return results


def _walk_non_md() -> list[Path]:
    """Collect non-.md files for loose-asset pass (pass 10)."""
    results: list[Path] = []
    for p in _iter_vault_files():
        if p.suffix.lower() == ".md":
            continue
        if _is_hands_off(p):
            continue
        results.append(p)
    return results


# ---------------------------------------------------------------------------
# Pass 1 — YAML parse and repair
# ---------------------------------------------------------------------------

def pass1_yaml_parse(path: Path, text: str) -> tuple[dict | None, str, bool]:
    """
    Returns (fm, body, parse_ok).
    If parse_ok is False, the file should be skipped for further passes.
    """
    fm, body, err = _parse_frontmatter(text)
    if err:
        _log_inference(
            inference_type="type-resolution",
            path=_rel(path),
            observed_value="yaml-parse-failure",
            context_hint=err,
        )
        return None, body, False
    return fm, body, True


# ---------------------------------------------------------------------------
# Pass 2 — Type normalization
# ---------------------------------------------------------------------------

def pass2_type_normalize(path: Path, fm: dict) -> tuple[dict, bool]:
    """Normalize type field in-place. Returns (fm, changed)."""
    raw = fm.get("type")
    if raw is None:
        return fm, False
    canonical = normalize_type(str(raw))
    if canonical is None:
        return fm, False
    if canonical != str(raw):
        fm = dict(fm)
        fm["type"] = canonical
        _log_fix(_rel(path), "type-normalized", f"{raw!r} -> {canonical!r}")
        return fm, True
    return fm, False


# ---------------------------------------------------------------------------
# Pass 3 — Tag normalization
# ---------------------------------------------------------------------------

def pass3_tag_normalize(path: Path, fm: dict, exceptions: set[str]) -> tuple[dict, bool]:
    """Normalize tags list. Returns (fm, changed)."""
    raw_tags = fm.get("tags")
    if not isinstance(raw_tags, list):
        return fm, False

    new_tags: list = []
    changed = False
    for tag in raw_tags:
        tag_str = str(tag)
        if tag_str in CANONICAL_TAG_KEYS:
            new_tags.append(tag_str)
            continue
        canonical = normalize_tag(tag_str)
        if canonical:
            new_tags.append(canonical)
            if canonical != tag_str:
                changed = True
                _log_fix(_rel(path), "tag-normalized", f"{tag_str!r} -> {canonical!r}")
        else:
            # Open vocabulary (CONFIG § Tags: the list grows over time). A tag
            # that is neither a known alias nor a Levenshtein-1 typo of a
            # canonical tag is a new domain tag, not a fault. Accept it as-is;
            # near-duplicate consolidation is the analyst's frequency-aware call,
            # not a per-file finding the janitor emits.
            new_tags.append(tag_str)

    if changed:
        fm = dict(fm)
        fm["tags"] = new_tags
    return fm, changed


# ---------------------------------------------------------------------------
# Pass 4 — Type inference ladder
# ---------------------------------------------------------------------------

_WIKILINK_RE = re.compile(r"(?<!!)\[\[([^\[\]]+?)\]\]")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _count_code_blocks(body: str) -> int:
    return len(_CODE_FENCE_RE.findall(body))


def _count_wikilinks(body: str) -> int:
    return len(_WIKILINK_RE.findall(body))


def _count_lines(body: str) -> int:
    return len([l for l in body.splitlines() if l.strip()])


def _infer_type(path: Path, fm: dict, body: str) -> str | None:
    """Run the 4-level inference ladder. Returns inferred type or None."""
    # Level 1: Folder match
    for folder_str, row in FOLDER_ROUTING.items():
        folder_path = VAULT_ROOT / folder_str
        try:
            path.relative_to(folder_path)
            in_this_folder = True
        except ValueError:
            in_this_folder = False

        if in_this_folder and len(row.type_defaults) == 1:
            return row.type_defaults[0]

    # Level 2: Filename / structure signals
    stem = path.stem
    parent_name = path.parent.name

    if parent_name == "Sessions" and _DATE_PREFIX_RE.match(stem):
        return "session"

    total_lines = _count_lines(body)
    wikilink_count = _count_wikilinks(body)
    if total_lines > 0 and wikilink_count / max(total_lines, 1) >= 0.8:
        return "index"

    if _count_code_blocks(body) >= 3:
        return "snippet"

    # Level 3: Content signals
    # wikilinks to projects + date-stamped structure → session
    has_project_fm = bool(fm.get("project"))
    if has_project_fm and _DATE_PREFIX_RE.match(stem):
        return "session"

    # body parent: pointing to existing file → reference or note
    parent_match = re.search(r"^[Pp]arent:\s*\[\[([^\[\]]+?)\]\]", body, re.MULTILINE)
    if parent_match:
        target = normalize_link_target(parent_match.group(1))
        if target:
            return "reference"

    # Level 4: Default — if folder's type_defaults is ['note']
    for folder_str, row in FOLDER_ROUTING.items():
        folder_path = VAULT_ROOT / folder_str
        try:
            path.relative_to(folder_path)
            in_this_folder = True
        except ValueError:
            in_this_folder = False
        if in_this_folder and list(row.type_defaults) == ["note"]:
            return "note"

    return None


def pass4_type_inference(path: Path, fm: dict, body: str) -> tuple[dict, bool]:
    """Infer type if missing. Returns (fm, changed)."""
    if fm.get("type"):
        return fm, False

    inferred = _infer_type(path, fm, body)
    if inferred:
        fm = dict(fm)
        fm["type"] = inferred
        tags = list(fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        fm["tags"] = tags
        _log_fix(_rel(path), "type-inferred", f"-> {inferred!r}")
        return fm, True

    _log_inference(
        inference_type="type-resolution",
        path=_rel(path),
        observed_value="(no type set)",
        context_hint="inference ladder exhausted",
    )
    return fm, False


# ---------------------------------------------------------------------------
# Pass 5 — Type-folder match
# ---------------------------------------------------------------------------

def _get_expected_folder(type_key: str) -> list[str]:
    """Return list of folder path-segment patterns valid for this type."""
    valid: list[str] = []
    for folder_str, row in FOLDER_ROUTING.items():
        if type_key in row.type_defaults:
            valid.append(folder_str)
    return valid


def pass5_type_folder_match(
    path: Path, fm: dict, basename_index: dict[str, list[Path]]
) -> tuple[Path, dict, bool]:
    """Move file if type-folder mismatch. Returns (new_path, fm, changed)."""
    rel = _rel(path)
    ftype = fm.get("type")
    if not ftype:
        return path, fm, False

    # Skip inbox and archive — they're transit zones
    try:
        path.relative_to(INBOX_FOLDER)
        return path, fm, False
    except ValueError:
        pass
    try:
        path.relative_to(ARCHIVE_FOLDER)
        return path, fm, False
    except ValueError:
        pass

    expected_folders = _get_expected_folder(ftype)
    if not expected_folders:
        return path, fm, False

    # Check if current location is valid
    for folder_str in expected_folders:
        folder_path = VAULT_ROOT / folder_str
        try:
            path.relative_to(folder_path)
            return path, fm, False  # already in a valid folder
        except ValueError:
            continue

    if len(expected_folders) == 1:
        # Single deterministic destination
        dest_folder = VAULT_ROOT / expected_folders[0]
        dest = dest_folder / path.name
        if dest.exists():
            _log_fix(rel, "type-folder-mismatch-collision", _rel(dest))
            return path, fm, False
        if not DRY_RUN:
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = rename_with_links(path, dest, VAULT_ROOT)
            if result.status in ("renamed", "no-op"):
                _log_fix(rel, "type-folder-relocated", f"-> {_rel(dest)}")
                # Tag auto-fixed
                new_fm = dict(fm)
                tags = list(new_fm.get("tags") or [])
                if "auto-fixed" not in tags:
                    tags.append("auto-fixed")
                new_fm["tags"] = tags
                return dest, new_fm, True
            else:
                _log_fix(rel, f"type-folder-relocation-failed: {result.error}")
                return path, fm, False
        else:
            _log_fix(rel, "type-folder-mismatch", f"[dry-run] would move -> {_rel(dest)}")
            return path, fm, False
    else:
        _log_inference(
            inference_type="parent-finding",
            path=rel,
            observed_value=ftype,
            candidates=", ".join(expected_folders),
            context_hint="multiple valid folders; cannot determine destination",
        )
        return path, fm, False


# ---------------------------------------------------------------------------
# Pass 6 — Naming normalization
# ---------------------------------------------------------------------------

def _canonical_filename(path: Path, fm: dict) -> str | None:
    """Return canonical filename (with .md) or None if already correct."""
    stem = path.stem
    ftype = fm.get("type") or ""

    if ftype == "snippet":
        canonical_stem = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")
        if canonical_stem == stem:
            return None
        return f"{canonical_stem}.md"

    if ftype in ("session", "journal"):
        if _DATE_PREFIX_RE.match(stem) and re.match(r"^\d{4}-\d{2}-\d{2}-[a-z0-9-]+$", stem):
            return None
        # Build from date field + slug
        date_raw = fm.get("date")
        if date_raw:
            date_str = str(date_raw)[:10]
            if _DATE_PREFIX_RE.match(date_str):
                slug_src = re.sub(r"^\d{4}-\d{2}-\d{2}-?", "", stem)
                slug = re.sub(r"[^a-z0-9]+", "-", slug_src.lower()).strip("-")
                if slug:
                    canonical = f"{date_str}-{slug}.md"
                    if canonical == path.name:
                        return None
                    return canonical
        return None

    # Title-Case-Hyphens for everything else
    canonical_stem = _title_case(stem)
    if canonical_stem == stem:
        return None
    return f"{canonical_stem}.md"


def pass6_naming(path: Path, fm: dict) -> tuple[Path, bool]:
    """Rename if naming convention violated. Returns (new_path, changed)."""
    rel = _rel(path)

    # Skip inbox (filing agent owns it) and archive
    try:
        path.relative_to(INBOX_FOLDER)
        return path, False
    except ValueError:
        pass
    try:
        path.relative_to(ARCHIVE_FOLDER)
        return path, False
    except ValueError:
        pass

    canonical = _canonical_filename(path, fm)
    if not canonical:
        return path, False

    # Check if it's a near-canonical Levenshtein-1 variant (still rename it)
    dest = path.parent / canonical

    # Case-only rename: on Windows' case-insensitive filesystem, dest.exists()
    # matches the SOURCE itself when the destination differs only by case
    # (e.g. `-And-` -> `-and-`). That is NOT a collision — only a genuinely
    # different existing file is. Detect it by name-casefold equality plus a
    # samefile check, and perform it via a two-step temp rename (a direct
    # rename is refused by rename_with_links' dest-exists validation).
    case_only = (
        dest.name != path.name and dest.name.lower() == path.name.lower()
    )
    if dest.exists():
        same_file = False
        if case_only:
            try:
                same_file = os.path.samefile(path, dest)
            except OSError:
                same_file = False
        if not (case_only and same_file):
            # A genuinely different file already holds the canonical name.
            _log_fix(rel, "naming-collision", canonical)
            return path, False
        # Case-only self-match: fall through to the rename below.
        if not DRY_RUN:
            tmp = path.with_name(f"{path.stem}.case-rename.tmp{path.suffix}")
            try:
                path.rename(tmp)
                tmp.rename(dest)
            except OSError as exc:
                # Roll back the half-step if possible, then report.
                try:
                    if tmp.exists() and not dest.exists():
                        tmp.rename(path)
                except OSError:
                    pass
                _log_fix(rel, f"naming-rename-failed: case-only two-step: {exc}")
                return path, False
            # Rewrite inbound wikilinks to the canonical casing so the exact-
            # stem indices (basename/inbound) keep resolving next run.
            old_stem, new_stem = path.stem, dest.stem
            try:
                for ref_file, _cnt in _find_inbound(VAULT_ROOT, old_stem, exclude=dest):
                    _rewrite_inbound(ref_file, old_stem, new_stem)
            except Exception as exc:
                _log_fix(rel, "naming-renamed",
                         f"-> {canonical} (link recase incomplete: {exc})")
                return dest, True
            _log_fix(rel, "naming-renamed", f"-> {canonical} (case-only)")
            return dest, True
        else:
            _log_fix(rel, "naming-would-rename", f"[dry-run] -> {canonical} (case-only)")
            return path, False

    if not DRY_RUN:
        result = rename_with_links(path, dest, VAULT_ROOT)
        if result.status in ("renamed", "no-op"):
            _log_fix(rel, "naming-renamed", f"-> {canonical}")
            return dest, True
        else:
            _log_fix(rel, f"naming-rename-failed: {result.error}")
            return path, False
    else:
        _log_fix(rel, "naming-would-rename", f"[dry-run] -> {canonical}")
        return path, False


# ---------------------------------------------------------------------------
# Pass 7 — Parent inference and backlink stamping
# ---------------------------------------------------------------------------

def _find_parent_from_folder(path: Path) -> str | None:
    """Level 1: folder hints. Inside {projects}/X/ → X's index."""
    try:
        rel_to_projects = path.relative_to(PROJECTS_FOLDER)
        parts = rel_to_projects.parts
        if len(parts) >= 2:
            project_name = parts[0]
            # Look for a Project.md or index inside this project folder
            project_folder = PROJECTS_FOLDER / project_name
            candidates = list(project_folder.glob("*.md"))
            for c in candidates:
                if c.stem == project_name or c.stem.endswith("-Index"):
                    return c.stem
            if candidates:
                return candidates[0].stem
    except ValueError:
        pass
    return None


def _find_parent_from_body(body: str, basename_index: dict[str, list[Path]]) -> str | None:
    """Level 2: topic hints — scan body wikilinks against existing indexes."""
    for m in _WIKILINK_RE.finditer(body):
        target = normalize_link_target(m.group(1))
        if not target:
            continue
        targets = basename_index.get(target, [])
        for t in targets:
            try:
                text = t.read_text(encoding="utf-8", errors="replace")
                fm2, _, _ = _parse_frontmatter(text)
                if fm2 and fm2.get("type") == "index":
                    return target
            except Exception:
                continue
    return None


def pass7_parent_inference(
    path: Path, fm: dict, body: str, basename_index: dict[str, list[Path]]
) -> tuple[dict, str, bool]:
    """Infer and stamp parent: if needed. Returns (fm, body, changed)."""
    rel = _rel(path)
    ftype = fm.get("type") or ""

    # Check if parent: is required for this type
    type_row = TYPES.get(ftype)
    if type_row is None:
        return fm, body, False
    needs_parent = "parent" in type_row.additional_frontmatter

    if not needs_parent:
        return fm, body, False

    # Check frontmatter for an existing parent: value (the documented location).
    # Fall back to a body scan only when the frontmatter carries no parent key.
    fm_parent_raw = fm.get("parent")
    if fm_parent_raw is not None:
        fm_parent = str(fm_parent_raw).strip()
        if fm_parent:
            # Extract the wikilink target from the frontmatter value, if any.
            fm_link = re.search(r"\[\[([^\[\]]+?)\]\]", fm_parent)
            target = normalize_link_target(fm_link.group(1)) if fm_link else fm_parent
            if target and target not in basename_index:
                _log_inference("parent-orphan", rel, observed_value=target,
                               context_hint="parent target not found in vault")
            return fm, body, False

    # No frontmatter parent: — check body as a secondary source.
    existing_parent = re.search(r"^[Pp]arent:\s*\[\[([^\[\]]+?)\]\]", body, re.MULTILINE)
    if existing_parent:
        target = normalize_link_target(existing_parent.group(1))
        # Check target exists
        if target and target not in basename_index:
            _log_inference("parent-orphan", rel, observed_value=target,
                           context_hint="parent target not found in vault")
        # Check for folder mismatch
        return fm, body, False

    # No parent: — try to infer
    inferred_parent = None

    # Level 1: folder hints
    inferred_parent = _find_parent_from_folder(path)

    # Level 2: topic hints from body
    if not inferred_parent:
        inferred_parent = _find_parent_from_body(body, basename_index)

    if inferred_parent:
        parent_line = f"\nparent: [[{inferred_parent}]]\n"
        # Insert after H1 if present
        h1_match = re.search(r"^# .+$", body, re.MULTILINE)
        if h1_match:
            insert_at = h1_match.end()
            new_body = body[:insert_at] + parent_line + body[insert_at:]
        else:
            new_body = parent_line + body
        new_fm = dict(fm)
        tags = list(new_fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        new_fm["tags"] = tags
        _log_fix(rel, "parent-inferred", f"parent: [[{inferred_parent}]]")
        return new_fm, new_body, True

    # Level 3: emit to inference needs
    _log_inference("parent-finding", rel, observed_value="(no parent:)",
                   context_hint="could not resolve parent from folder or body links")
    return fm, body, False


# ---------------------------------------------------------------------------
# Pass 8 — Index child registration
# ---------------------------------------------------------------------------

def pass8_index_children(
    path: Path, fm: dict, body: str, basename_index: dict[str, list[Path]]
) -> None:
    """Register file in its parent index if parent: points to one."""
    rel = _rel(path)
    parent_match = re.search(r"^[Pp]arent:\s*\[\[([^\[\]]+?)\]\]", body, re.MULTILINE)
    if not parent_match:
        return

    target = normalize_link_target(parent_match.group(1))
    if not target:
        return

    index_paths = basename_index.get(target, [])
    for index_path in index_paths:
        try:
            text = index_path.read_text(encoding="utf-8", errors="replace")
            index_fm, _, _ = _parse_frontmatter(text)
            if not (index_fm and index_fm.get("type") == "index"):
                continue
        except Exception:
            continue

        if not DRY_RUN:
            try:
                added = add_child_link_to_index(index_path, path.stem)
                if added:
                    _log_fix(rel, "index-child-registered", f"-> [[{target}]]")
            except Exception as exc:
                _log_fix(rel, f"index-child-registration-failed: {exc}")
        else:
            _log_fix(rel, "index-child-would-register", f"[dry-run] -> [[{target}]]")
        break  # only register in first matching index

    # Check for index files: zero children or overflow
    if fm.get("type") == "index":
        children = list(_WIKILINK_RE.finditer(body))
        if len(children) == 0:
            _log_inference("index-empty", rel, context_hint="index has no children")
        elif len(children) > 40:
            _log_inference("index-overflow", rel,
                           observed_value=str(len(children)),
                           context_hint="index has >40 children; consider splitting")


# ---------------------------------------------------------------------------
# Pass 9 — Orphan detection
# ---------------------------------------------------------------------------

def pass9_orphan_detection(
    path: Path, fm: dict, body: str,
    basename_index: dict[str, list[Path]],
    inbound_index: dict[str, set[str]],
) -> None:
    """Move orphaned filed files to inbox."""
    rel = _rel(path)
    ftype = fm.get("type") or ""

    # Skip inbox
    try:
        path.relative_to(INBOX_FOLDER)
        return
    except ValueError:
        pass

    # Skip types that need no uplink (areas, index, log)
    if ftype in ("area", "index", "log"):
        return

    # Skip if stem ends with -Index and catch-all exists
    if path.stem.endswith("-Index") and path.stem in basename_index:
        return

    # Check reachability — parent may be in frontmatter (the documented field)
    # or as a legacy body line; either counts as a declared parent.
    fm_parent_raw = fm.get("parent")
    has_parent = bool(
        (fm_parent_raw is not None and str(fm_parent_raw).strip())
        or re.search(r"^[Pp]arent:\s*\[\[[^\[\]\s]", body, re.MULTILINE)
    )
    project_val = fm.get("project") or ""
    has_project = bool(isinstance(project_val, str) and re.search(r"\[\[[^\[\]\s]", project_val))
    inbound = inbound_index.get(path.stem, set())
    non_self_inbound = {s for s in inbound if s != rel}

    if has_parent or has_project or non_self_inbound:
        return

    # Orphan detected — move to inbox
    dest = INBOX_FOLDER / path.name
    if not DRY_RUN:
        if not dest.exists():
            result = rename_with_links(path, dest, VAULT_ROOT)
            if result.status in ("renamed", "no-op"):
                # Update frontmatter on the destination
                try:
                    dest_text = dest.read_text(encoding="utf-8", errors="replace")
                    dest_fm, dest_body, _ = _parse_frontmatter(dest_text)
                    if dest_fm is not None:
                        dest_fm = dict(dest_fm)
                        dest_fm["reviewed"] = False
                        tags = list(dest_fm.get("tags") or [])
                        if "inferred" not in tags:
                            tags.append("inferred")
                        dest_fm["tags"] = tags
                        _write_frontmatter(dest, dest_fm, dest_body)
                except Exception:
                    pass
                _log_fix(rel, "orphan-moved-to-inbox", f"-> {_rel(dest)}")
            else:
                _log_fix(rel, f"orphan-move-failed: {result.error}")
        else:
            _log_fix(rel, "orphan-inbox-collision", path.name)
    else:
        _log_fix(rel, "orphan-would-move-to-inbox", f"[dry-run] -> {_rel(dest)}")


# ---------------------------------------------------------------------------
# Pass 10 — Loose asset relocation
# ---------------------------------------------------------------------------

def pass10_loose_assets(all_md: list[Path], all_non_md: list[Path]) -> None:
    """Relocate orphaned non-.md assets to `<inbox-assets>`.

    Per CONFIG § File handling: orphaned non-markdown assets (not already under
    a placed-asset folder) route to `<inbox-assets>` for the user to place —
    never the bare inbox root, never a new top-level folder. Assets are
    frontmatter-exempt: this pass moves the file only and never stamps `type`,
    `tags`, `date`, or any metadata on it.

    Mutation safety: both loops run over the fixed file-list snapshots taken
    before any change (`all_md`, `all_non_md`) — never a live walk iterator.
    """
    # Build referenced asset names from the snapshot's md files
    referenced: set[str] = set()
    asset_ref_re = re.compile(
        r"!\[\[([^\[\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
        r"|!\[[^\]]*\]\(([^)\s]+?)(?:\s+[^)]+)?\)"
        r"|\[\[([^\[\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
    )
    for md_path in all_md:
        try:
            text = md_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for m in asset_ref_re.finditer(text):
            ref = m.group(1) or m.group(2) or m.group(3) or ""
            if ref:
                referenced.add(ref.strip())
                referenced.add(Path(ref).name)

    # Orphan assets route to `<inbox-assets>` (CONFIG § File handling) — not
    # the bare inbox root.
    orphan_folder = VAULT_ROOT / INBOX_ASSETS_REL

    for asset_path in all_non_md:
        # Skip if in inbox or any project subtree
        try:
            asset_path.relative_to(INBOX_FOLDER)
            continue
        except ValueError:
            pass
        try:
            asset_path.relative_to(PROJECTS_FOLDER)
            continue
        except ValueError:
            pass
        try:
            asset_path.relative_to(ARCHIVE_FOLDER)
            continue
        except ValueError:
            pass

        # Skip assets already living under a placed-asset home (the inbox
        # staging folder or a CONFIG § Asset folders home) — they are placed.
        if _PLACED_ASSET_DIRS & set(asset_path.parts):
            continue

        name = asset_path.name
        stem = asset_path.stem
        if name in referenced or stem in referenced:
            continue

        dest = orphan_folder / name
        if dest.exists():
            date_prefix = datetime.now(timezone.utc).strftime("%Y%m%d")
            dest = orphan_folder / f"{date_prefix}-{name}"

        if not DRY_RUN:
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil
                shutil.move(str(asset_path), str(dest))
                _log_fix(_rel(asset_path), "loose-asset-relocated", f"-> {_rel(dest)}")
            except Exception as exc:
                _log_fix(_rel(asset_path), f"loose-asset-move-failed: {exc}")
        else:
            _log_fix(_rel(asset_path), "loose-asset-would-relocate",
                     f"[dry-run] -> {_rel(dest)}")


# ---------------------------------------------------------------------------
# Pass 11 — Missing project folder
# ---------------------------------------------------------------------------

def pass11_project_folder(path: Path, fm: dict, basename_index: dict[str, list[Path]]) -> None:
    """Handle project: frontmatter references pointing at nonexistent folders."""
    rel = _rel(path)
    project_val = fm.get("project") or ""
    if not isinstance(project_val, str):
        return
    m = re.search(r"\[\[([^\[\]]+?)\]\]", project_val)
    if not m:
        return
    proj_name = normalize_link_target(m.group(1))
    if not proj_name:
        return

    proj_folder = PROJECTS_FOLDER / proj_name
    if proj_folder.is_dir():
        return

    # The `project:` value is a WIKILINK, not a literal folder name — e.g.
    # `project: "[[X]]"` points at the project's folder-note cover
    # `<projects>/X/X.md` (legacy: `00-X.md`). The project exists when the link target
    # resolves anywhere under the projects root; the check is link
    # resolution, not folder-name equality. (Fixes the live false positive
    # where every index-linked project was reported missing.)
    for tgt in basename_index.get(proj_name, []):
        try:
            tgt.relative_to(PROJECTS_FOLDER)
            return  # link resolves under the projects root — project exists
        except ValueError:
            continue

    # Guard: if the top-level projects folder does not exist yet (e.g. a
    # fresh scaffold), iterdir() would raise FileNotFoundError. Skip gracefully.
    if not PROJECTS_FOLDER.is_dir():
        return

    # Check existing project folders for a fuzzy match
    existing_projects = [p.name for p in PROJECTS_FOLDER.iterdir() if p.is_dir()]
    close_matches = [ep for ep in existing_projects if _levenshtein_le1(proj_name.lower(), ep.lower())]

    if close_matches:
        match = close_matches[0]
        new_fm = dict(fm)
        new_fm["project"] = f'[[{match}]]'
        tags = list(new_fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        new_fm["tags"] = tags
        _log_fix(rel, "project-ref-fuzzy-corrected", f"{proj_name!r} -> {match!r}")
        if not DRY_RUN:
            try:
                _, body, _ = _parse_frontmatter(
                    path.read_text(encoding="utf-8", errors="replace")
                )
                _write_frontmatter(path, new_fm, body or "")
            except Exception as exc:
                _log_fix(rel, f"project-ref-write-failed: {exc}")
    else:
        # No close match — never materialize a stub folder. A missing project
        # folder is a structural choice that belongs to the user, so propose it
        # to the action queue for filing/janitor resolution.
        _log_queue(
            rule="missing-project-folder",
            path=rel,
            summary=f"project [[{proj_name}]] referenced but no folder at {_rel(proj_folder)}/",
            suggested_options=f"create-folder:{proj_name}/correct-ref/relocate-file",
            cluster_key=f"missing-project:{proj_name}",
        )


# ---------------------------------------------------------------------------
# Pass 12 — Body wikilink resolution (filed files)
# ---------------------------------------------------------------------------

def pass12_body_wikilinks(
    path: Path, body: str, basename_index: dict[str, list[Path]], exceptions: set[str]
) -> None:
    """Emit queue candidates for broken body wikilinks in filed files."""
    if _excepted("body-wikilink-resolution", exceptions):
        return
    # Skip inbox and archive
    try:
        path.relative_to(INBOX_FOLDER)
        return
    except ValueError:
        pass
    try:
        path.relative_to(ARCHIVE_FOLDER)
        return
    except ValueError:
        pass

    rel = _rel(path)
    containing_folder = path.parent.name
    for link in extract_wikilinks(body):
        if not link:
            continue
        if link in basename_index:
            continue
        _log_queue(
            rule="body-wikilink-resolution",
            path=rel,
            summary=f"broken link [[{link}]]",
            suggested_options="link-to-existing/suppress/delete",
            cluster_key=f"body-wikilink:{containing_folder}",
        )


# ---------------------------------------------------------------------------
# Pass 14 — Flag an in-progress idea whose originating session completed
# ---------------------------------------------------------------------------

def pass14_flag_completed_idea(
    path: Path, fm: dict, basename_index: dict[str, list[Path]], exceptions: set[str]
) -> None:
    """Flag — never move — an in-progress idea whose session has completed.

    The idea→session lifecycle (CONFIG § File handling): handoff stamps an idea
    that seeded a session with `status: in-progress` and
    `session: "[[<session>]]"`. When that session is filed `status: complete`,
    the deterministic *preconditions* for retiring the idea are met — but whether
    the session genuinely carried the idea forward is a judgment, not a field
    match, so the script does not archive. It records an `idea-archive` inference
    need naming the session to read; the janitor agent then reads both the idea
    and the session, confirms archiving is appropriate, and only then archives
    (CONFIG: direct auto-archive on completion, agent-judged). Skips the inbox
    and archive; honors an `archive-completed-idea` exception slug.
    """
    if _excepted("archive-completed-idea", exceptions):
        return
    if (fm.get("type") or "") != "idea":
        return
    if str(fm.get("status") or "").strip() != "in-progress":
        return

    # Never flag ideas in the inbox or already in the archive.
    for zone in (INBOX_FOLDER, ARCHIVE_FOLDER):
        try:
            path.relative_to(zone)
            return
        except ValueError:
            pass

    raw = fm.get("session")
    if not isinstance(raw, str):
        return
    m = re.search(r"\[\[([^\[\]]+?)\]\]", raw)
    if not m:
        return
    session_name = normalize_link_target(m.group(1))
    if not session_name:
        return

    # Deterministic precondition: the originating session exists and is complete.
    session_complete = False
    for sp in basename_index.get(session_name, []):
        try:
            s_fm, _, _ = _parse_frontmatter(sp.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if (s_fm and s_fm.get("type") == "session"
                and str(s_fm.get("status") or "").strip() == "complete"):
            session_complete = True
            break
    if not session_complete:
        return

    _log_inference(
        inference_type="idea-archive",
        path=_rel(path),
        observed_value="status: in-progress",
        candidates=f"[[{session_name}]]",
        context_hint=("originating session is complete; read the idea and the session "
                      "and confirm it carried the idea forward before archiving"),
    )


# ---------------------------------------------------------------------------
# Pass 15 — Unprocessed content detection
# ---------------------------------------------------------------------------

def _build_known_dir_names() -> frozenset[str]:
    """Build the set of directory names that are expected in the vault.

    Covers: all top-level FOLDER_ROUTING folder basenames, all SUBFOLDERS
    subfolder names, and `*-<slug>/` containers for container skill slugs.
    Dot-directories are handled separately by `is_excluded_dir`.
    """
    known: set[str] = set()
    for folder_str in FOLDER_ROUTING:
        # Add each path segment (e.g. 'Areas/Journal' adds both parts)
        for part in Path(folder_str).parts:
            known.add(part)
    for subfolder in SUBFOLDERS:
        # Add each path segment, dropping `<placeholder>` parts so e.g.
        # 'Logs/<agent-name>' contributes the literal 'Logs'.
        for part in Path(subfolder).parts:
            if not (part.startswith("<") and part.endswith(">")):
                known.add(part)
    # Operational folders the scaffold creates on demand: the asset staging
    # (<inbox-assets>), the asset homes (CONFIG § Asset folders), and retired
    # legacy surfaces still present in older installs.
    known.update({"00-Actions", "Checkpoints", "00-Assets", _ASSETS_DIR_NAME}
                 | ASSET_HOME_DIRS)
    return frozenset(known)


_KNOWN_DIR_NAMES = _build_known_dir_names()


def _is_known_dir(dir_path: Path) -> bool:
    """Return True if this directory is a recognized vault container.

    A directory is recognized if:
    - it is at the vault root and its name is a top-level FOLDER_ROUTING key, or
    - its name matches a SUBFOLDERS key, or
    - its name matches the pattern `*-<slug>` for a container skill slug, or
    - it is a dot-directory (handled by is_excluded_dir upstream).
    """
    name = dir_path.name
    # Top-level folder names and subfolder names
    if name in _KNOWN_DIR_NAMES:
        return True
    # Skill container pattern: <topic>-<container-slug>
    container_slugs = {slug for slug, row in SKILL_SLUGS.items() if row.inbox_container}
    for slug in container_slugs:
        if name.endswith(f"-{slug}"):
            return True
    # A per-agent log subfolder under the logs root (CONFIG § Subfolders:
    # log → Logs/<agent-name>; legacy installs used 99-Logs).
    if dir_path.parent.name in (_LOGS_DIR_NAME, "99-Logs"):
        return True
    # A direct child of a content root (a FOLDER_ROUTING folder carrying a
    # type-default — projects, areas, reference, snippets) is a typed-content
    # folder (a project, an area, a reference domain), not a stray.
    parent_name = dir_path.parent.name
    for row in FOLDER_ROUTING.values():
        if row.type_defaults and row.folder.split("/")[-1] == parent_name:
            return True
    return False


def _is_frontmatter_exempt(path: Path) -> bool:
    """Return True if this path matches a frontmatter exception from CONFIG."""
    rel = _rel(path)
    for exc in FILE_HANDLING.exceptions:
        pattern = exc.pattern
        # Convert glob-like patterns to a regex for matching
        # Wildcards used: *, **, <wildcard>
        # Replace <wildcard> and folder tokens with folder paths
        pat = pattern
        # Expand known folder wildcards in the pattern
        for wc_token, folder_name in [
            ("<archive>", _folder_by_semantic("archive")),
            ("<inbox>", _folder_by_semantic("inbox")),
        ]:
            pat = pat.replace(wc_token, folder_name)
        # Convert glob to regex
        pat_re = pat.replace("**", "\x00").replace("*", "[^/]*").replace("\x00", ".*")
        pat_re = pat_re.replace(".", r"\.")
        try:
            if re.match(f"^{pat_re}$", rel) or re.match(f"^{pat_re}$", path.name):
                return True
        except re.error:
            pass
        # Also check plain filename match (for patterns like `README.md`)
        if pattern == path.name:
            return True
    return False


def pass15_unprocessed_content(all_md: list[Path], all_non_md: list[Path]) -> None:
    """Detect unprocessed content and emit terse pipe-lines to the detect log.

    Three categories, with the frontmatter-exception and hands-off sets
    subtracted first so protected files never qualify:

    1. Loose non-.md files not already under a placed-asset folder.
    2. Markdown files missing one or more required frontmatter fields
       (`type`, `tags`, `date`), excluding files that are frontmatter-exempt
       per CONFIG § File handling.
    3. Directories at any depth whose names do not match a defined folder,
       subfolder, or skill container — stray folders that suggest unprocessed
       content was dropped without going through the inbox.

    Detections are emitted via `_log_detect` and collected into the
    `## Unprocessed content` section of the run log. The stage-to-inbox step
    (moving detected items to `<inbox-assets>`) is the janitor agent's
    responsibility, not the script's, so this pass only logs.
    """
    inbox_folder_path = INBOX_FOLDER
    assets_folder_name = _ASSETS_DIR_NAME
    required_fields = set(FILE_HANDLING.global_frontmatter)  # typically type, tags, date

    # Category 1: loose non-.md files not under a placed-asset home
    for p in all_non_md:
        if _PLACED_ASSET_DIRS & set(p.parts):
            continue  # already placed (inbox staging or an asset home)
        if _is_hands_off(p):
            continue
        _log_detect("loose-non-md", _rel(p))

    # Category 2: markdown missing required frontmatter
    for p in all_md:
        if _is_frontmatter_exempt(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm, _, parse_ok = pass1_yaml_parse(p, text)
        if not parse_ok or fm is None:
            continue  # parse errors already logged by pass1
        missing = [f for f in required_fields if not fm.get(f)]
        if missing:
            _log_detect(
                "missing-frontmatter",
                _rel(p),
                f"missing: {', '.join(sorted(missing))}",
            )

    # Category 3: stray directories (matching no defined folder / subfolder /
    # skill container).  Walk with os.walk so we can prune excluded dirs.
    for dirpath_str, dirnames, _filenames in os.walk(VAULT_ROOT):
        dirpath = Path(dirpath_str)
        # Prune excluded, hands-off (script-skip — the archive `*`, the outbox
        # `*`), and asset directories from descent — none should be walked or
        # flagged stray.
        dirnames[:] = [
            d for d in dirnames
            if not is_excluded_dir(d)
            and not _is_hands_off(dirpath / d)
            and not is_asset_folder(dirpath / d)
        ]
        for dirname in list(dirnames):
            child = dirpath / dirname
            if is_excluded_dir(dirname) or _is_hands_off(child) or is_asset_folder(child):
                continue
            if _is_known_dir(child):
                continue
            # Stray folder: does not match any known vault container
            _log_detect("stray-folder", _rel(child), f"unrecognized folder name: {dirname!r}")


# ---------------------------------------------------------------------------
# Pass 16 — Deterministic missing-date resolution (--apply gated)
# ---------------------------------------------------------------------------

_DATE_TOKEN_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})[-_ ]?(.+)$")


def _build_archive_date_map() -> dict[str, str]:
    """stem(lower) -> earliest date prefix of a dated archived copy under
    <archive>. Resolution source (a): a dated archived copy of the same
    basename carries the original date in its filename prefix."""
    out: dict[str, str] = {}
    if not ARCHIVE_FOLDER.is_dir():
        return out
    for ap in ARCHIVE_FOLDER.rglob("*.md"):
        if any(is_excluded_dir(part) for part in ap.parts):
            continue
        m = _DATE_TOKEN_RE.match(ap.stem)
        if not m:
            continue
        key, d = m.group(2).lower(), m.group(1)
        if key not in out or d < out[key]:
            out[key] = d
    return out


def _build_history_date_map() -> dict[str, str]:
    """stem(lower) -> earliest known date of an appearance under <history>
    (`.history/`, cold storage). Resolution source (c): a dated copy's prefix,
    else the copy's mtime date."""
    out: dict[str, str] = {}
    history_root = VAULT_ROOT / HISTORY_DIRNAME
    if not history_root.is_dir():
        return out
    for hp in history_root.rglob("*.md"):
        if not hp.is_file():
            continue
        m = _DATE_TOKEN_RE.match(hp.stem)
        if m:
            key, d = m.group(2).lower(), m.group(1)
        else:
            key = hp.stem.lower()
            try:
                d = datetime.fromtimestamp(
                    hp.stat().st_mtime, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            except OSError:
                continue
        if key not in out or d < out[key]:
            out[key] = d
    return out


def _session_date_for(stem: str, inbound_index: dict[str, set[str]]) -> str | None:
    """Resolution source (b): the earliest originating session log that links
    this file — the session's own date (filename prefix, else `date`)."""
    dates: list[str] = []
    for ref_rel in inbound_index.get(stem, set()):
        ref_path = VAULT_ROOT / ref_rel
        try:
            text = ref_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        s_fm, _, err = _parse_frontmatter(text)
        if err or not s_fm or normalize_type(str(s_fm.get("type") or "")) != "session":
            continue
        if _DATE_PREFIX_RE.match(ref_path.stem):
            dates.append(ref_path.stem[:10])
        else:
            d = str(s_fm.get("date") or "")[:10]
            if _DATE_PREFIX_RE.match(d):
                dates.append(d)
    return min(dates) if dates else None


def pass16_missing_dates(
    all_md: list[Path], inbound_index: dict[str, set[str]]
) -> None:
    """Resolve a missing `date` deterministically, in order (CONFIG
    § Helper-script automation row for audit.py):

      (a) a dated archived copy of the same basename under <archive> — its
          date prefix;
      (b) the originating session log that links it — the session's date;
      (c) first appearance under <history> (`.history/`) — date prefix, else
          the copy's mtime date;
      (d) today, as the import date.

    Under --apply the resolved date is stamped along with an `inferred` tag.
    Detect-only mode writes nothing and reports one `date-resolved-pending`
    row per file instead.
    """
    archive_dates = _build_archive_date_map()
    history_dates = _build_history_date_map()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for p in all_md:
        if _is_frontmatter_exempt(p):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm, body, err = _parse_frontmatter(text)
        if err or fm is None:
            continue  # unparseable YAML is pass 1's finding, not a date hole
        if fm.get("date"):
            continue
        if _excepted("date-resolution", _exceptions_for(fm)):
            continue

        rel = _rel(p)
        key = p.stem.lower()
        # Strip an existing date prefix so `2026-01-05-foo.md` keys as `foo`
        # when matched against archived/history copies.
        m = _DATE_TOKEN_RE.match(p.stem)
        bare_key = m.group(2).lower() if m else key

        if bare_key in archive_dates or key in archive_dates:
            resolved = archive_dates.get(key) or archive_dates[bare_key]
            source = "archive-copy"
        else:
            sess = _session_date_for(p.stem, inbound_index)
            if sess:
                resolved, source = sess, "session-log"
            elif key in history_dates or bare_key in history_dates:
                resolved = history_dates.get(key) or history_dates[bare_key]
                source = "history"
            else:
                resolved, source = today, "import-date"

        if DRY_RUN:
            _log_detect("date-resolved-pending", rel, f"{resolved} via {source}")
            continue

        new_fm = dict(fm)
        new_fm["date"] = resolved
        tags = list(new_fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        new_fm["tags"] = tags
        try:
            _write_frontmatter(p, new_fm, body)
            _log_fix(rel, "date-resolved", f"{resolved} via {source}")
        except Exception as exc:
            _log_fix(rel, f"date-resolve-failed: {exc}")


# ---------------------------------------------------------------------------
# Pass 17 — Plan multiplicity (detect-only; NEVER auto-fixes)
# ---------------------------------------------------------------------------

def pass17_plan_multiplicity(all_md: list[Path]) -> None:
    """One canonical plan per scope. Count `type: plan` notes per scope — the
    same resolved `parent` value, or the same top-level inbox container for
    inbox drafts. A scope holding more than one plan, where none names another
    in-scope plan as superseded, emits ONE `duplicate-canonical-plan` row
    (CONFIG § Helper-script automation). Detect-only: this pass never moves,
    edits, or archives a plan.
    """
    scopes: dict[str, list[tuple[Path, dict, str]]] = {}
    inbox_rel = _rel(INBOX_FOLDER)

    for p in all_md:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm, _, err = _parse_frontmatter(text)
        if err or not fm:
            continue
        if normalize_type(str(fm.get("type") or "")) != "plan":
            continue
        if _excepted("plan-multiplicity", _exceptions_for(fm)):
            continue
        rel = _rel(p)
        if rel.startswith(inbox_rel + "/"):
            inner = rel[len(inbox_rel) + 1:].split("/")
            scope = f"{inbox_rel}/{inner[0]}" if len(inner) > 1 else inbox_rel
        else:
            parent_raw = str(fm.get("parent") or "").strip()
            mlink = re.search(r"\[\[([^\[\]]+?)\]\]", parent_raw)
            target = normalize_link_target(mlink.group(1)) if mlink else parent_raw
            scope = f"parent:{target}" if target else f"folder:{_rel(p.parent)}"
        scopes.setdefault(scope, []).append((p, fm, text))

    for scope, plans in scopes.items():
        if len(plans) < 2:
            continue
        stems = {pp.stem for pp, _f, _t in plans}
        superseded = False
        for pp, f, t in plans:
            # A plan that marks ITSELF superseded resolves the scope.
            if "supersed" in str(f.get("status") or "").lower():
                superseded = True
                break
            # A plan that names another in-scope plan as superseded/superseding
            # (frontmatter key or body mention) resolves the scope.
            blob = (
                " ".join(
                    str(f.get(k) or "")
                    for k in ("superseded-by", "superseded_by", "supersedes")
                )
                + " " + t
            ).lower()
            if "supersed" in blob and any(
                o.lower() in blob for o in (stems - {pp.stem})
            ):
                superseded = True
                break
        if superseded:
            continue
        _log_detect(
            "duplicate-canonical-plan",
            scope,
            "; ".join(sorted(_rel(pp) for pp, _f, _t in plans)),
        )


# ---------------------------------------------------------------------------
# Pass 18 — Hook-registration validation (detect-only)
# ---------------------------------------------------------------------------

def pass18_hooks_registration() -> None:
    """Validate hook registrations in `.claude/settings.json` (+ .local).

    Claude Code requires each event's entries to be matcher groups carrying a
    `hooks` list of handlers; a bare `{"type", "command"}` object at group
    level is silently ignored — the hook never fires and nothing reports it.
    A registration can also rot by pointing at a script that no longer exists.
    Both failure modes are invisible at runtime, so this pass is the only
    place they surface. Detect-only: findings route like any other.
    """
    for settings_name in ("settings.json", "settings.local.json"):
        settings_path = VAULT_ROOT / ".claude" / settings_name
        if not settings_path.exists():
            continue
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception as exc:
            _log_detect("hooks-settings-unparseable", _rel(settings_path),
                        f"every hook in this file is dead: {exc}")
            continue
        hooks = data.get("hooks")
        if hooks is None:
            continue
        if not isinstance(hooks, dict):
            _log_detect("dead-hook-registration", _rel(settings_path),
                        "'hooks' is not an object of event -> matcher-group lists")
            continue
        for event, groups in hooks.items():
            if not isinstance(groups, list):
                _log_detect("dead-hook-registration", _rel(settings_path),
                            f"{event}: expected a list of matcher groups")
                continue
            for group in groups:
                if not isinstance(group, dict):
                    _log_detect("dead-hook-registration", _rel(settings_path),
                                f"{event}: non-object matcher group")
                    continue
                if "hooks" not in group:
                    _log_detect(
                        "dead-hook-registration", _rel(settings_path),
                        f"{event}: entry lacks a 'hooks' list — a bare command "
                        "object here is silently ignored; wrap it in "
                        "{'matcher': ..., 'hooks': [...]}",
                    )
                    continue
                handlers = group.get("hooks")
                if not isinstance(handlers, list) or not handlers:
                    _log_detect("dead-hook-registration", _rel(settings_path),
                                f"{event}: 'hooks' is not a non-empty list")
                    continue
                for handler in handlers:
                    if (not isinstance(handler, dict)
                            or handler.get("type") != "command"
                            or not str(handler.get("command") or "").strip()):
                        _log_detect("dead-hook-registration", _rel(settings_path),
                                    f"{event}: handler missing type/command")
                        continue
                    for script in re.findall(r"\./\.claude/[^\s\"']+",
                                             str(handler["command"])):
                        if not (VAULT_ROOT / script.removeprefix("./")).exists():
                            _log_detect("missing-hook-script", _rel(settings_path),
                                        f"{event}: command references {script} "
                                        "which does not exist")


# ---------------------------------------------------------------------------
# Pass 13 — Drift detection
# ---------------------------------------------------------------------------

def pass13_drift_detection() -> None:
    """Compare types in vault-state-index to normalize_type to find drift."""
    state_index = VAULT_ROOT / LOGS_REL / "Vault-State-Index.md"
    if not state_index.exists():
        return

    try:
        text = state_index.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return

    m = re.search(r"^## Types in use\s*$", text, re.MULTILINE)
    if not m:
        return

    after = text[m.end():]
    next_h2 = re.search(r"^## ", after, re.MULTILINE)
    section = after[: next_h2.start()] if next_h2 else after

    used_types: list[str] = re.findall(r"`([^`]+)`", section)
    for t in used_types:
        if t not in CANONICAL_TYPE_KEYS and normalize_type(t) is None:
            _log_inference("consolidation", str(state_index.name),
                           observed_value=t,
                           context_hint="type in state-index not resolvable via normalize_type")

    # Check for canonical keys with zero occurrences
    for canonical_key in sorted(CANONICAL_TYPE_KEYS):
        if canonical_key not in used_types:
            _log_inference("type-unused", str(state_index.name),
                           observed_value=canonical_key,
                           context_hint="canonical type with zero files in vault-state-index")


# ---------------------------------------------------------------------------
# build_state_index invocation
# ---------------------------------------------------------------------------

def _find_build_state_index() -> Path | None:
    """Locate build_state_index, preferring the newest draft beside this file,
    then the installed scripts dir."""
    for cand in (
        _VAULT_JANITOR_DIR / "build_state_index.py",
        _KIT_ROOT / "scripts" / "build_state_index.py",
        _VAULT_JANITOR_DIR / "build_state_index.py",
        _KIT_ROOT / "scripts" / "build_state_index.py",
    ):
        if cand.exists():
            return cand
    return None


def _run_build_state_index(findings: list[tuple[str, str, str]] | None = None) -> None:
    """Refresh the single shared state snapshot via build_state_index.

    Events live in the append-only per-agent ledger, not in the snapshot. When
    `findings` is given (the end-of-run call), they are written to a transient
    `code | target | value` file and passed through --findings so they merge into
    the snapshot's ## Open findings section — the snapshot is the one work list,
    and no second snapshot file is written.
    """
    build_script = _find_build_state_index()
    if build_script is None:
        return
    cmd = [sys.executable, str(build_script)]
    tmp_findings: Path | None = None
    if findings:
        fd, tmp_name = tempfile.mkstemp(prefix="janitor-findings-", suffix=".txt")
        tmp_findings = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for code, target, value in findings:
                fh.write(f"{code} | {target} | {value or '-'}\n")
        cmd += ["--findings", str(tmp_findings)]
    try:
        subprocess.run(
            cmd,
            env={**os.environ, "JANITOR_VAULT_ROOT": str(VAULT_ROOT)},
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        _log_fix("(build-state-index)", f"build_state_index exited {exc.returncode}")
    except Exception as exc:
        _log_fix("(build-state-index)", f"build_state_index error: {exc}")
    finally:
        if tmp_findings is not None:
            try:
                tmp_findings.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Write run log
# ---------------------------------------------------------------------------

# A finding carries one of these markers in its action when it is pending or
# could not be applied; a completed mutation carries none. A dry run completes
# nothing, so every dry-run record is a finding.
_FINDING_MARKERS = ("would", "collision", "failed", "mismatch", "error")


def _clean_code(action: str) -> str:
    """Reduce an action to its terse code: drop the dry-run prefix and any
    trailing free-text detail after a colon."""
    return action.replace("[dry-run]", "").strip().split(":", 1)[0].strip()


def _is_event(rec: dict) -> bool:
    """True for a completed mutation this run. Dry-run records and
    would/collision/failure records are findings (state), not events."""
    if rec.get("dry"):
        return False
    return not any(m in rec.get("action", "") for m in _FINDING_MARKERS)


def _scope_of(target: str) -> str:
    """Top-level folder of a vault-relative path, for the count rollup."""
    t = target.strip()
    return t.split("/", 1)[0] if "/" in t else (t or "-")


def _clean_value(v: str) -> str:
    """Reduce a value to a bare token (CONFIG § Log files: no field is prose).
    Strips the dry-run marker, a transition arrow (keeping the destination), a
    leading `label:` prefix, and surrounding repr quotes."""
    v = v.replace("[dry-run]", " ").strip()
    if " -> " in v:
        v = v.rsplit(" -> ", 1)[-1]
    elif v.startswith("->"):
        v = v[2:]
    v = v.strip()
    m = re.match(r"^[a-z][a-z -]*:\s+(.+)$", v)
    if m:
        v = m.group(1).strip()
    return v.strip().strip("'\"")


# The run-scoped findings this pass produced, written to a transient file and
# handed to build_state_index via --findings so they land in the one shared
# snapshot. Populated by _write_logs, consumed by _run_build_state_index.
_RUN_FINDINGS: list[tuple[str, str, str]] = []   # (code, target, value)


def _write_logs() -> None:
    """Append completed actions to the event ledger and collect open findings.

    Two artifacts only (CONFIG § Log files); this script owns just the first:
    - Event ledger `<logs>/janitor-agent/janitor-agent.md`, append-only: one
      `timestamp | actor | code | target | value` line per completed action. A
      dry run completes nothing, so nothing is appended.
    - State snapshot `<logs>/Vault-State-Index.md` is OWNED by build_state_index.
      This pass does NOT write it; instead it stages its open findings into the
      module-level `_RUN_FINDINGS`, which `_run_build_state_index` passes through
      --findings so they merge into the snapshot's ## Open findings section. No
      second snapshot file is written.
    """
    write_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")

    def _line(code: str, target: str, value: str = "") -> str:
        return f"{write_ts} | {AGENT_NAME} | {code} | {target} | {_clean_value(value) or '-'}"

    # Split recorded fixes into completed events and pending findings.
    event_lines: list[str] = []
    findings: list[tuple[str, str, str]] = []   # (code, target, value)
    for rec in _auto_fixes_log:
        code = _clean_code(rec["action"])
        if _is_event(rec):
            event_lines.append(_line(code, rec["path"], rec.get("detail", "")))
        else:
            findings.append((code, rec["path"], _clean_value(rec.get("detail", ""))))

    # Deferred judgment calls and detections are findings too.
    for r in _inference_rows:
        findings.append((r["inference_type"], r["path"],
                         (r.get("candidates") or r.get("observed_value") or "").strip()))
    for r in _queue_candidate_rows:
        findings.append((r["rule"], r["path"], r.get("cluster_key", "")))
    for r in _detect_log:
        findings.append((r["action"], r["path"], r.get("detail", "")))

    # Event ledger — append only when something actually happened. A detect-
    # only run completes nothing and therefore writes nothing, the log
    # directory included.
    if event_lines and not DRY_RUN:
        log_dir.mkdir(parents=True, exist_ok=True)
        ledger_path = log_dir / f"{AGENT_NAME}.md"
        with ledger_path.open("a", encoding="utf-8") as f:
            f.write("\n".join(event_lines) + "\n")

    # Stage findings for the shared snapshot (build_state_index --findings).
    _RUN_FINDINGS.clear()
    _RUN_FINDINGS.extend(findings)


# ---------------------------------------------------------------------------
# Main pass orchestrator
# ---------------------------------------------------------------------------

def run_audit() -> None:
    # Build a fresh state-index snapshot at the START of the run (per the
    # janitor SKILL). Pass 13 drift-detection and the analyst both read it.
    # GATED behind --apply: a detect-only run writes NOTHING — no mtime
    # touches, no ledger, and no state-snapshot refresh (the snapshot write is
    # a filesystem write like any other). Pass 13 then reads whatever snapshot
    # already exists, or skips when there is none.
    if not DRY_RUN:
        print("Building state index (pre-pass)...", file=sys.stderr)
        _run_build_state_index()
    else:
        print("Detect-only: skipping state-index refresh (no writes).",
              file=sys.stderr)

    print("Walking vault...", file=sys.stderr)
    all_md = _walk_vault()
    all_non_md = _walk_non_md()
    print(f"Found {len(all_md)} .md files, {len(all_non_md)} non-md files.", file=sys.stderr)

    # Build baseline indices before any mutations
    basename_index, inbound_index = _build_indices(all_md)

    print("Running per-file passes 1-12...", file=sys.stderr)

    for original_path in list(all_md):
        path = original_path

        # Read file
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            _log_inference("read-error", _rel(path), context_hint=str(exc))
            continue

        # Pass 1 — YAML parse
        fm, body, parse_ok = pass1_yaml_parse(path, text)
        if not parse_ok:
            continue
        if fm is None:
            fm = {}

        # Per-file compliance-check opt-outs
        exceptions = _exceptions_for(fm)

        changed = False

        # Pass 2 — Type normalization
        if not _excepted("type-normalize", exceptions):
            fm, ch = pass2_type_normalize(path, fm)
            changed = changed or ch

        # Pass 3 — Tag normalization
        if not _excepted("tag-normalize", exceptions):
            fm, ch = pass3_tag_normalize(path, fm, exceptions)
            changed = changed or ch

        # Pass 4 — Type inference (if still no type)
        if not _excepted("type-inference", exceptions):
            fm, ch = pass4_type_inference(path, fm, body)
            changed = changed or ch

        # Write frontmatter if changed so far
        if changed and not DRY_RUN:
            _write_frontmatter(path, fm, body)
            changed = False

        # Pass 5 — Type-folder match (may move file)
        if not _excepted("type-folder-match", exceptions):
            path, fm, moved = pass5_type_folder_match(path, fm, basename_index)
            if moved:
                # Rebuild indices after move
                basename_index, inbound_index = _build_indices(_walk_vault())
                changed = False

        # Pass 6 — Naming normalization (may rename file)
        if not _excepted("naming", exceptions):
            path, renamed = pass6_naming(path, fm)
            if renamed:
                basename_index, inbound_index = _build_indices(_walk_vault())

        # Pass 7 — Parent inference
        if not _excepted("parent-inference", exceptions):
            fm, body, ch = pass7_parent_inference(path, fm, body, basename_index)
            if ch and not DRY_RUN:
                _write_frontmatter(path, fm, body)

        # Pass 8 — Index child registration
        if not _excepted("index-children", exceptions):
            pass8_index_children(path, fm, body, basename_index)

        # Pass 9 — Orphan detection
        if not _excepted("orphan-detection", exceptions):
            pass9_orphan_detection(path, fm, body, basename_index, inbound_index)

        # Pass 12 — Body wikilink resolution
        pass12_body_wikilinks(path, body, basename_index, exceptions)

        # Pass 14 — Flag an in-progress idea whose session has completed (agent archives)
        pass14_flag_completed_idea(path, fm, basename_index, exceptions)

    # Pass 10 — Loose asset relocation (fixed snapshot lists, never a live walk)
    print("Pass 10: loose asset relocation...", file=sys.stderr)
    pass10_loose_assets(all_md, all_non_md)

    # Pass 11 — Missing project folder. Re-walk to pick up any moves above;
    # _walk_vault returns a materialized list, never a live iterator.
    print("Pass 11: missing project folders...", file=sys.stderr)
    for p in _walk_vault():
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        fm, _, parse_ok = pass1_yaml_parse(p, text)
        if not parse_ok or not fm:
            continue
        if _excepted("project-folder", _exceptions_for(fm)):
            continue
        pass11_project_folder(p, fm, basename_index)

    # Pass 13 — Drift detection
    print("Pass 13: drift detection...", file=sys.stderr)
    pass13_drift_detection()

    # Pass 15 — Unprocessed content detection
    print("Pass 15: unprocessed content detection...", file=sys.stderr)
    pass15_unprocessed_content(all_md, all_non_md)

    # Pass 16 — Deterministic missing-date resolution (--apply gated; detect
    # mode reports `date-resolved-pending` rows instead of writing).
    print("Pass 16: missing-date resolution...", file=sys.stderr)
    pass16_missing_dates(all_md, inbound_index)

    # Pass 17 — Plan multiplicity (detect-only; never auto-fixes).
    print("Pass 17: plan multiplicity...", file=sys.stderr)
    pass17_plan_multiplicity(all_md)

    # Pass 18 — Hook-registration validation (detect-only; a dead hook is
    # silent everywhere else).
    print("Pass 18: hook-registration validation...", file=sys.stderr)
    pass18_hooks_registration()

    # Append events to the ledger and stage this run's findings, then refresh
    # the single shared snapshot with those findings folded into ## Open findings.
    print("Writing event ledger + staging findings...", file=sys.stderr)
    _write_logs()

    if not DRY_RUN:
        print("Refreshing shared state snapshot (with findings)...", file=sys.stderr)
        _run_build_state_index(_RUN_FINDINGS)
        print(f"Ledger under: {log_dir}; snapshot at <logs>/Vault-State-Index.md",
              file=sys.stderr)
    else:
        # Detect-only writes NOTHING — print the findings to stdout instead so
        # the caller (the janitor agent or a person) still gets the work list.
        print("Detect-only: snapshot not refreshed; findings follow on stdout.",
              file=sys.stderr)
        for code, target, value in _RUN_FINDINGS:
            print(f"{code} | {target} | {value or '-'}")
    print(
        f"Done - {len(_auto_fixes_log)} recorded actions/findings, "
        f"{len(_inference_rows)} inference items, "
        f"{len(_queue_candidate_rows)} queue candidates, "
        f"{len(_detect_log)} detections.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    run_audit()
