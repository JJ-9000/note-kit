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
  - --apply refuses to run against a root that lacks the kit's top-level
    folders (a snapshot/kit container, not a real vault). A root hand-renamed
    off its CONFIG literal is reverted first (`folder-reverted`), so the refusal
    reads a genuinely incomplete vault rather than a rename; an ambiguous shape
    reverts nothing and reports instead.
  - Vault-root loose files (the user's draft space) are excluded from every pass.
  - is_asset_folder gates every decision as the first check; nothing inside an
    asset folder is typed, parented, renamed, or normalized.
  - Mutating passes run over fixed file-list snapshots, never live iterators.

Environment:
    JANITOR_VAULT_ROOT — absolute path to vault root (required)
    JANITOR_APPLY      — enable writes when set to an allowlisted value (`1`/
                         `true`); any other value stays detect-only and logs the
                         refused value. Equivalent to passing --apply.
    JANITOR_LEASE_PATH — override the run-lease file location (default
                         `<vault>/<logs>/run-lease.md`); the scratch-lease seam
                         for tests.

CLI flags:
    --apply     perform filesystem writes (also via JANITOR_APPLY=1/true). Takes
                the vault-wide run lease for the duration of the pass.
    --dry-run   force detect-only; no filesystem writes. Always overrides --apply.
    --skip <class>[,<class>…]
                withhold these fix classes in --apply mode; their fixes become
                detect-only findings for the run (logged as would-…, exactly as a
                dry run logs them) while every other class applies. Repeatable.
    --only <class>[,<class>…]
                the inverse: apply ONLY these classes, withhold the rest.
                Mutually exclusive with --skip.
    A class is the prefix its fix and finding codes share (`naming`, `orphan`,
    `parent`, `index-child`, …); `FIX_CLASSES` lists every one, an unknown name
    is refused before the pass runs, and a detect-only run ignores both flags
    with a warning. The active selector prints in the run header and lands in the
    run-start ledger line's value.
"""
from __future__ import annotations

import sys
import os
import re
import ast
import json
import hashlib
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
    HISTORY_DIRNAME, token_path, levenshtein_le1,
    _CONFIG as _CONFIG_PATH,
)
from wikilink_helpers import (
    normalize_link_target, extract_wikilinks, rewrite_wikilink_interior,
    WIKILINK_RE,
)
from normalize_type import normalize_type
from normalize_tag import normalize_tag
from rename_with_link_integrity import (
    rename_with_links, _find_inbound,
)
from index_helpers import add_child_link_to_index
# Shared write-safety substrate (Kit-Code-Quality-Plan Lane H). Reads route
# through the strict-UTF-8 decode probe; frontmatter writes are targeted
# raw-block edits (archive-first, reparse-verify, restore-on-failure).
from frontmatter_helpers import (
    read_text_or_none, split_frontmatter, structured_rewrite, archive_dest_for,
    write_text, StructuralInvariantError, OutsideVaultError,
)
from run_lease import RunLease, LeaseHeldError
# The shared archive-first step (CONFIG § Helper-script automation). Every
# pre-image this script writes outside the frontmatter substrate goes through
# it: probe a free archive name, copy, hash-verify, raise on failure so the
# caller aborts before it mutates.
from archive_first import ArchiveFirstError, archive_preimage
# One implementation of the hands-off predicate, the scope rollup, and the
# per-file-loop helpers — imported from build_state_index so the janitor and the
# snapshot builder gate every path through the SAME rules (no drift).
from build_state_index import (
    is_hands_off as _shared_is_hands_off,
    in_asset_folder as _shared_in_asset_folder,
    build_hands_off_patterns,
    is_folder_cover,
)
import subfolder_housekeeping

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
_selector = parser.add_mutually_exclusive_group()
_selector.add_argument("--skip", action="append", default=[], metavar="CODE[,CODE...]",
                       help="Withhold these fix classes in --apply mode; their "
                            "fixes downgrade to detect-only findings for the run. "
                            "Repeatable, and comma-separated lists are accepted.")
_selector.add_argument("--only", action="append", default=[], metavar="CODE[,CODE...]",
                       help="Apply ONLY these fix classes in --apply mode; every "
                            "other class downgrades to detect-only for the run. "
                            "Repeatable, and comma-separated lists are accepted.")
args = parser.parse_args()

# Writes are gated behind an explicit opt-in: a bare `python audit.py` performs
# NO filesystem writes. Apply mode requires --apply or an ALLOWLISTED
# JANITOR_APPLY value (`1`/`true`); any other value — `no`, `off`, a typo —
# stays detect-only and the refused value is logged. Failing toward detect-only
# (not toward mutation) is the safe default against the documented `=1` contract.
# --dry-run always wins and forces detect-only.
_JANITOR_APPLY_RAW = os.environ.get("JANITOR_APPLY", "")
_JANITOR_APPLY_NORM = _JANITOR_APPLY_RAW.strip().lower()
_env_apply = _JANITOR_APPLY_NORM in ("1", "true")
if _JANITOR_APPLY_NORM not in ("", "1", "true"):
    print(
        f"JANITOR_APPLY={_JANITOR_APPLY_RAW!r} is not an allowlisted enable value "
        "(1/true) — staying detect-only.",
        file=sys.stderr,
    )
_apply = args.apply or _env_apply
DRY_RUN: bool = args.dry_run or not _apply

if DRY_RUN:
    print("DRY-RUN mode - no file writes will occur. "
          "Pass --apply or set JANITOR_APPLY=1 to enable writes.", file=sys.stderr)

# ---------------------------------------------------------------------------
# Per-class apply selector — --skip / --only
# ---------------------------------------------------------------------------
#
# Every fix this script performs belongs to one CLASS, named by the prefix its
# finding and fix codes share (`naming-renamed` / `naming-would-rename` are the
# `naming` class). `--skip naming` withholds that class's fixes for the run:
# everything else applies, and each withheld fix is recorded as a would-… finding
# exactly as a dry run records it, so the work stays on the list. `--only naming`
# inverts it. The two flags are mutually exclusive (argparse enforces it).
#
# The class list is the authority for what a selector may name: a typo in
# `--skip` would otherwise silently withhold nothing while the operator believed
# a class was held back, so an unknown name is refused before any pass runs.
FIX_CLASSES: tuple[str, ...] = (
    "body-wikilink",   # pass 12 — space->hyphen link normalization
    "date",            # pass 16 — deterministic missing-date resolution
    "folder",          # root-folder revert — a hand-renamed kit root put back
    "index-child",     # pass  8 — index child registration
    "loose-asset",     # pass 10 — loose asset relocation
    "naming",          # pass  6 — filename normalization (rename)
    "orphan",          # pass  9 — orphan relocation to the inbox
    "parent",          # pass  7 — parent inference and stamping
    "project-ref",     # pass 11 — fuzzy project-reference correction
    "subfolder",       # subfolder housekeeping — empty subfolders/indexes
    "tag",             # pass  3 — tag normalization
    "type",            # passes 2 and 4 — type normalization and inference
    "type-folder",     # pass  5 — type-folder relocation
)


def _split_classes(values: list[str]) -> list[str]:
    """Flatten repeated flags and comma-separated lists into one ordered list."""
    out: list[str] = []
    for chunk in values:
        for part in chunk.split(","):
            part = part.strip()
            if part and part not in out:
                out.append(part)
    return out


_SKIP_CLASSES: frozenset[str] = frozenset(_split_classes(args.skip))
_ONLY_CLASSES: frozenset[str] = frozenset(_split_classes(args.only))

_unknown = sorted((_SKIP_CLASSES | _ONLY_CLASSES) - set(FIX_CLASSES))
if _unknown:
    sys.exit(
        f"unknown fix class(es) {_unknown}. Known classes: "
        f"{', '.join(FIX_CLASSES)}."
    )

# A detect-only run applies nothing, so a selector has nothing to select. Say so
# rather than letting the flag read as if it changed the run.
if DRY_RUN and (_SKIP_CLASSES or _ONLY_CLASSES):
    print(
        "--skip/--only apply only to an --apply run; this run is detect-only, "
        "so every class is already reported without being fixed. Selector ignored.",
        file=sys.stderr,
    )
    _SKIP_CLASSES = frozenset()
    _ONLY_CLASSES = frozenset()


def _selector_label() -> str:
    """The active selector as one token for the run header and the ledger."""
    if _ONLY_CLASSES:
        return f"--only {','.join(sorted(_ONLY_CLASSES))}"
    if _SKIP_CLASSES:
        return f"--skip {','.join(sorted(_SKIP_CLASSES))}"
    return "all-classes"


def _selector_allows(fix_class: str) -> bool:
    """True when the run's selector lets `fix_class` apply its fixes."""
    if _ONLY_CLASSES:
        return fix_class in _ONLY_CLASSES
    return fix_class not in _SKIP_CLASSES


def _apply_fixes(fix_class: str) -> bool:
    """True when this run PERFORMS `fix_class`'s fixes — apply mode, class
    allowed by the selector. Every mutating branch gates on this instead of on
    `not DRY_RUN`."""
    return not DRY_RUN and _selector_allows(fix_class)


def _withheld_by_selector(fix_class: str) -> bool:
    """True when apply mode is on but the selector holds this class back."""
    return not DRY_RUN and not _selector_allows(fix_class)


def _withhold_marker(fix_class: str) -> str:
    """The marker a withheld fix carries in its logged value: the dry-run marker
    on a detect-only run, the selector's own marker when a class is held back."""
    return "[dry-run]" if DRY_RUN else f"[skipped: {fix_class}]"


if not DRY_RUN and (_SKIP_CLASSES or _ONLY_CLASSES):
    print(f"Apply selector: {_selector_label()} — withheld classes report "
          "would-… findings instead of applying.", file=sys.stderr)

# ---------------------------------------------------------------------------
# Kit-as-vault guard — --apply refuses to mutate anything that is not a real
# vault. A real vault carries every top-level root CONFIG § Folders declares
# (the seven roots). A folder missing them is a snapshot, a kit container, or a
# review copy — pointing --apply at one has corrupted staged files before.
# ---------------------------------------------------------------------------

_REQUIRED_ROOTS = [f for f in FOLDER_ROUTING if "*" not in f]

# ---------------------------------------------------------------------------
# Root-folder revert (CONFIG § Folders) — a kit root hand-renamed off its
# `literal` is put back and the attempt logged (`folder-reverted`), so the
# analyst can propose adopting a rename that recurs instead of fighting it.
# ---------------------------------------------------------------------------
#
# This runs BEFORE the kit-as-vault guard below, because a renamed root is the
# exact state that guard refuses on: revert first, and the guard then sees a
# whole vault. The revert is the ONE unambiguous case — exactly one canonical
# literal missing and exactly one unknown top-level directory present, which is
# a one-for-one rename and nothing else. Every other shape is reported and left
# on disk: moving a whole root on a guess is the expensive mistake here, and
# the operator (or the analyst) resolves the ambiguity by hand.
#
# The records stage here and replay through the normal log helpers at the top
# of the pass (`_replay_root_folder_records`), so a completed revert reaches the
# event ledger after `run-start` and every detection reaches the shared snapshot
# as a finding.

# (code, target, detail, detect_only) — replayed once the log helpers exist.
_ROOT_FOLDER_RECORDS: list[tuple[str, str, str, bool]] = []


def _stage_root_record(code: str, target: str, detail: str,
                       detect_only: bool) -> None:
    """Stage one root-folder record for replay, and echo it to stderr now.

    The echo is what keeps an ambiguous shape legible: two missing roots trip
    the kit-as-vault guard below, which exits before any pass runs, so stderr is
    the only place that record can still be read. A vault whose roots all sit at
    their literals stages nothing and prints nothing.
    """
    _ROOT_FOLDER_RECORDS.append((code, target, detail, detect_only))
    print(f"[root-folder] {code} | {target} | {detail}", file=sys.stderr)

# The single-segment roots of CONFIG § Folders; a nested routing path is not a
# top-level root and takes no part in this check.
_ROOT_LITERALS: list[str] = [
    f for f in _REQUIRED_ROOTS if "/" not in f and "\\" not in f
]


def _unknown_root_dirs() -> list[str]:
    """Top-level directory names that are none of CONFIG's roots.

    A dot-directory is skipped (§ Scan exclusions) and an asset-classified
    folder is skipped too (§ Asset folders: an opaque, hands-off unit that was
    dropped at the root, never a renamed kit root).
    """
    known = set(_ROOT_LITERALS)
    found: list[str] = []
    try:
        children = sorted(p for p in VAULT_ROOT.iterdir() if p.is_dir())
    except OSError:
        return found
    for child in children:
        name = child.name
        if name in known or name.startswith(".") or is_excluded_dir(name):
            continue
        try:
            if is_asset_folder(child):
                continue
        except OSError:
            continue
        found.append(name)
    return found


def _check_root_folders() -> None:
    """Detect a hand-renamed kit root, and in apply mode put it back."""
    missing = [f for f in _ROOT_LITERALS if not (VAULT_ROOT / f).is_dir()]
    unknown = _unknown_root_dirs()

    if not missing and not unknown:
        return                      # every root at its literal — nothing to say

    if len(missing) == 1 and len(unknown) == 1:
        literal, found = missing[0], unknown[0]
        if not _apply_fixes("folder"):
            # Detect-only, or the class held back by the selector: report the
            # revert this run would have performed and touch nothing.
            _stage_root_record(
                "folder-would-revert", found,
                f"{_withhold_marker('folder')} {found} -> {literal}", False,
            )
            return
        try:
            os.rename(VAULT_ROOT / found, VAULT_ROOT / literal)
        except OSError as exc:
            # A rename that could not happen is a finding, never an event: the
            # run continues and the guard below decides whether it may mutate.
            _stage_root_record(
                "folder-revert-failed", found, f"{found} -> {literal}: {exc}", False,
            )
            return
        _stage_root_record(
            "folder-reverted", found, f"{found} -> {literal}", False,
        )
        return

    if missing:
        # Two roots missing, two unknown directories, or a missing root with no
        # candidate: which name belongs to which root is a guess, so the pass
        # names what it saw and leaves the disk alone.
        _stage_root_record(
            "folder-rename-ambiguous", ".",
            f"missing root(s) {', '.join(missing) or 'none'}; unknown top-level "
            f"dir(s) {', '.join(unknown) or 'none'} — reverted nothing (a rename "
            "is unambiguous only one-for-one)", True,
        )
        return

    # Every root present, plus something else at the root: an extra directory,
    # not a rename.
    for name in unknown:
        _stage_root_record(
            "stray-root-folder", name,
            "unrecognized top-level directory with every CONFIG root present",
            True,
        )


_check_root_folders()


def _replay_root_folder_records() -> None:
    """Feed the staged root-folder records through the run's log helpers.

    Called at the top of the pass, past `run-start`, so a completed revert
    appends to the event ledger in run order and every detection lands in the
    run's findings.
    """
    for code, target, detail, detect_only in _ROOT_FOLDER_RECORDS:
        if detect_only:
            _log_detect(code, target, detail)
        else:
            _log_fix(target, code, detail)


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
    rec = {
        "action": action,
        "path": rel_path,
        "detail": detail,
        "dry": DRY_RUN,
    }
    _auto_fixes_log.append(rec)
    # Per-action flush: a COMPLETED mutation reaches the append-only ledger the
    # moment it happens, not at exit. The whole list used to buffer until
    # _write_logs, so a pass that died mid-run wrote a `run-start` line and
    # nothing else — the 2026-07-26 19:22Z run renamed files and left zero
    # rename records, which is what made the loss untraceable. A finding
    # (dry-run/would/collision/failed) still waits for _write_logs, because its
    # home is the snapshot, not the ledger. `flushed` marks the record so
    # _write_logs does not append it twice.
    if not DRY_RUN and _is_event(rec):
        try:
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
            _append_ledger([
                f"{ts} | {AGENT_NAME} | {_clean_code(action)} | {rel_path} | "
                f"{_clean_value(detail) or '-'}"
            ])
            rec["flushed"] = True
        except OSError as exc:
            # A ledger append that fails leaves the record buffered for
            # _write_logs; surface it rather than losing the line silently.
            print(f"[ledger] per-action flush failed for {rel_path}: {exc}",
                  file=sys.stderr)


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
# Hands-off filtering
# ---------------------------------------------------------------------------

# Hands-off predicate and the asset-folder gate are the ONE shared
# implementation (imported from build_state_index), bound here to this run's
# vault root so the janitor and the snapshot builder gate every path through the
# same rules. Patterns are compiled once from FOLDER_ROUTING (§ hands-off).
_HANDS_OFF_PATTERNS = build_hands_off_patterns()


def _in_asset_folder(path: Path) -> bool:
    """Asset-folder gate for this vault root (CONFIG § Asset folders)."""
    return _shared_in_asset_folder(path, VAULT_ROOT)


def _is_hands_off(path: Path) -> bool:
    """Hands-off predicate for this vault root — asset-gate-first, dot-dir and
    __pycache__/*.pyc exclusion, vault-root loose-file exemption, and every
    folder's compiled hands_off_patterns (CONFIG § Folders, § Asset folders)."""
    return _shared_is_hands_off(path, VAULT_ROOT, _HANDS_OFF_PATTERNS)


def _is_preserved_copy(path: Path) -> bool:
    """True for a path under `<archive>` or `<history>` — a preserved copy of a
    note, never a live one. Every scope-grouping and shape lint reads live notes
    only: an archived plan is a record of a decision already made, so counting
    it into its old scope invents a multiplicity that does not exist, and
    linting its shape reports work that is finished."""
    try:
        path.relative_to(ARCHIVE_FOLDER)
        return True
    except ValueError:
        pass
    return HISTORY_DIRNAME in path.parts


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
# Strict-UTF-8 read funnel — every read routes through the substrate's
# strict-decode probe: a non-UTF-8 (e.g. cp1252) file is skipped and reported
# ONCE, never scanned with a lossy U+FFFD decode or rewritten over. A vanished
# path (moved/renamed by a mid-run pass) returns None silently, so the per-file
# loop stops emitting spurious read-error rows for files another pass relocated.
# ---------------------------------------------------------------------------

_NON_UTF8_REPORTED: set[str] = set()


def _read_text(path: Path) -> str | None:
    """Strict-UTF-8 text of `path`, or None to skip.

    None means one of: the file vanished (moved mid-run — skip silently), or its
    bytes are not valid UTF-8 (skip and report once as `non-utf8-skipped`), or a
    genuine OS read error (reported as `read-error`).

    `read_text_or_none` returns None for every one of those, so the three cases
    are told apart HERE, after the fact: a path that no longer resolves to a file
    vanished, one whose bytes still refuse to read is a read error, and what
    remains is the non-UTF-8 case. The existence check before the read stays as
    the cheap common path; the re-probe covers the file that disappears inside
    the gap between that check and the read, which is a real gap in a vault an
    external sync actor writes to."""
    p = Path(path)
    if not p.exists():
        return None  # vanished (relocated by a prior pass) — not a read error
    text = read_text_or_none(p)  # substrate strict-decode probe
    if text is None:
        if not p.is_file():
            return None  # vanished between the check and the read — skip silently
        try:
            p.read_bytes()
        except OSError as exc:
            _log_inference("read-error", _rel(p), context_hint=str(exc))
            return None
        rel = _rel(p)
        if rel not in _NON_UTF8_REPORTED:
            _NON_UTF8_REPORTED.add(rel)
            _log_detect("non-utf8-skipped", rel,
                        "not valid UTF-8; skipped (never rewritten)")
    return text


# ---------------------------------------------------------------------------
# Frontmatter write helper — targeted raw-block edit (CONFIG § Versioning)
# ---------------------------------------------------------------------------

_FM_KEY_RE = re.compile(r"^([A-Za-z_][\w-]*)\s*:")


def _fm_value_equal(a, b) -> bool:
    """True when a parsed on-disk value equals a new value, so its line stays
    byte-identical (no rewrite). Lists compare element-wise as strings; bools by
    truth; everything else by string form (a YAML-parsed date equals its
    `YYYY-MM-DD` string)."""
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        return [str(x) for x in (a or [])] == [str(x) for x in (b or [])]
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b)
    return str(a) == str(b)


def _render_fm_scalar(value) -> str:
    """Render a scalar frontmatter value. Wikilink strings and anything YAML
    would misread are double-quoted; plain words and dates stay bare."""
    if isinstance(value, bool):
        return "true" if value else "false"
    s = str(value)
    if s == "":
        return '""'
    if (s[0] in "[{\"'!&*#|>%@`" or "[[" in s or ": " in s
            or s != s.strip() or s.endswith(":")):
        esc = s.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{esc}"'
    return s


def _render_fm_entry(key, value) -> str:
    """Render one frontmatter entry (no trailing newline). A list renders in the
    vault's block style; a scalar on one line."""
    if isinstance(value, (list, tuple)):
        items = list(value)
        if not items:
            return f"{key}: []"
        return "\n".join([f"{key}:"] + [f"  - {_render_fm_scalar(it)}" for it in items])
    return f"{key}: {_render_fm_scalar(value)}"


def _targeted_fm_rewrite(inner: str, changed: dict) -> str:
    """Rewrite ONLY the entries named in `changed` inside a raw frontmatter block,
    leaving every other line byte-identical (the bifurcate_plan._set_fm_field
    pattern, generalized to whole logical entries). An existing key's `key:` line
    plus its indented / block-list continuation lines are replaced as a unit; a
    new key is appended. Key order, comments, and quoting of untouched keys are
    preserved."""
    if not changed:
        return inner
    lines = inner.splitlines(keepends=True)
    term = "\r\n" if any(ln.endswith("\r\n") for ln in lines) else "\n"
    out: list[str] = []
    seen: set[str] = set()
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        stripped = line.rstrip("\r\n")
        m = _FM_KEY_RE.match(stripped)
        is_top_key = (
            bool(m) and not stripped[:1].isspace()
            and not stripped.lstrip().startswith("- ")
        )
        if is_top_key and m.group(1) in changed:
            key = m.group(1)
            seen.add(key)
            out.append(_render_fm_entry(key, changed[key]).replace("\n", term) + term)
            i += 1
            while i < n:  # consume this key's continuation lines
                nxt = lines[i].rstrip("\r\n")
                if nxt[:1].isspace() or nxt.lstrip().startswith("- "):
                    i += 1
                    continue
                break
            continue
        out.append(line)
        i += 1
    new_inner = "".join(out)
    appended = [
        _render_fm_entry(k, v).replace("\n", term)
        for k, v in changed.items() if k not in seen
    ]
    if appended:
        if new_inner and not new_inner.endswith(("\n", "\r")):
            new_inner += term
        new_inner += term.join(appended) + term
    return new_inner


# Per-run mechanical-rewrite bundle (Kit-Code-Quality-Plan Open Decision, decided
# 2026-07-24): a full --apply pass routes every structured_rewrite pre-image into
# ONE bundle directory `<archive>/<date>-audit-apply/`, mirroring the source-
# relative path inside it, with a `manifest.md` (one line per archived file) —
# not hundreds of dated per-source-path copies scattered through the archive. The
# bundle ages normally under the 30-day archive retention. Set once per --apply
# run in _audit_passes; None (detect-only, or a direct call outside a run) falls
# back to the substrate's default per-source-path layout.
_APPLY_BUNDLE_DIR: Path | None = None
_APPLY_BUNDLE_MANIFEST: Path | None = None


def _append_bundle_manifest(source: Path, dest: Path, first_write: bool,
                            *, kind: str | None = None) -> None:
    """Append one manifest line per rewrite (first-write-wins keeps ONE pristine
    pre-image per source in the bundle, but every write still records a line):
    `<ISO-8601Z> | <source-path> | <bytes> | <sha256-first-12> | pre-image: <kind>`
    where <kind> is `first-write` (this rewrite created the pristine pre-image) or
    `retained` (an earlier rewrite this run already archived it). `bytes`/`sha` are
    of the retained pristine pre-image on disk, so they identify the true starting
    state either way.

    An explicit `kind` labels a pre-image that lives outside the edit namespace —
    `case-rename` for the `case-renames/` copy, which is a fresh capture at rename
    time rather than the run's pristine original, so an operator reading the
    manifest can tell the two same-source lines apart."""
    if _APPLY_BUNDLE_MANIFEST is None:
        return
    rel = _rel(source)
    try:
        data = Path(dest).read_bytes()
    except OSError as exc:
        # The pre-image itself is safe (structured_rewrite archived it first);
        # only the manifest LINE is at risk. A silent drop would leave an operator
        # enumerating the run from manifest.md with an incomplete list and no
        # warning — so surface it (stderr + a finding), and never raise.
        print(f"[bundle-manifest] read failed for {rel}: {exc}", file=sys.stderr)
        _log_fix(rel, f"bundle-manifest-failed: {exc}")
        return
    sha12 = hashlib.sha256(data).hexdigest()[:12]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    kind = kind or ("first-write" if first_write else "retained")
    line = f"{ts} | {rel} | {len(data)} | {sha12} | pre-image: {kind}\n"
    try:
        _APPLY_BUNDLE_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        with open(_APPLY_BUNDLE_MANIFEST, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line)
    except OSError as exc:
        print(f"[bundle-manifest] append failed for {rel}: {exc}", file=sys.stderr)
        _log_fix(rel, f"bundle-manifest-failed: {exc}")


# Every case-only rename this run performed: (pre-rename path, post-rename path,
# archived pre-image). The end-of-run sweep re-checks each one and puts a
# vanished file back from its pre-image (`_restore_lost_case_renames`).
_CASE_RENAMES_THIS_RUN: list[tuple[Path, Path, Path]] = []

# Case-rename pre-images take their OWN namespace inside the run bundle:
# `<bundle-dir>/case-renames/<source-relative-path>`, separate from the edit
# pre-images at `<bundle-dir>/<source-relative-path>`.
#
# The two must not share a path. Passes 2/3/4 write frontmatter BEFORE pass 6
# renames, in the same per-file loop, so by rename time the edit namespace
# already holds this source's PRISTINE original — and first-write-wins would
# skip the rename's own copy. Every consumer of the rename pre-image (the
# post-rename size verify, the end-of-pass sweep, the crash-path sweep) then
# compared or restored against that stale original, reverting the run's own
# frontmatter fixes and logging a rename line that told the operator nothing had
# gone wrong. Its own namespace, written from a FRESH read at rename time, makes
# the copy a true picture of what the rename moved. The edit namespace keeps
# first-write-wins untouched: whole-run rollback still restores the true
# starting state.
_CASE_RENAME_BUNDLE_SUBDIR = "case-renames"


def _archive_case_rename_preimage(path: Path) -> Path | None:
    """Copy `path` into this run's bundle BEFORE a case-only rename touches it,
    and record the manifest line. Returns the pre-image path, or None when no
    recoverable copy could be made.

    The case-only rename was the ONE mutating path in this script with no
    archive-first step: `_write_frontmatter` archives through
    `structured_rewrite`, `rename_with_links` copies before it deletes, and
    `subfolder_housekeeping` hash-verifies its archive copy — the case rename
    moved the only copy of the file with nothing behind it. Two runs
    (2026-07-26 08:28Z and 19:22Z) lost case-pending files with no tmp, no
    destination, no source, and no trace; the missing pre-image is why the loss
    was unrecoverable. Archiving first makes a failure at ANY later point
    recoverable, so the rename never carries the only copy.

    The copy is a FRESH read taken at rename time and lands in the bundle's
    `case-renames/` namespace (`_CASE_RENAME_BUNDLE_SUBDIR`), so it holds exactly
    the bytes the rename moved — frontmatter fixes this run already applied
    included. Sharing the edit namespace made the copy the run's pristine
    original instead, and every restore from it silently undid those fixes.

    Falls back to the substrate's dated per-source-path layout when no run
    bundle is set (a direct call outside a run).

    The copy, the free-name probe, and the verification are the SHARED
    `archive_first.archive_preimage` step (CONFIG § Helper-script automation) —
    one implementation for every kit script, and a sha256 comparison rather than
    the size check this function used to carry. `None` is returned on failure so
    pass 6 declines the rename; the helper raises rather than returning a
    half-made copy, so there is no state in between."""
    try:
        if _APPLY_BUNDLE_DIR is not None:
            # One case-rename per source per run, so the helper's probe normally
            # returns the mirrored path itself; it is there so a repeat never
            # overwrites an archive file.
            dest = archive_preimage(
                path,
                vault_root=VAULT_ROOT,
                archive_root=_APPLY_BUNDLE_DIR / _CASE_RENAME_BUNDLE_SUBDIR,
                mirror_tree=True,
            )
        else:
            dest = archive_preimage(path, vault_root=VAULT_ROOT)
        _append_bundle_manifest(path, dest, True, kind="case-rename")
        return dest
    except (ArchiveFirstError, OSError, OutsideVaultError) as exc:
        _log_fix(_rel(path), f"case-rename-preimage-failed: {exc}")
        return None


def _restore_lost_case_renames() -> None:
    """Put back any file this run case-renamed that is no longer on disk.

    The rename itself is atomic and content-preserving (proven in the sandbox),
    yet both loss incidents removed exactly the case-renamed files while the
    pass was still running — a watcher outside this script acting on the rename.
    So the run verifies its own work: a case-renamed file that is absent under
    BOTH spellings at the end of the pass is written back from its archived
    pre-image and logged. Idempotent — a file still present is left alone.

    The pre-image is the `case-renames/` copy captured at rename time, so the
    restored file carries every fix this run had already written to it."""
    for source, dest, pre_image in list(_CASE_RENAMES_THIS_RUN):
        try:
            if dest.exists() or source.exists():
                continue
            if not pre_image.is_file():
                _log_fix(_rel(dest),
                         "case-rename-restore-failed: pre-image missing at "
                         f"{_rel(pre_image)}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(pre_image.read_bytes())
            _log_fix(_rel(dest), "case-rename-restored",
                     f"from {_rel(pre_image)} (file vanished after the rename)")
        except OSError as exc:
            _log_fix(_rel(dest), f"case-rename-restore-failed: {exc}")


def _write_frontmatter(file_path: Path, fm: dict, body: str | None = None) -> None:
    """Targeted raw-block frontmatter write (CONFIG § Versioning and archiving
    discipline). Rewrite ONLY the changed keys' lines; untouched frontmatter
    lines and the body stay byte-identical — key order and quoting survive (the
    old full `yaml.dump` alphabetized every key on every pass). Archive-first,
    reparse-verify the result, restore the archived pre-image on failure — all
    via the substrate's `structured_rewrite`. During an --apply run the pre-image
    lands in the per-run bundle (`<archive>/<date>-audit-apply/`, mirrored
    source-relative path) with a manifest line; otherwise the substrate's default
    per-source-path layout is used.

    `body=None` is a frontmatter-only edit: the on-disk body is preserved
    verbatim. A caller that rewrote the body (pass12) passes the new body.

    The read routes through the strict-UTF-8 probe, so a non-UTF-8 file is
    skipped and reported rather than rewritten with replacement bytes."""
    if DRY_RUN:
        return
    disk_text = _read_text(file_path)
    if disk_text is None:
        return  # vanished or non-UTF-8 — handled/reported by _read_text
    spans = split_frontmatter(disk_text)
    disk_fm = spans.to_dict() if spans.has_frontmatter else {}
    changed = {
        k: v for k, v in fm.items()
        if k not in disk_fm or not _fm_value_equal(disk_fm.get(k), v)
    }
    new_body = spans.body if body is None else body
    if not changed and new_body == spans.body:
        return  # nothing to write

    if spans.has_frontmatter:
        new_text = (
            spans.opening
            + _targeted_fm_rewrite(spans.inner, changed)
            + spans.closing
            + new_body
        )
    else:
        entries = "\n".join(_render_fm_entry(k, v) for k, v in fm.items())
        new_text = f"---\n{entries}\n---\n" + new_body

    expected_body = new_body

    def _invariant(text: str) -> bool:
        s = split_frontmatter(text)
        return s.has_frontmatter and isinstance(s.to_dict(), dict) and s.body == expected_body

    try:
        if _APPLY_BUNDLE_DIR is not None:
            # First-write-wins: check whether the bundle already holds this
            # source's pristine pre-image (an earlier rewrite this run) BEFORE the
            # call, so the manifest marks first-write vs retained. structured_rewrite
            # keeps the pristine copy and restores from its own in-memory pre-image.
            bundle_dest = archive_dest_for(
                Path(file_path), VAULT_ROOT,
                archive_root=_APPLY_BUNDLE_DIR, mirror_tree=True,
            )
            first_write = not bundle_dest.exists()
            dest = structured_rewrite(
                Path(file_path), new_text, vault_root=VAULT_ROOT, invariant=_invariant,
                archive_root=_APPLY_BUNDLE_DIR, mirror_tree=True, preserve_existing=True,
            )
            _append_bundle_manifest(Path(file_path), dest, first_write)
        else:
            structured_rewrite(
                Path(file_path), new_text, vault_root=VAULT_ROOT, invariant=_invariant,
            )
    except (StructuralInvariantError, OutsideVaultError, OSError) as exc:
        _log_fix(_rel(Path(file_path)), f"frontmatter-write-failed: {exc}")


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


# Levenshtein-1 is the shared config_variables.levenshtein_le1 (imported); the
# private audit copy is retired.


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
        text = _read_text(p)
        if text is None:
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
        if _withheld_by_selector("type"):
            _log_fix(_rel(path), "type-would-normalize",
                     f"{_withhold_marker('type')} {raw!r} -> {canonical!r}")
            return fm, False
        fm = dict(fm)
        fm["type"] = canonical
        _log_fix(_rel(path), "type-normalized", f"{raw!r} -> {canonical!r}")
        return fm, True
    return fm, False


# ---------------------------------------------------------------------------
# Pass 3 — Tag normalization
# ---------------------------------------------------------------------------

def pass3_tag_normalize(path: Path, fm: dict) -> tuple[dict, bool]:
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
        # Ambiguous-tag deferral: a tag within edit-distance 1 of TWO OR MORE
        # canonical tags is a guess the user should confirm, not a deterministic
        # fix — writing normalize_tag's first frozenset-iteration match to disk
        # pre-empts the adjudication. Leave it as-is and let the snapshot's
        # `tag-resolution` finding route it (build_state_index emits exactly this
        # case on the same len(near) >= 2 gate).
        _tl = tag_str.strip().lower()
        if len([c for c in CANONICAL_TAG_KEYS if levenshtein_le1(_tl, c)]) >= 2:
            new_tags.append(tag_str)
            continue
        canonical = normalize_tag(tag_str)
        if canonical:
            if canonical != tag_str and _withheld_by_selector("tag"):
                # Class held back this run: keep the tag as written and report
                # the fix as a finding, exactly as a dry run would.
                new_tags.append(tag_str)
                _log_fix(_rel(path), "tag-would-normalize",
                         f"{_withhold_marker('tag')} {tag_str!r} -> {canonical!r}")
                continue
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

_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _count_code_blocks(body: str) -> int:
    return len(_CODE_FENCE_RE.findall(body))


def _count_wikilinks(body: str) -> int:
    """Count non-embed wikilink occurrences (the shared wikilink_helpers pattern,
    replacing the deleted private `_WIKILINK_RE`). Occurrences, not unique
    basenames, so pass12's rewrite invariant (count before == count after) holds."""
    return sum(1 for embed, _interior in WIKILINK_RE.findall(body) if not embed)


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
        if _withheld_by_selector("type"):
            _log_fix(_rel(path), "type-would-infer",
                     f"{_withhold_marker('type')} -> {inferred!r}")
            return fm, False
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
        if _apply_fixes("type-folder"):
            dest.parent.mkdir(parents=True, exist_ok=True)
            result = rename_with_links(path, dest, VAULT_ROOT)
            if result.status in ("renamed", "no-op"):
                _log_fix(rel, "type-folder-relocated", f"-> {_rel(dest)}")
                # Stamp the `auto-fixed` provenance tag AFTER the move so the
                # relocation keeps it (decided 2026-07-24): write it onto the
                # MOVED file at `dest`, targeted to the tags line only.
                new_fm = dict(fm)
                tags = list(new_fm.get("tags") or [])
                if "auto-fixed" not in tags:
                    tags.append("auto-fixed")
                new_fm["tags"] = tags
                _write_frontmatter(dest, new_fm)
                return dest, new_fm, True
            else:
                _log_fix(rel, f"type-folder-relocation-failed: {result.error}")
                return path, fm, False
        else:
            _log_fix(rel, "type-folder-mismatch",
                     f"{_withhold_marker('type-folder')} would move -> {_rel(dest)}")
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
    # samefile check, and perform it here rather than through rename_with_links
    # (whose dest-exists validation refuses it).
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
        if _apply_fixes("naming"):
            # Archive-first (CONFIG § Versioning). The rename used to move the
            # only copy of the file with nothing behind it; a pre-image in this
            # run's bundle makes a failure at any later point recoverable. No
            # pre-image means no rename — the file keeps its off-convention name
            # until the archive works, which is a finding, not a data loss.
            pre_image = _archive_case_rename_preimage(path)
            if pre_image is None:
                _log_fix(rel, "naming-rename-skipped: case-only pre-image "
                              "unavailable", canonical)
                return path, False
            # ONE atomic step. `os.replace` performs a case-only rename within a
            # directory on NTFS in a single MoveFileEx call (verified in the
            # sandbox), so the file is never parked under a third name. The old
            # two-step temp dance (`path.rename(tmp)` then `tmp.rename(dest)`)
            # opened a window in which the canonical name did not exist and the
            # only copy lived at `<stem>.case-rename.tmp.md`.
            try:
                os.replace(path, dest)
            except OSError as exc:
                _log_fix(rel, f"naming-rename-failed: case-only: {exc}")
                return path, False
            # Register the rename the instant it returns, ahead of the verify and
            # everything after it, so the end-of-run sweep covers this file even
            # if the very next statement raises.
            _CASE_RENAMES_THIS_RUN.append((path, dest, pre_image))
            # Verify the rename landed and the bytes survived; restore from the
            # pre-image when it did not.
            try:
                ok = dest.is_file() and dest.stat().st_size == pre_image.stat().st_size
            except OSError:
                ok = False
            if not ok:
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_bytes(pre_image.read_bytes())
                    _log_fix(rel, "case-rename-restored",
                             f"from {_rel(pre_image)} (post-rename verify failed)")
                except OSError as exc:
                    _log_fix(rel, f"case-rename-restore-failed: {exc}")
                    return path, False
            # Rewrite inbound wikilinks to the canonical casing so the exact-
            # stem indices (basename/inbound) keep resolving next run. Uses the
            # shared wikilink_helpers rewrite + safe-write primitives (the
            # private `_rewrite_inbound` was retired from the mover).
            old_stem, new_stem = path.stem, dest.stem
            try:
                for ref_file, _cnt in _find_inbound(VAULT_ROOT, old_stem, exclude=dest):
                    ref_text = read_text_or_none(ref_file)
                    if ref_text is None:
                        continue
                    rewritten, cnt = rewrite_wikilink_interior(ref_text, old_stem, new_stem)
                    if cnt:
                        write_text(ref_file, rewritten)
            except Exception as exc:
                _log_fix(rel, "naming-renamed",
                         f"-> {canonical} (link recase incomplete: {exc})")
                return dest, True
            _log_fix(rel, "naming-renamed", f"-> {canonical} (case-only)")
            return dest, True
        else:
            _log_fix(rel, "naming-would-rename",
                     f"{_withhold_marker('naming')} -> {canonical} (case-only)")
            return path, False

    if _apply_fixes("naming"):
        result = rename_with_links(path, dest, VAULT_ROOT)
        if result.status in ("renamed", "no-op"):
            _log_fix(rel, "naming-renamed", f"-> {canonical}")
            return dest, True
        else:
            _log_fix(rel, f"naming-rename-failed: {result.error}")
            return path, False
    else:
        _log_fix(rel, "naming-would-rename",
                 f"{_withhold_marker('naming')} -> {canonical}")
        return path, False


# ---------------------------------------------------------------------------
# Pass 7 — Parent inference and backlink stamping
# ---------------------------------------------------------------------------

def _find_parent_from_folder(path: Path) -> str | None:
    """Level 1: folder hints. Inside {projects}/X/ → X's index.

    A folder cover note (stem == its containing folder name) sits at the top of
    its folder; its parent is upward, never a sibling or itself, so it is left
    empty for inference. Sibling candidates equal to this file's own stem are
    also skipped — a note never parents itself.
    """
    if path.stem == path.parent.name:
        return None
    try:
        rel_to_projects = path.relative_to(PROJECTS_FOLDER)
        parts = rel_to_projects.parts
        if len(parts) >= 2:
            project_name = parts[0]
            # Look for a Project.md or index inside this project folder
            project_folder = PROJECTS_FOLDER / project_name
            candidates = [c for c in project_folder.glob("*.md") if c.stem != path.stem]
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
    for m in WIKILINK_RE.finditer(body):
        if m.group(1):  # embed ![[...]] — skip
            continue
        target = normalize_link_target(m.group(2))
        if not target:
            continue
        targets = basename_index.get(target, [])
        for t in targets:
            text = _read_text(t)
            if text is None:
                continue
            fm2, _, _ = _parse_frontmatter(text)
            if fm2 and fm2.get("type") == "index":
                return target
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

    # Never self-reference: a note is not its own parent (a cover, a root).
    if inferred_parent and inferred_parent != path.stem:
        if _withheld_by_selector("parent"):
            _log_fix(rel, "parent-would-infer",
                     f"{_withhold_marker('parent')} parent: [[{inferred_parent}]]")
            return fm, body, False
        # Stamp the inferred parent into FRONTMATTER, never a body line — a
        # frontmatter key in the body breaks parent resolution downstream.
        new_fm = dict(fm)
        new_fm["parent"] = f"[[{inferred_parent}]]"
        tags = list(new_fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        new_fm["tags"] = tags
        _log_fix(rel, "parent-inferred", f"parent: [[{inferred_parent}]]")
        return new_fm, body, True

    # Level 3: emit to inference needs
    _log_inference("parent-finding", rel, observed_value="(no parent:)",
                   context_hint="could not resolve parent from folder or body links")
    return fm, body, False


# ---------------------------------------------------------------------------
# Pass 8 — Index child registration
# ---------------------------------------------------------------------------

def _register_into_index(index_path: Path, path: Path, rel: str, label: str) -> None:
    """Register `path.stem` into an index cover (idempotent), logging the add."""
    if _apply_fixes("index-child"):
        try:
            if add_child_link_to_index(index_path, path.stem):
                _log_fix(rel, "index-child-registered", f"-> {label}")
        except Exception as exc:
            _log_fix(rel, f"index-child-registration-failed: {exc}")
    else:
        _log_fix(rel, "index-child-would-register",
                 f"{_withhold_marker('index-child')} -> {label}")


_FOLDER_INDEX_COVER_CACHE: dict[str, "Path | None"] = {}


def _folder_index_cover(path: Path, basename_index: dict[str, list[Path]]) -> Path | None:
    """The index-type folder-note COVER of `path`'s containing folder, or None.

    A cover is the folder-note whose stem equals the folder name OR a legacy `NN-`
    prefixed form of it (the shared `is_folder_cover` predicate — CONFIG § Numbering
    keeps a legacy `00-`/`01-` cover acting as the folder index). Result is cached
    per folder (the cover's identity/type does not change within a run), so the
    sibling scan runs once per folder, not once per note. Returns None for the
    cover itself (a folder index never indexes into itself)."""
    folder = path.parent
    key = str(folder)
    if key not in _FOLDER_INDEX_COVER_CACHE:
        cover: Path | None = None
        for cand in sorted(folder.glob("*.md")):
            if not is_folder_cover(cand):
                continue
            text = _read_text(cand)
            if text is None:
                continue
            c_fm, _, _ = _parse_frontmatter(text)
            if c_fm and normalize_type(str(c_fm.get("type") or "")) == "index":
                cover = cand
                if cand.stem == folder.name:
                    break  # the exact folder-note wins over a legacy-prefixed one
        _FOLDER_INDEX_COVER_CACHE[key] = cover
    cover = _FOLDER_INDEX_COVER_CACHE[key]
    return cover if cover != path else None


def pass8_index_children(
    path: Path, fm: dict, body: str, basename_index: dict[str, list[Path]]
) -> None:
    """Register a file into its index. Two registration paths:

    1. Parent-named index: the note's `parent` (frontmatter, else a legacy body
       line) points at a note whose type is `index`.
    2. Physical-location index (parent-scoped blind-spot fix,
       Note-Kit-Structural-Integrity-Plan): a note that physically sits in a
       folder carrying an index-type folder-note cover is registered into that
       cover REGARDLESS of where its `parent` points — the Presentation-Splat
       miss, where a note whose parent named a project cover registered nowhere.

    Both are idempotent (add_child_link_to_index no-ops when already present), so
    a note already in an index is not re-registered or duplicated."""
    rel = _rel(path)

    # Path 1 — parent-named index.
    target = None
    fm_parent_raw = fm.get("parent")
    if fm_parent_raw is not None and str(fm_parent_raw).strip():
        mlink = re.search(r"\[\[([^\[\]]+?)\]\]", str(fm_parent_raw))
        target = normalize_link_target(mlink.group(1)) if mlink else None
    if not target:
        parent_match = re.search(r"^[Pp]arent:\s*\[\[([^\[\]]+?)\]\]", body, re.MULTILINE)
        if parent_match:
            target = normalize_link_target(parent_match.group(1))

    parent_index: Path | None = None
    if target:
        for index_path in basename_index.get(target, []):
            text = _read_text(index_path)
            if text is None:
                continue
            index_fm, _, _ = _parse_frontmatter(text)
            if index_fm and index_fm.get("type") == "index":
                parent_index = index_path
                _register_into_index(index_path, path, rel, f"[[{target}]]")
                break  # only register in first matching parent index

    # Path 2 — physical-location index (skip inbox/archive transit zones and the
    # cover itself; a folder's index never indexes into itself).
    in_transit = False
    for zone in (INBOX_FOLDER, ARCHIVE_FOLDER):
        try:
            path.relative_to(zone)
            in_transit = True
        except ValueError:
            pass
    if not in_transit:
        folder_index = _folder_index_cover(path, basename_index)
        if (folder_index is not None and folder_index != path
                and folder_index != parent_index):
            _register_into_index(folder_index, path, rel, f"folder index [[{folder_index.stem}]]")

    # Check for index files: zero children or overflow
    if fm.get("type") == "index":
        child_count = _count_wikilinks(body)
        if child_count == 0:
            _log_inference("index-empty", rel, context_hint="index has no children")
        elif child_count > 40:
            _log_inference("index-overflow", rel,
                           observed_value=str(child_count),
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
    if _apply_fixes("orphan"):
        if not dest.exists():
            result = rename_with_links(path, dest, VAULT_ROOT)
            if result.status in ("renamed", "no-op"):
                # Update frontmatter on the destination (targeted: reviewed +
                # inferred tag only; body preserved).
                dest_text = _read_text(dest)
                if dest_text is not None:
                    dest_fm, _dest_body, _ = _parse_frontmatter(dest_text)
                    if dest_fm is not None:
                        dest_fm = dict(dest_fm)
                        dest_fm["reviewed"] = False
                        tags = list(dest_fm.get("tags") or [])
                        if "inferred" not in tags:
                            tags.append("inferred")
                        dest_fm["tags"] = tags
                        _write_frontmatter(dest, dest_fm)
                _log_fix(rel, "orphan-moved-to-inbox", f"-> {_rel(dest)}")
            else:
                _log_fix(rel, f"orphan-move-failed: {result.error}")
        else:
            _log_fix(rel, "orphan-inbox-collision", path.name)
    else:
        _log_fix(rel, "orphan-would-move-to-inbox",
                 f"{_withhold_marker('orphan')} -> {_rel(dest)}")


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
    # Build referenced asset names from the snapshot's md files.
    referenced: set[str] = set()
    asset_ref_re = re.compile(
        r"!\[\[([^\[\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
        r"|!\[[^\]]*\]\(([^)\s]+?)(?:\s+[^)]+)?\)"
        r"|\[\[([^\[\]|#]+?)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]"
    )

    def _collect_refs(text: str) -> None:
        for m in asset_ref_re.finditer(text):
            ref = m.group(1) or m.group(2) or m.group(3) or ""
            if ref:
                referenced.add(ref.strip())
                referenced.add(Path(ref).name)

    for md_path in all_md:
        text = _read_text(md_path)
        if text is None:
            continue
        _collect_refs(text)

    # Include the read-only archive scan: an asset embedded only by an ARCHIVED
    # doc is still referenced, so it must not be counted orphaned and relocated
    # out from under those archived embeds. The archive is hands-off to the walk,
    # so scan it here explicitly (read-only; nothing under <archive> is moved).
    if ARCHIVE_FOLDER.is_dir():
        for ap in ARCHIVE_FOLDER.rglob("*.md"):
            if any(is_excluded_dir(part) for part in ap.parts):
                continue
            text = _read_text(ap)
            if text is None:
                continue
            _collect_refs(text)

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

        if _apply_fixes("loose-asset"):
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                import shutil
                shutil.move(str(asset_path), str(dest))
                _log_fix(_rel(asset_path), "loose-asset-relocated", f"-> {_rel(dest)}")
            except Exception as exc:
                _log_fix(_rel(asset_path), f"loose-asset-move-failed: {exc}")
        else:
            _log_fix(_rel(asset_path), "loose-asset-would-relocate",
                     f"{_withhold_marker('loose-asset')} -> {_rel(dest)}")


# ---------------------------------------------------------------------------
# Pass 11 — Missing project folder
# ---------------------------------------------------------------------------

def _uplink_type_mismatch(path: Path, rel: str, fm: dict, proj_name: str,
                          resolved: list[Path]) -> None:
    """Report a `project:` uplink that resolves under `<projects>` to a target
    that is not `type: project` — the phantom-project class.

    A `project:` link names the note's PROJECT, and the rollups that group work
    by project read that link as the project's identity. A session pointing at
    its project's PLAN, or at a loose note that merely lives inside a project
    folder, therefore invents a project that does not exist: the link resolves,
    the folder check passes, and every count downstream is filed under a name
    with no project behind it. Resolution alone is not identity, so the target's
    own `type` is read.

    Detect-only in every mode, `--apply` included: repointing an uplink decides
    which project owns a note, which is the owner's call, not a mechanical one.
    `<archive>`/`<history>` copies are out of scope — a preserved note's uplink
    is settled history. Silent when any resolving candidate IS a project cover,
    so an ambiguous basename never reports a mismatch it cannot prove."""
    if _is_preserved_copy(path):
        return

    observed: list[str] = []
    for tgt in resolved:
        text = _read_text(tgt)
        if text is None:
            continue  # vanished or non-UTF-8 — no basis to judge this candidate
        t_fm, _, err = _parse_frontmatter(text)
        if err or t_fm is None:
            continue  # unparseable frontmatter — pass 1 owns that report
        ttype = normalize_type(str(t_fm.get("type") or "")) or "(none)"
        if ttype == "project":
            return  # the link resolves to a real project cover
        observed.append(f"{_rel(tgt)} is type: {ttype}")

    if observed:
        _log_detect("uplink-type-mismatch", rel,
                    f"project: [[{proj_name}]] -> " + "; ".join(observed))


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

    # The `project:` value is a WIKILINK, not a literal folder name — e.g.
    # `project: "[[X]]"` points at the project's folder-note cover
    # `<projects>/X/X.md` (legacy: `00-X.md`). The project exists when the link target
    # resolves anywhere under the projects root; the check is link
    # resolution, not folder-name equality. (Fixes the live false positive
    # where every index-linked project was reported missing.)
    #
    # Resolution runs BEFORE the folder-name check so a link that resolves is
    # handed to the uplink type check either way; the early return is the same
    # either order, so the missing-folder codes are unchanged.
    resolved: list[Path] = []
    for tgt in basename_index.get(proj_name, []):
        try:
            tgt.relative_to(PROJECTS_FOLDER)
        except ValueError:
            continue
        resolved.append(tgt)

    if resolved:
        _uplink_type_mismatch(path, rel, fm, proj_name, resolved)
        return  # link resolves under the projects root — project exists

    if proj_folder.is_dir():
        return

    # Guard: if the top-level projects folder does not exist yet (e.g. a
    # fresh scaffold), iterdir() would raise FileNotFoundError. Skip gracefully.
    if not PROJECTS_FOLDER.is_dir():
        return

    # Check existing project folders for a fuzzy match
    existing_projects = [p.name for p in PROJECTS_FOLDER.iterdir() if p.is_dir()]
    close_matches = [ep for ep in existing_projects if levenshtein_le1(proj_name.lower(), ep.lower())]

    if close_matches:
        match = close_matches[0]
        new_fm = dict(fm)
        new_fm["project"] = f'[[{match}]]'
        tags = list(new_fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        new_fm["tags"] = tags
        if _withheld_by_selector("project-ref"):
            _log_fix(rel, "project-ref-would-correct",
                     f"{_withhold_marker('project-ref')} {proj_name!r} -> {match!r}")
            return
        _log_fix(rel, "project-ref-fuzzy-corrected", f"{proj_name!r} -> {match!r}")
        if _apply_fixes("project-ref"):
            _write_frontmatter(path, new_fm)
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
    path: Path, fm: dict, body: str, basename_index: dict[str, list[Path]]
) -> None:
    """Resolve broken body wikilinks in filed files.

    For each link that does not resolve, in order:
      1. Space->hyphen normalization (sibling of the casing rewrite): when the
         hyphenated form of a spaced link basename resolves to a real file
         (`[[X Y Z]]` -> `[[X-Y-Z]]`), rewrite the link in place. Apply-gated;
         the wikilink count is invariant across the rewrite (links are replaced,
         never added or dropped), so a count change aborts the write.
      2. Otherwise emit a queue candidate for the janitor to resolve by hand.
    """
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
    new_body = body
    for link in extract_wikilinks(body):
        if not link:
            continue
        if link in basename_index:
            continue
        # Step 1 — space->hyphen normalization, only when the hyphenated
        # basename resolves to a real file.
        if " " in link:
            hyphenated = link.replace(" ", "-")
            if hyphenated in basename_index:
                if not _apply_fixes("body-wikilink"):
                    _log_fix(rel, "body-wikilink-would-normalize",
                             f"{_withhold_marker('body-wikilink')} "
                             f"[[{link}]] -> [[{hyphenated}]]")
                else:
                    rewritten, count = rewrite_wikilink_interior(new_body, link, hyphenated)
                    if count:
                        new_body = rewritten
                        _log_fix(rel, "body-wikilink-normalized",
                                 f"[[{link}]] -> [[{hyphenated}]] ({count})")
                continue
        # Step 2 — unresolved: queue candidate for hand resolution.
        _log_queue(
            rule="body-wikilink-resolution",
            path=rel,
            summary=f"broken link [[{link}]]",
            suggested_options="link-to-existing/suppress/delete",
            cluster_key=f"body-wikilink:{containing_folder}",
        )

    if new_body != body and _apply_fixes("body-wikilink"):
        # Structural invariant: a pure rewrite preserves the wikilink count.
        if _count_wikilinks(new_body) == _count_wikilinks(body):
            _write_frontmatter(path, fm, new_body)
        else:
            _log_fix(rel, "body-wikilink-normalize-aborted",
                     "wikilink count changed; rewrite discarded")


# ---------------------------------------------------------------------------
# Pass 14 — Flag an in-progress idea whose originating session completed
# ---------------------------------------------------------------------------

def pass14_flag_completed_idea(
    path: Path, fm: dict, basename_index: dict[str, list[Path]]
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
    and archive.
    """
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
        s_text = _read_text(sp)
        if s_text is None:
            continue
        s_fm, _, _ = _parse_frontmatter(s_text)
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
        text = _read_text(p)
        if text is None:
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
        # Prune excluded, hands-off (script-skip — the archive `*`, the queue files
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
        text = _read_text(ref_path)
        if text is None:
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
        text = _read_text(p)
        if text is None:
            continue
        fm, body, err = _parse_frontmatter(text)
        if err or fm is None:
            continue  # unparseable YAML is pass 1's finding, not a date hole
        if fm.get("date"):
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

        if not _apply_fixes("date"):
            note = "" if DRY_RUN else " (class skipped this run)"
            _log_detect("date-resolved-pending", rel,
                        f"{resolved} via {source}{note}")
            continue

        new_fm = dict(fm)
        new_fm["date"] = resolved
        tags = list(new_fm.get("tags") or [])
        if "inferred" not in tags:
            tags.append("inferred")
        new_fm["tags"] = tags
        try:
            _write_frontmatter(p, new_fm)   # frontmatter-only: body preserved
            _log_fix(rel, "date-resolved", f"{resolved} via {source}")
        except Exception as exc:
            _log_fix(rel, f"date-resolve-failed: {exc}")


# ---------------------------------------------------------------------------
# Pass 17 — Plan multiplicity (detect-only; NEVER auto-fixes)
# ---------------------------------------------------------------------------

_SUPERSESSION_KEYS = ("superseded-by", "superseded_by", "supersedes")

# Which plan of a declared pair the key marks as superseded: the note itself for
# a `superseded-by` value ("I was replaced by X"), the plan it names for
# `supersedes` ("I replaced X").
_SUPERSESSION_KEY_SETTLES = {
    "superseded-by": "self",
    "superseded_by": "self",
    "supersedes": "named",
}

# A body line reads the same way: `supersedes` in the active voice puts the named
# plan out of the running, and every other form of the word — "superseded by",
# "superseded-by", a bare "Superseded: [[X]]" — puts this one out.
_ACTIVE_SUPERSESSION_RE = re.compile(r"supersedes\b")
_PASSIVE_SUPERSESSION_RE = re.compile(r"supersed(?:ed|es|e)?[\s_-]*by\b")


def _declared_supersessions(
    path: Path, fm: dict, text: str, in_scope_stems: set[str]
) -> set[str]:
    """Return the stems a declared supersession settles for `path`.

    A supersession is DECLARED three ways, each naming its counterpart:
      - `status: superseded` (or any status carrying `supersed`) — this plan is
        out of the running whether or not it names its successor;
      - a `superseded-by` / `superseded_by` / `supersedes` frontmatter value
        naming exactly one in-scope plan;
      - a body LINE that carries `supersed` and names exactly one in-scope plan.

    A declaration settles the SUPERSEDED plan of the pair, and only that one. The
    successor is the plan the scope kept — it is live, canonical, and still
    counts toward the scope's plan multiplicity, so it stays in the reported
    list. Settling both meant a scope of three unsettled live plans reported two,
    and a scope of two reported none: one declared supersession made the
    surviving plan invisible to the very count it was supposed to survive.
    Direction comes from the declaration itself (`_SUPERSESSION_KEY_SETTLES` for
    a frontmatter key, active vs passive voice for a body line).

    Two narrowings keep this to the pair it settles:

    The match is line-scoped. Scanning the whole body meant one incidental use
    of the word anywhere in a long plan silenced the entire scope — a project
    holding twenty live plans reported none of them because a sentence
    somewhere used "supersedes" about something else.

    The line names ONE other plan. A line naming several is a list — an
    orchestration master enumerating its work packages — and a list is not a
    declared pair. One such line settled five plans at a stroke."""
    settled: set[str] = set()
    others = in_scope_stems - {path.stem}
    lower_others = {o.lower(): o for o in others}

    if "supersed" in str(fm.get("status") or "").lower():
        settled.add(path.stem)

    def _pair_from(blob: str, settles: str) -> None:
        named = [orig for low, orig in lower_others.items() if low in blob]
        if len(named) == 1:
            settled.add(path.stem if settles == "self" else named[0])

    for key in _SUPERSESSION_KEYS:
        value = str(fm.get(key) or "").lower()
        if value.strip():
            _pair_from(value, _SUPERSESSION_KEY_SETTLES[key])

    for line in text.splitlines():
        low_line = line.lower()
        if "supersed" in low_line:
            # Passive voice wins the read: "Alpha supersedes Beta" settles Beta,
            # while "superseded by Alpha" and a bare "Superseded: [[Alpha]]"
            # settle this note.
            settles = "self"
            if (_ACTIVE_SUPERSESSION_RE.search(low_line)
                    and not _PASSIVE_SUPERSESSION_RE.search(low_line)):
                settles = "named"
            _pair_from(low_line, settles)
    return settled


def pass17_plan_multiplicity(all_md: list[Path]) -> None:
    """One canonical plan per scope. Count LIVE `type: plan` notes per scope —
    the same resolved `parent` value, or the same top-level inbox container for
    inbox drafts. A scope still holding more than one unsettled plan emits ONE
    `duplicate-canonical-plan` row naming those plans (CONFIG § Helper-script
    automation). Detect-only: this pass never moves, edits, or archives a plan.

    Two rules keep the row truthful:
      - `<archive>` and `<history>` copies stay out of the grouping. An archived
        plan sits under its old `parent`, so counting it made every scope that
        ever archived a plan look multiplied.
      - A declared supersession settles the PAIR it names — the superseded plan
        and its successor — and the rest of the scope still reports. One
        supersession no longer excuses eighteen other plans.
    """
    scopes: dict[str, list[tuple[Path, dict, str]]] = {}
    inbox_rel = _rel(INBOX_FOLDER)

    for p in all_md:
        if _is_preserved_copy(p):
            continue
        text = _read_text(p)
        if text is None:
            continue
        fm, _, err = _parse_frontmatter(text)
        if err or not fm:
            continue
        if normalize_type(str(fm.get("type") or "")) != "plan":
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
        settled: set[str] = set()
        for pp, f, t in plans:
            settled |= _declared_supersessions(pp, f, t, stems)
        unsettled = [pp for pp, _f, _t in plans if pp.stem not in settled]
        if len(unsettled) < 2:
            continue
        _log_detect(
            "duplicate-canonical-plan",
            scope,
            "; ".join(sorted(_rel(pp) for pp in unsettled)),
        )


# ---------------------------------------------------------------------------
# Pass 19 — Session word budget (detect-only; NEVER auto-trims)
# ---------------------------------------------------------------------------

# The Format-Session target for a session log's body. A log past it has stopped
# being the newest state-of-play its project reads and started being a
# transcript. Reported, never trimmed — cutting a session log is an editorial
# call, and the words belong to the person who has to keep them.
_SESSION_WORD_BUDGET = 600


def _body_word_count(body: str) -> int:
    """Words in a note body — whitespace-separated tokens after the closing
    frontmatter fence, which is what `_parse_frontmatter` already returns."""
    return len(body.split())


def pass19_session_word_budget(all_md: list[Path]) -> None:
    """A live `type: session` note whose BODY runs past `_SESSION_WORD_BUDGET`
    words files one `session-over-budget` finding carrying the count and the
    target. Detect-only in every mode, `--apply` included: this pass never
    edits, trims, or splits a session log.

    `<archive>` and `<history>` copies are out of scope — a filed session log is
    a record, and its length is settled."""
    for p in all_md:
        if _is_preserved_copy(p):
            continue
        text = _read_text(p)
        if text is None:
            continue
        fm, body, err = _parse_frontmatter(text)
        if err or not fm:
            continue
        if normalize_type(str(fm.get("type") or "")) != "session":
            continue
        words = _body_word_count(body)
        if words > _SESSION_WORD_BUDGET:
            _log_detect(
                "session-over-budget",
                _rel(p),
                f"{words} words vs {_SESSION_WORD_BUDGET} target",
            )


# ---------------------------------------------------------------------------
# Pass 20 — Plan shape lint (detect-only; NEVER auto-fixes)
# ---------------------------------------------------------------------------

# A markdown checkbox item: `- [ ]` or `- [x]`, under any list bullet, at any
# indent. A plan states the work to reach a goal as checkboxes, so a plan
# holding none is prose about the work rather than the work itself.
_CHECKBOX_RE = re.compile(r"^[ \t]*[-*+][ \t]+\[[ xX]\]", re.MULTILINE)


def pass20_plan_shape(all_md: list[Path]) -> None:
    """A live `type: plan` note containing ZERO markdown checkbox items files
    one `plan-no-checkbox` finding. Detect-only in every mode, `--apply`
    included: the missing checklist is written by whoever owns the plan.

    `<archive>` and `<history>` copies are out of scope — a filed plan's shape
    is settled."""
    for p in all_md:
        if _is_preserved_copy(p):
            continue
        text = _read_text(p)
        if text is None:
            continue
        fm, body, err = _parse_frontmatter(text)
        if err or not fm:
            continue
        if normalize_type(str(fm.get("type") or "")) != "plan":
            continue
        if not _CHECKBOX_RE.search(body):
            _log_detect("plan-no-checkbox", _rel(p), "zero checkbox items")


# ---------------------------------------------------------------------------
# CONFIG table readers for the static lints (focused parsers)
# ---------------------------------------------------------------------------
#
# `config_variables` parses the tables the routing layer needs. The two tables
# below are read by these lints alone, so they are parsed here rather than
# widening the shared module's surface: § Tags "Fast-moving domains" and
# § Retired tokens. Both read the SAME CONFIG file `config_variables` resolved
# (honouring the NOTE_KIT_CONFIG override), so a test fixture's CONFIG governs
# the lint exactly as the live one does.

_CONFIG_TEXT_CACHE: str | None = None


def _config_text() -> str:
    """The canon's text, read once per run. Empty when it cannot be read — the
    lints below then find no rows and stay silent rather than guessing."""
    global _CONFIG_TEXT_CACHE
    if _CONFIG_TEXT_CACHE is None:
        try:
            _CONFIG_TEXT_CACHE = Path(_CONFIG_PATH).read_text(encoding="utf-8")
        except OSError:
            _CONFIG_TEXT_CACHE = ""
    return _CONFIG_TEXT_CACHE


def _table_rows_after(text: str, anchor_re: str) -> list[list[str]]:
    """The cells of every body row of the FIRST markdown table after `anchor_re`.

    Header and separator rows are dropped; each remaining row is split on `|`
    with the outer empties removed. Returns [] when the anchor or the table is
    absent, so a CONFIG without the section simply yields no rows.
    """
    m = re.search(anchor_re, text, re.MULTILINE)
    if not m:
        return []
    rows: list[list[str]] = []
    started = False
    for line in text[m.end():].splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            if started:
                break          # table ended
            if stripped.startswith("#"):
                return rows    # next section reached before any table
            continue
        started = True
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(set(c) <= set("-: ") and c for c in cells):
            continue           # separator row
        rows.append(cells)
    return rows[1:] if rows else []   # drop the header row


def _fast_moving_domains() -> frozenset[str]:
    """The § Tags fast-moving domains, casefolded — a reference in one of them
    carries a version or date anchor for the provenance lint."""
    rows = _table_rows_after(_config_text(), r"^\*\*Fast-moving domains\*\*")
    return frozenset(
        r[0].strip("` ").casefold() for r in rows if r and r[0].strip("` ")
    )


# One retired-token row, reduced to what the lint matches on.
_RETIRED_ROW_FIELDS = ("literals", "pattern", "retired_date", "replacement", "scope")


def _retired_token_rows() -> list[dict]:
    """§ Retired tokens as match rules.

    Each row contributes the backticked literals in its token cell. A literal
    that is a proper substring of another literal in the SAME row is dropped, so
    the `checkpoint` row matches its resume artifact (`type: checkpoint`,
    `[[Format-Checkpoint]]`) and never the bare word — the narrow scope the
    retirement was recorded with. A row whose token cell says "pattern" matches a
    FORM instead: the numbered-folder prefix (`00-Inbox`, `99-…`), built as a
    regex rather than as its illustrative literals.
    """
    rows: list[dict] = []
    for cells in _table_rows_after(_config_text(), r"^## Retired tokens\s*$"):
        if len(cells) < 4:
            continue
        token_cell, retired_date, replacement, scope = cells[0], cells[1], cells[2], cells[3]
        literals = [t for t in re.findall(r"`([^`]+)`", token_cell) if t.strip()]
        # Drop a literal wholly contained in a longer sibling: the row's own
        # parenthetical is the precise form, the bare word is its label.
        literals = [
            t for t in literals
            if not any(t != o and t in o for o in literals)
        ]
        rows.append({
            "literals": literals,
            "pattern": "pattern" in token_cell.lower(),
            "retired_date": retired_date,
            "replacement": replacement,
            "scope": scope.lower(),
        })
    return rows


# A retired token survives in three forms the plain word-boundary match walked
# past — the shapes a token takes once it is inside code and paths rather than
# prose:
#   - underscore-glued adjacency — a token embedded in a snake_case identifier
#     or an environment-variable name (`<prefix>_<token>_root`, `<TOKEN>_<X>=1`).
#     `_` is a \w character, so `(?![\w-])` rejected exactly the identifier a
#     retired path variable lives in. `_` is a JOINER, not a letter: the edge
#     below rejects letters, digits, and hyphens and lets `_` through, so a
#     retired token hits inside an identifier while a prose word that merely
#     BEGINS with it (`fleeting` for `fleet`) stays clean.
#   - separator substitution — `_` written where the retired literal carries `-`
#     and the reverse. Every separator INSIDE a literal matches either form.
#   - percent-encoded hyphens — `%2D` (either case) as a URL or escaped path
#     writes the hyphen.
_TOKEN_EDGE_L = r"(?<![A-Za-z0-9-])"
_TOKEN_EDGE_R = r"(?![A-Za-z0-9-])"
_SEPARATOR_ALT = r"(?:[-_]|%2[dD])"


def _retired_variant_pattern(literal: str) -> str:
    """`literal` as a regex body matching its separator-substituted and
    percent-encoded forms too (`needs-live-session` also matches
    `needs_live_session` and `needs%2Dlive%2Dsession`)."""
    return _SEPARATOR_ALT.join(
        re.escape(part) for part in re.split(r"[-_]", literal)
    )


# The numbered-folder FORM the pattern row retires: two digits, a separator, then
# a capitalized name (`00-Inbox`, `00_Inbox`, `00%2DInbox`, `99-Archive`). The
# lookbehind keeps a date (`2026-07-26`) and a hyphenated number out of it.
_NUMBERED_PREFIX_RE = re.compile(
    rf"{_TOKEN_EDGE_L}\d{{2}}{_SEPARATOR_ALT}(?=[A-Z])")


def _retired_matchers(row: dict) -> list[tuple[str, re.Pattern]]:
    """(label, compiled matcher) for one retired-token row.

    A word-shaped literal matches on token edges, case-insensitively unless it
    is all-caps (an all-caps machine NAME keeps its casing, which is the point), and
    matches its glued, separator-substituted, and percent-encoded forms as well.
    A literal carrying punctuation matches as an exact substring.
    """
    out: list[tuple[str, re.Pattern]] = []
    if row["pattern"]:
        out.append(("NN- numbered folder prefix", _NUMBERED_PREFIX_RE))
        return out
    for literal in row["literals"]:
        if re.fullmatch(r"[\w-]+", literal):
            flags = 0 if literal.isupper() else re.IGNORECASE
            out.append((literal, re.compile(
                _TOKEN_EDGE_L + _retired_variant_pattern(literal) + _TOKEN_EDGE_R,
                flags)))
        else:
            out.append((literal, re.compile(re.escape(literal))))
    return out


# ---------------------------------------------------------------------------
# Pass 21 — Archive-first bypass (static; detect-only)
# ---------------------------------------------------------------------------

# Attribute calls that destroy or overwrite a file's current bytes. `rmdir` is
# absent on purpose: removing an empty DIRECTORY destroys no content, so it needs
# no pre-image (CONFIG § Helper-script automation, archive_first row).
_MUTATING_METHODS = frozenset({"unlink", "rename", "write_bytes", "write_text"})
# Module-qualified mutators. `os.replace` and `shutil.move` are counted; a bare
# `.replace(` is string replacement and is not.
_MUTATING_QUALIFIED = frozenset({
    ("os", "replace"), ("os", "remove"), ("os", "unlink"),
    ("shutil", "move"), ("shutil", "rmtree"),
})
# A module that binds any of these names routes its mutations through an
# archive-first mechanism: the shared helper, the frontmatter substrate's
# archive-first rewrite, or the run-bundle pre-image layout.
_ARCHIVE_FIRST_ROUTES = frozenset({
    "archive_first", "archive_preimage", "structured_rewrite", "archive_dest_for",
})


def _module_bound_names(tree: ast.Module) -> set[str]:
    """Every name the module imports or defines at any level — the evidence that
    it has an archive-first route available to it."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names.add(node.module.split(".")[0])
            for alias in node.names:
                names.add(alias.asname or alias.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
    return names


def _mutating_call_lines(tree: ast.Module) -> list[tuple[int, str]]:
    """(line, rendered call) for every mutating call outside a test function.

    A function whose name carries `test` — and everything nested inside it — is
    a fixture builder, not a vault mutator, so its writes are not call sites the
    archive-first rule governs.
    """
    hits: list[tuple[int, str]] = []

    def is_test(name: str) -> bool:
        return "test" in name.lower()

    def walk(node: ast.AST, in_test: bool) -> None:
        for child in ast.iter_child_nodes(node):
            child_in_test = in_test
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_in_test = in_test or is_test(child.name)
            if not child_in_test and isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute):
                    owner = getattr(func.value, "id", None)
                    if (owner, func.attr) in _MUTATING_QUALIFIED:
                        hits.append((child.lineno, f"{owner}.{func.attr}()"))
                    elif func.attr in _MUTATING_METHODS and owner not in ("os", "shutil"):
                        hits.append((child.lineno, f".{func.attr}()"))
                elif isinstance(func, ast.Name) and func.id in ("write_text", "write_bytes"):
                    hits.append((child.lineno, f"{func.id}()"))
            walk(child, child_in_test)

    walk(tree, False)
    return hits


def pass21_archive_first_bypass() -> None:
    """A kit script that deletes, renames, or overwrites a file without an
    archive-first route files one `archive-first-bypass` finding.

    CONFIG § Helper-script automation makes `archive_first.py` the step every
    mutating call site runs first; this pass is the check that the rule holds in
    the code. Scope: `<kit-root>/scripts/*.py` plus this file. A script is clear
    when it binds any archive-first route name (`archive_first`,
    `archive_preimage`, `structured_rewrite`, `archive_dest_for`) — importing it
    or, for the substrate modules, defining it.

    Detect-only, and a STATIC heuristic: it reads call sites, not the paths they
    run against, so it cannot tell a filed note from a temp fixture (calls inside
    a `test`-named function are excluded for exactly that reason) and it cannot
    prove a bound route is the one guarding a given call. The finding names the
    limit so a reader triages rather than trusts.
    """
    scripts_dir = _KIT_ROOT / "scripts"
    candidates: list[Path] = []
    if scripts_dir.is_dir():
        candidates.extend(sorted(p for p in scripts_dir.glob("*.py")))
    candidates.append(Path(__file__).resolve())

    for script in candidates:
        try:
            source = script.read_text(encoding="utf-8")
            tree = ast.parse(source, filename=str(script))
        except (OSError, SyntaxError, ValueError) as exc:
            _log_detect("archive-first-bypass", _rel(script),
                        f"could not be parsed for the check: {exc}")
            continue
        if _module_bound_names(tree) & _ARCHIVE_FIRST_ROUTES:
            continue
        hits = _mutating_call_lines(tree)
        if not hits:
            continue
        kinds = sorted({call for _line, call in hits})
        _log_detect(
            "archive-first-bypass",
            _rel(script),
            f"{len(hits)} mutating call(s) {' '.join(kinds)} from line "
            f"{hits[0][0]}, with no archive_first import and no "
            f"structured_rewrite route (static check; call sites only, not the "
            f"paths they run against)",
        )


# ---------------------------------------------------------------------------
# Pass 22 — Merge residue (detect-only)
# ---------------------------------------------------------------------------

# A completed merge leaves the target rewritten, not appended to (CONFIG § Types,
# addendum). These are the traces of an appended merge.
_MERGE_MARKER_RE = re.compile(r"\(merged 20")
_PROPOSED_EDIT_RE = re.compile(r"^#{1,6}\s*Proposed edit\b", re.MULTILINE | re.IGNORECASE)
_TRIGGER_LINE_RE = re.compile(r"^\s*Trigger:", re.MULTILINE)
_H1_RE = re.compile(r"^# \S", re.MULTILINE)
# Sentence-initial first person: at the start of a line, or after a sentence end.
_FIRST_PERSON_RE = re.compile(r"(?:^|(?<=[.!?] ))(?:I |[Mm]y )", re.MULTILINE)
# The closing-fence line tolerates a CR. Vault text is read byte-exact (no
# universal-newline translation — the round-trip depends on `\r\n` surviving), so
# on a CRLF file the `\r` sits between the fence and the `$` and defeated
# `[ \t]*$`: no fence ever closed, no fenced block was ever blanked, and every
# `#` comment inside a shell example counted as an H1.
_FENCE_RE = re.compile(r"^[ \t]*(?:```|~~~).*?^[ \t]*(?:```|~~~)[ \t\r]*$",
                       re.MULTILINE | re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

# The types whose voice is first person by design. A session log and a journal
# are written by their author about their own work; a `source` is an external
# artifact preserved as captured (a transcript's speaker says "I"); a `log`
# records an actor's own actions. First person in any of them is the shape, not
# residue.
_FIRST_PERSON_TYPES = frozenset({"session", "journal", "source", "log"})


def _prose_only(body: str) -> str:
    """`body` with fenced blocks and inline code spans blanked out.

    A format note documents a shape by showing it — `Format-Addendum` carries a
    whole `type: addendum` skeleton, `## Proposed edit` heading included, inside
    a fence. Documenting the residue is not carrying it, so the lint reads prose
    only. Blanking (rather than deleting) keeps line offsets intact.
    """
    def blank(m: re.Match) -> str:
        return re.sub(r"[^\n]", " ", m.group(0))
    return _INLINE_CODE_RE.sub(blank, _FENCE_RE.sub(blank, body))


def pass22_merge_residue(all_md: list[Path]) -> None:
    """A live filed note carrying the traces of an appended merge files one
    `merge-residue` finding naming every trace it carries.

    Traces: a `(merged 20…)` stamp, a `## Proposed edit` heading, a `Trigger:`
    line in the body, a second H1, or sentence-initial first person in a type
    whose voice is not first person. A legal merge rewrites its target's rule
    text, so none of these survives it.

    Scope is live canon: `<archive>` and `<history>` copies are records, and
    `<inbox>` drafts are pre-merge by definition — an addendum awaiting review
    is SUPPOSED to carry `## Proposed edit`. Detect-only in every mode.
    """
    for p in all_md:
        if _is_preserved_copy(p):
            continue
        try:
            p.relative_to(INBOX_FOLDER)
            continue
        except ValueError:
            pass
        text = _read_text(p)
        if text is None:
            continue
        fm, body, err = _parse_frontmatter(text)
        if err or fm is None:
            continue
        ftype = normalize_type(str(fm.get("type") or "")) or ""
        prose = _prose_only(body)

        traces: list[str] = []
        if _MERGE_MARKER_RE.search(prose):
            traces.append("(merged 20… stamp")
        if _PROPOSED_EDIT_RE.search(prose):
            traces.append("'Proposed edit' heading")
        if _TRIGGER_LINE_RE.search(prose):
            traces.append("'Trigger:' line in the body")
        h1s = len(_H1_RE.findall(prose))
        if h1s > 1:
            traces.append(f"{h1s} H1 headings")
        if ftype not in _FIRST_PERSON_TYPES and _FIRST_PERSON_RE.search(prose):
            traces.append("sentence-initial first person")

        if traces:
            _log_detect("merge-residue", _rel(p), "; ".join(traces))


# ---------------------------------------------------------------------------
# Pass 23 — Reference provenance (detect-only)
# ---------------------------------------------------------------------------

_SOURCE_HEADING_RE = re.compile(
    r"^#{1,6}\s*(sources?|references?|provenance|citations?|origin)\b",
    re.MULTILINE | re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://\S")
# A version or date anchor: `v2`, `21.0.729`, `1.2`, or an ISO / spelled date.
_VERSION_ANCHOR_RE = re.compile(
    r"(?<![\w.])v?\d+\.\d+(?:\.\d+)*(?![\w.])"
    r"|(?<![\w-])\d{4}-\d{2}-\d{2}(?![\w-])"
    r"|(?<![\w-])(?:January|February|March|April|May|June|July|August|September"
    r"|October|November|December)\s+\d{4}(?![\w-])",
    re.IGNORECASE,
)


def _reference_domain(path: Path) -> str:
    """The domain folder a reference sits in — the first segment under the
    reference root, or the note's own parent folder name elsewhere."""
    try:
        rel = path.relative_to(VAULT_ROOT / _folder_by_semantic("reference"))
        return rel.parts[0] if len(rel.parts) > 1 else ""
    except ValueError:
        return path.parent.name


def pass23_provenance(all_md: list[Path]) -> None:
    """A live `type: reference` note that vouches for nothing files one
    `no-provenance` finding.

    A reference is canonical knowledge, so it says where the knowledge came
    from: a source/references heading, a URL, a `source:` frontmatter field, or
    the `inferred` tag (which declares a machine filled it in — a provenance
    claim of its own, CONFIG § Tags).

    A reference in a § Tags fast-moving domain carries one thing more: a version
    or date anchor in the body. Knowledge about a tool that ships monthly is
    unusable without saying WHICH release it describes, so a fast-moving
    reference with no anchor files the finding even when its source is named.
    Detect-only in every mode.
    """
    fast_moving = _fast_moving_domains()
    for p in all_md:
        if _is_preserved_copy(p):
            continue
        try:
            p.relative_to(INBOX_FOLDER)
            continue
        except ValueError:
            pass
        text = _read_text(p)
        if text is None:
            continue
        fm, body, err = _parse_frontmatter(text)
        if err or fm is None:
            continue
        if normalize_type(str(fm.get("type") or "")) != "reference":
            continue

        tags = [str(t).strip().casefold() for t in (fm.get("tags") or [])]
        has_source = bool(
            _SOURCE_HEADING_RE.search(body)
            or _URL_RE.search(body)
            or str(fm.get("source") or "").strip()
            or "inferred" in tags
        )
        if not has_source:
            _log_detect("no-provenance", _rel(p),
                        "no source heading, URL, source: field, or inferred tag")
            continue

        domain = _reference_domain(p).casefold()
        in_fast_moving = domain in fast_moving or any(t in fast_moving for t in tags)
        if in_fast_moving and not _VERSION_ANCHOR_RE.search(body):
            _log_detect(
                "no-provenance", _rel(p),
                f"fast-moving domain {_reference_domain(p)} with no version or "
                "date anchor in the body",
            )


# ---------------------------------------------------------------------------
# Pass 24 — Retired tokens (detect-only)
# ---------------------------------------------------------------------------

# Words that mark a line as talking ABOUT a retirement rather than instructing
# with the retired vocabulary. CONFIG § Deprecation expects the sweep to edit or
# queue the USES; a document recording that the token was retired is the record
# the retirement produced.
_RETIREMENT_CONTEXT_RE = re.compile(
    r"\b(retir(?:ed|es|ement)|deprecat\w*|supersed\w*|formerly|historical|"
    r"no longer|replaced by)\b",
    re.IGNORECASE,
)
# The immutable operational records: a session log and an append-only log say
# what was true when they were written and are never edited to match new canon.
_IMMUTABLE_RECORD_TYPES = frozenset({"session", "log"})
# Extensions counted as scripts for a row scoped to project `Assets/`.
_ASSET_SCRIPT_SUFFIXES = frozenset({".py", ".ps1", ".sh", ".bat", ".cmd"})


def _walk_asset_scripts() -> list[Path]:
    """Script files inside a project's `Assets/` tree.

    The ordinary walk prunes asset folders (CONFIG § Asset folders: their
    interior is never linted), yet § Retired tokens puts scripts inside project
    `Assets/` in scope for path tokens — a script still writing `01-Projects/` is
    exactly the straggler the retirement sweep is for. So this walk enters those
    folders and collects script files only, nothing else in them.
    """
    results: list[Path] = []
    if not PROJECTS_FOLDER.is_dir():
        return results
    for dirpath, dirnames, filenames in os.walk(PROJECTS_FOLDER):
        dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
        here = Path(dirpath)
        if not (ASSET_HOME_DIRS | {"Assets"}) & set(here.parts):
            continue
        for fname in filenames:
            if Path(fname).suffix.lower() in _ASSET_SCRIPT_SUFFIXES:
                results.append(here / fname)
    return results


def _retired_hits(text: str, matcher: re.Pattern, token: str,
                  stem: str) -> tuple[int, int]:
    """(count, first line) of non-descriptive hits for one matcher.

    A hit is skipped when its LINE reads as a record of the retirement, when the
    line cites a note whose own name carries the token, or when the file is
    named after it — the vault keeps the historical documents that discuss a
    retired mechanism, and naming one is not using it.
    """
    token_slug = re.sub(r"[^a-z0-9]+", "-", token.casefold()).strip("-")
    if token_slug and token_slug in re.sub(r"[^a-z0-9]+", "-", stem.casefold()):
        return 0, 0
    count = 0
    first = 0
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not matcher.search(line):
            continue
        if _RETIREMENT_CONTEXT_RE.search(line):
            continue
        if token_slug and re.search(
            rf"\[\[[^\]]*{re.escape(token_slug)}[^\]]*\]\]", line, re.IGNORECASE
        ):
            continue
        count += 1
        if not first:
            first = lineno
    return count, first


def pass24_retired_tokens(all_md: list[Path]) -> None:
    """Grep live docs for every § Retired tokens row and file one
    `retired-token` finding per document and token.

    CONFIG § Deprecation makes the table plus a blast-radius sweep the whole
    retirement event; this pass is the standing half of that sweep. Each row's
    SCOPE column decides where it is read: every row covers live documents, and a
    row scoped to `Assets` scripts is additionally read against script files
    inside project `Assets/` trees.

    Three exclusions keep the list to live uses. `<archive>` and `<history>`
    copies are preserved records. `type: session` and `type: log` notes are
    immutable — they record what was true when written. And a hit whose line
    reads as a record of the retirement (or cites a document named after the
    retired mechanism) is the retirement's own paper trail, not a straggler.
    Detect-only in every mode; the finding names the heuristic so a reader
    triages a descriptive mention the line-level check let through.
    """
    rows = _retired_token_rows()
    if not rows:
        return

    md_targets: list[tuple[Path, str]] = []
    for p in all_md:
        if _is_preserved_copy(p):
            continue
        text = _read_text(p)
        if text is None:
            continue
        fm, _body, err = _parse_frontmatter(text)
        if not err and fm:
            if normalize_type(str(fm.get("type") or "")) in _IMMUTABLE_RECORD_TYPES:
                continue
        md_targets.append((p, text))

    asset_targets: list[tuple[Path, str]] | None = None

    for row in rows:
        scope = row["scope"]
        targets = list(md_targets)
        if "asset" in scope:
            if asset_targets is None:
                asset_targets = []
                for p in _walk_asset_scripts():
                    t = _read_text(p)
                    if t is not None:
                        asset_targets.append((p, t))
            targets += asset_targets
        for token, matcher in _retired_matchers(row):
            for p, text in targets:
                count, first = _retired_hits(text, matcher, token, p.stem)
                if not count:
                    continue
                _log_detect(
                    "retired-token", _rel(p),
                    f"{token!r} retired {row['retired_date']} -> "
                    f"{row['replacement']}; {count} hit(s) from line {first} "
                    f"(descriptive mentions filtered by line context)",
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
        settings_text = _read_text(settings_path)
        if settings_text is None:
            _log_detect("hooks-settings-unparseable", _rel(settings_path),
                        "every hook in this file is dead: not valid UTF-8")
            continue
        try:
            data = json.loads(settings_text)
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
                    # Match BOTH the live absolute form
                    # `"$CLAUDE_PROJECT_DIR/.claude/..."` and the relative
                    # `./.claude/...` form; capture the `.claude/...` path and
                    # resolve it against the vault root. Without the absolute
                    # form the only detector for a rotted hook never runs against
                    # the registrations that actually exist (settings.json).
                    for _m in re.finditer(
                        r"(?:\$CLAUDE_PROJECT_DIR/|\./)(\.claude/[^\s\"']+)",
                        str(handler["command"]),
                    ):
                        script_rel = _m.group(1)
                        if not (VAULT_ROOT / script_rel).exists():
                            _log_detect("missing-hook-script", _rel(settings_path),
                                        f"{event}: command references {_m.group(0)} "
                                        "which does not exist")


# ---------------------------------------------------------------------------
# Pass 13 — Drift detection
# ---------------------------------------------------------------------------

def pass13_drift_detection() -> None:
    """Compare types in vault-state-index to normalize_type to find drift."""
    state_index = VAULT_ROOT / LOGS_REL / "Vault-State-Index.md"
    if not state_index.exists():
        return

    text = _read_text(state_index)
    if text is None:
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
    """Locate build_state_index, preferring a copy beside this file, then the
    installed scripts dir."""
    for cand in (
        _VAULT_JANITOR_DIR / "build_state_index.py",
        _KIT_ROOT / "scripts" / "build_state_index.py",
    ):
        if cand.exists():
            return cand
    return None


def _run_build_state_index(
    findings: list[tuple[str, str, str]] | None = None,
    baseline: Path | None = None,
) -> bool:
    """Refresh the single shared state snapshot via build_state_index.

    Returns True on success, False when the refresh could not run or failed — the
    caller persists the findings durably on a False so a refresh failure never
    vanishes the run's work list.

    Events live in the append-only per-agent ledger, not in the snapshot. When
    `findings` is given (the end-of-run call), they are written to a transient
    `code | target | value` file and passed through --findings so they merge into
    the snapshot's ## Open findings section. `baseline`, when given, is passed as
    --baseline: the change-evidence snapshot the end-of-run refresh compares
    against (the previous run's snapshot the janitor captured before its pre-pass
    overwrote it), so reviewed-stale and ## Lifecycle events land in the PERSISTED
    snapshot instead of only the transient pre-pass one.
    """
    build_script = _find_build_state_index()
    if build_script is None:
        return False
    cmd = [sys.executable, str(build_script)]
    tmp_findings: Path | None = None
    if findings:
        fd, tmp_name = tempfile.mkstemp(prefix="janitor-findings-", suffix=".txt")
        tmp_findings = Path(tmp_name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            for code, target, value in findings:
                fh.write(f"{code} | {target} | {value or '-'}\n")
        cmd += ["--findings", str(tmp_findings)]
    if baseline is not None:
        cmd += ["--baseline", str(baseline)]
    try:
        subprocess.run(
            cmd,
            env={**os.environ, "JANITOR_VAULT_ROOT": str(VAULT_ROOT)},
            check=True,
        )
        return True
    except subprocess.CalledProcessError as exc:
        _log_fix("(build-state-index)", f"build_state_index exited {exc.returncode}")
        return False
    except Exception as exc:
        _log_fix("(build-state-index)", f"build_state_index error: {exc}")
        return False
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
#
# `skipped` marks the action a pass declined to perform — `naming-rename-skipped`
# is the one such action, raised when a case-only rename has no recoverable
# pre-image and so does not happen. Without the marker it read as a completed
# mutation and flushed to the event ledger, telling the operator a rename had
# landed that the code had refused. Only the `action` string is matched, and only
# this one action carries `skipped`, so every other classification is unchanged
# (`subfolder-empty-subfolder` carries `skip-nonempty` in its VALUE, which
# `_is_event` never reads).
_FINDING_MARKERS = ("would", "collision", "failed", "mismatch", "error", "skipped")


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
            if rec.get("flushed"):
                continue  # already appended by _log_fix's per-action flush
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
        _append_ledger(event_lines)

    # Stage findings for the shared snapshot (build_state_index --findings).
    _RUN_FINDINGS.clear()
    _RUN_FINDINGS.extend(findings)


def _append_ledger(lines: list[str]) -> None:
    """Append pre-formatted `timestamp | actor | code | target | value` lines to
    the append-only event ledger, LF-only (never re-expanded to CRLF)."""
    log_dir.mkdir(parents=True, exist_ok=True)
    ledger_path = log_dir / f"{AGENT_NAME}.md"
    with open(ledger_path, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def _persist_findings_to_ledger(findings: list[tuple[str, str, str]]) -> None:
    """Durably record the run's findings to the append-only ledger when the final
    snapshot refresh FAILED. The snapshot is the only home for open findings, so
    a refresh failure would otherwise vanish the entire work list with no trace.
    A `snapshot-refresh-failed` marker plus one line per finding keeps the list
    recoverable (findings-persistence ordering — persist before the trace is lost,
    never after)."""
    write_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    lines = [
        f"{write_ts} | {AGENT_NAME} | snapshot-refresh-failed | (build-state-index) | "
        f"findings-preserved-below ({len(findings)})"
    ]
    for code, target, value in findings:
        lines.append(f"{write_ts} | {AGENT_NAME} | {code} | {target} | {_clean_value(value) or '-'}")
    _append_ledger(lines)


# ---------------------------------------------------------------------------
# Run-lease + baseline-snapshot helpers
# ---------------------------------------------------------------------------

def _lease_kwargs() -> dict:
    """Where RunLease takes its lease: an explicit JANITOR_LEASE_PATH override
    (the scratch-lease seam for tests), else this run's vault root so the lease
    lands in the vault's own `<logs>/run-lease.md`."""
    override = os.environ.get("JANITOR_LEASE_PATH")
    if override:
        return {"lease_path": Path(override)}
    return {"vault_root": VAULT_ROOT}


def _capture_baseline_snapshot() -> Path | None:
    """Copy the existing Vault-State-Index.md to a temp file BEFORE the pre-pass
    refresh overwrites it, so the persisted end-of-run refresh compares content
    hashes and the file list against the PREVIOUS run — not the seconds-old
    pre-pass write (double-snapshot fix). None on the first run (no prior
    snapshot)."""
    existing = VAULT_ROOT / LOGS_REL / "Vault-State-Index.md"
    if not existing.is_file():
        return None
    fd, tmp_name = tempfile.mkstemp(prefix="janitor-baseline-", suffix=".md")
    os.close(fd)
    tmp = Path(tmp_name)
    tmp.write_bytes(existing.read_bytes())
    return tmp


_CASE_RENAME_TMP_STEM_RE = re.compile(r"^(?P<stem>.+)\.case-rename\.tmp$")


def _recover_stranded_case_renames() -> None:
    """Recover a `<stem>.case-rename.tmp.md` left behind by the RETIRED two-step
    case-only rename (`path.rename(tmp)` then `tmp.rename(dest)`). Pass 6 now
    renames in one atomic `os.replace`, so no new tmp is ever created; this
    helper stays to clear a tmp stranded by an older run.
    The original was renamed to the tmp and the second step never ran, so the tmp
    holds the only copy. Roll it back to `<stem>.md` so the naming pass re-attempts
    the case fix cleanly this run. If both the tmp and the original are present
    (crash plus re-creation), archive the tmp pre-image — never delete (CONFIG
    § Versioning). Runs at the start of an --apply pass, before the file list is
    built."""
    for tmp in VAULT_ROOT.rglob("*.case-rename.tmp.md"):
        if any(is_excluded_dir(part) for part in tmp.parts):
            continue
        # A tmp an EARLIER run already archived keeps its name inside
        # `<archive>`. It is a preserved pre-image, not a stranded live file —
        # recovering it would rename archive content, which the never-touch rule
        # forbids and which would move the copy out from under the manifest line
        # that names it.
        if _is_preserved_copy(tmp):
            continue
        m = _CASE_RENAME_TMP_STEM_RE.match(tmp.stem)
        if not m:
            continue
        original = tmp.with_name(f"{m.group('stem')}.md")
        rel_tmp = _rel(tmp)
        if original.exists():
            try:
                # Archive-first (shared helper): copy onto a probed free name and
                # hash-verify BEFORE removing the tmp. The old `shutil.move` put
                # the tmp's only copy on an unprobed archive path, so a name
                # collision there overwrote an archive file.
                dest = archive_preimage(tmp, vault_root=VAULT_ROOT)
                tmp.unlink()
                _log_fix(rel_tmp, "case-rename-tmp-archived", f"-> {_rel(dest)}")
            except Exception as exc:
                _log_fix(rel_tmp, f"case-rename-recover-failed: {exc}")
        else:
            try:
                tmp.rename(original)
                _log_fix(rel_tmp, "case-rename-recovered", f"-> {_rel(original)}")
            except OSError as exc:
                _log_fix(rel_tmp, f"case-rename-recover-failed: {exc}")


# ---------------------------------------------------------------------------
# Main pass orchestrator
# ---------------------------------------------------------------------------

def run_audit() -> None:
    """Detect-only runs go straight through; an --apply run takes the vault-wide
    run lease first (CONFIG § Concurrency rule 1) and carries the pre-pass
    baseline snapshot through, releasing the lease and cleaning the baseline at
    exit even on exception."""
    if DRY_RUN:
        _audit_passes(baseline=None)
        return

    lease = RunLease(AGENT_NAME, "janitor --apply", **_lease_kwargs())
    try:
        lease.acquire()
    except LeaseHeldError as exc:
        # A fresh foreign lease is held — defer this pass. No work, exit clean.
        print(f"Run lease held by another agent — deferring this pass: {exc}",
              file=sys.stderr)
        return

    try:
        baseline = _capture_baseline_snapshot()
        try:
            _audit_passes(baseline=baseline)
        finally:
            # Runs on the crash path too: a pass that dies part-way still puts
            # back any file it case-renamed that has since vanished. The
            # 2026-07-26 19:22Z loss happened inside a run that later crashed,
            # so the sweep belongs here, not only at the clean end of the pass.
            try:
                _restore_lost_case_renames()
            except Exception as exc:   # never mask the original failure
                print(f"[case-rename] restore sweep failed: {exc}",
                      file=sys.stderr)
            if baseline is not None:
                try:
                    baseline.unlink()
                except OSError:
                    pass
    finally:
        lease.release()


def _audit_passes(baseline: Path | None) -> None:
    # Set up the per-run mechanical-rewrite bundle for --apply: one
    # `<archive>/<date>-audit-apply/` directory takes every frontmatter-rewrite
    # pre-image (created lazily on first archive, so a zero-rewrite run makes no
    # empty bundle). Detect-only leaves it None (no writes at all).
    global _APPLY_BUNDLE_DIR, _APPLY_BUNDLE_MANIFEST
    if not DRY_RUN:
        _bundle_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        _APPLY_BUNDLE_DIR = ARCHIVE_FOLDER / f"{_bundle_date}-audit-apply"
        _APPLY_BUNDLE_MANIFEST = _APPLY_BUNDLE_DIR / "manifest.md"

    # Run-liveness (Note-Kit-Structural-Integrity-Plan): the run's FIRST logged
    # action is a `run-start` ledger line, so build_state_index can tell a run
    # happened. An agent's cadence day passing with no run-start is what
    # `missed-cadence` catches — the janitor's 07-08 crash left zero log lines and
    # nothing alarmed. Detect-only writes nothing, so no run-start there.
    if not DRY_RUN:
        # The run-start value carries the active selector, so the ledger records
        # WHICH classes this pass was allowed to fix. Without it a run that
        # withheld a class is indistinguishable from one that found nothing.
        _append_ledger([
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')} | "
            f"{AGENT_NAME} | run-start | - | janitor --apply {_selector_label()}"
        ])

    # Root-folder revert (CONFIG § Folders): the check ran before the
    # kit-as-vault guard, so a reverted root is already back under its literal
    # and this run walks the corrected tree. Its records replay here, past
    # `run-start`, so the ledger reads in run order.
    _replay_root_folder_records()

    # Recover any case-rename temp stranded by a prior crash BEFORE the walk, so
    # the recovered file is included in this run's file list (--apply only).
    if not DRY_RUN:
        _recover_stranded_case_renames()

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

    print(f"Run mode: {'detect-only' if DRY_RUN else 'apply'}; "
          f"fix classes: {_selector_label()}", file=sys.stderr)

    print("Walking vault...", file=sys.stderr)
    all_md = _walk_vault()
    all_non_md = _walk_non_md()
    print(f"Found {len(all_md)} .md files, {len(all_non_md)} non-md files.", file=sys.stderr)

    # Build baseline indices before any mutations
    basename_index, inbound_index = _build_indices(all_md)

    print("Running per-file passes 1-12...", file=sys.stderr)

    for original_path in list(all_md):
        path = original_path

        # Read file. A prior pass (5/6/9) may have moved or renamed this path
        # mid-run; _read_text returns None for a vanished path (skip silently,
        # NOT a read error) and for a non-UTF-8 file (reported once). This ends
        # the spurious read-error rows every run that included a move emitted.
        text = _read_text(path)
        if text is None:
            continue

        # Pass 1 — YAML parse
        fm, body, parse_ok = pass1_yaml_parse(path, text)
        if not parse_ok:
            continue
        if fm is None:
            fm = {}

        changed = False

        # Pass 2 — Type normalization
        fm, ch = pass2_type_normalize(path, fm)
        changed = changed or ch

        # Pass 3 — Tag normalization
        fm, ch = pass3_tag_normalize(path, fm)
        changed = changed or ch

        # Pass 4 — Type inference (if still no type)
        fm, ch = pass4_type_inference(path, fm, body)
        changed = changed or ch

        # Write frontmatter if changed so far (frontmatter-only: body preserved).
        if changed and not DRY_RUN:
            _write_frontmatter(path, fm)
            changed = False

        # Pass 5 — Type-folder match (may move file)
        path, fm, moved = pass5_type_folder_match(path, fm, basename_index)
        if moved:
            # Rebuild indices after move
            basename_index, inbound_index = _build_indices(_walk_vault())
            changed = False

        # Pass 6 — Naming normalization (may rename file)
        path, renamed = pass6_naming(path, fm)
        if renamed:
            basename_index, inbound_index = _build_indices(_walk_vault())

        # Pass 7 — Parent inference (frontmatter-only: body preserved).
        fm, body, ch = pass7_parent_inference(path, fm, body, basename_index)
        if ch and not DRY_RUN:
            _write_frontmatter(path, fm)

        # Pass 8 — Index child registration
        pass8_index_children(path, fm, body, basename_index)

        # Pass 9 — Orphan detection
        pass9_orphan_detection(path, fm, body, basename_index, inbound_index)

        # Pass 12 — Body wikilink resolution (space->hyphen normalize, then queue)
        pass12_body_wikilinks(path, fm, body, basename_index)

        # Pass 14 — Flag an in-progress idea whose session has completed (agent archives)
        pass14_flag_completed_idea(path, fm, basename_index)

    # Pass 10 — Loose asset relocation (fixed snapshot lists, never a live walk)
    print("Pass 10: loose asset relocation...", file=sys.stderr)
    pass10_loose_assets(all_md, all_non_md)

    # Pass 11 — Missing project folder. Re-walk to pick up any moves above;
    # _walk_vault returns a materialized list, never a live iterator.
    print("Pass 11: missing project folders...", file=sys.stderr)
    for p in _walk_vault():
        text = _read_text(p)
        if text is None:
            continue
        fm, _, parse_ok = pass1_yaml_parse(p, text)
        if not parse_ok or not fm:
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

    # Pass 19 — Session word budget (detect-only; never auto-trims).
    print("Pass 19: session word budget...", file=sys.stderr)
    pass19_session_word_budget(all_md)

    # Pass 20 — Plan shape lint (detect-only; never auto-fixes).
    print("Pass 20: plan shape lint...", file=sys.stderr)
    pass20_plan_shape(all_md)

    # Pass 21 — Archive-first bypass in the kit's own scripts (static,
    # detect-only). CONFIG § Helper-script automation, archive_first row.
    print("Pass 21: archive-first bypass...", file=sys.stderr)
    pass21_archive_first_bypass()

    # Pass 22 — Merge residue in live canon (detect-only).
    print("Pass 22: merge residue...", file=sys.stderr)
    pass22_merge_residue(all_md)

    # Pass 23 — Reference provenance (detect-only).
    print("Pass 23: reference provenance...", file=sys.stderr)
    pass23_provenance(all_md)

    # Pass 24 — Retired-token sweep (detect-only; CONFIG § Deprecation).
    print("Pass 24: retired tokens...", file=sys.stderr)
    pass24_retired_tokens(all_md)

    # Subfolder housekeeping — prune empty subfolders and empty indexes on the
    # janitor cadence (CONFIG § Helper-script automation: "inline by audit.py
    # each janitor run"). Apply-gated; detect-only records the would-be actions.
    print("Subfolder housekeeping (prune empty subfolders/indexes)...", file=sys.stderr)
    _subfolder_apply = _apply_fixes("subfolder")
    try:
        for code, target, value in subfolder_housekeeping.sweep(
            VAULT_ROOT, apply=_subfolder_apply
        ):
            # A withheld class reports what it WOULD have done. The marker lands
            # in the code (never only the value) because the event/finding split
            # reads the code alone — a `subfolder-empty-index` line with an
            # unread caveat in its value would enter the ledger as a completed
            # archive that never happened.
            prefix = "subfolder-" if _subfolder_apply or DRY_RUN else "subfolder-would-"
            _log_fix(target, f"{prefix}{code}",
                     value if _subfolder_apply or DRY_RUN
                     else f"{_withhold_marker('subfolder')} {value}")
    except Exception as exc:
        _log_fix("(subfolder-housekeeping)", f"subfolder-housekeeping-failed: {exc}")

    # Verify this run's case-only renames survived the pass and restore any that
    # did not (both loss incidents removed exactly these files while the pass was
    # still running). Runs BEFORE _write_logs so a restore failure reaches the
    # snapshot as a finding; run_audit repeats it on the crash path, where it is
    # idempotent.
    if not DRY_RUN:
        print("Verifying case-only renames...", file=sys.stderr)
        _restore_lost_case_renames()

    # Append events to the ledger and stage this run's findings, then refresh
    # the single shared snapshot with those findings folded into ## Open findings.
    print("Writing event ledger + staging findings...", file=sys.stderr)
    _write_logs()

    if not DRY_RUN:
        print("Refreshing shared state snapshot (with findings)...", file=sys.stderr)
        # Findings-persistence ordering: the snapshot is the only durable home
        # for the run's open findings, so a refresh failure must not vanish them.
        # Persist the whole list to the append-only ledger when the refresh fails.
        # The baseline carries the previous run's change-evidence so reviewed-stale
        # and ## Lifecycle events land in this PERSISTED snapshot (double-snapshot fix).
        ok = _run_build_state_index(_RUN_FINDINGS, baseline=baseline)
        if not ok:
            _persist_findings_to_ledger(_RUN_FINDINGS)
        print(f"Ledger under: {log_dir}; snapshot at <logs>/Vault-State-Index.md",
              file=sys.stderr)
    else:
        # Detect-only writes NOTHING — print the findings to stdout instead so
        # the caller (the janitor agent or a person) still gets the work list.
        print("Detect-only: snapshot not refreshed; findings follow on stdout.",
              file=sys.stderr)
        for code, target, value in _RUN_FINDINGS:
            print(f"{code} | {target} | {value or '-'}")

    # Run boundary (CONFIG § Log files): a pass opens `run-start` and closes
    # `run-end`; a missing run-end marks the pass unrecorded. Emitted only
    # here, past every mutating step and the snapshot refresh, so a crash
    # anywhere earlier truthfully leaves the boundary open. Detect-only wrote
    # no run-start, so it writes no run-end either.
    if not DRY_RUN:
        _append_ledger([
            f"{datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')} | "
            f"{AGENT_NAME} | run-end | - | "
            f"{len(_auto_fixes_log)} actions, {len(_RUN_FINDINGS)} findings"
        ])
    print(
        f"Done - {len(_auto_fixes_log)} recorded actions/findings, "
        f"{len(_inference_rows)} inference items, "
        f"{len(_queue_candidate_rows)} queue candidates, "
        f"{len(_detect_log)} detections.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    run_audit()
