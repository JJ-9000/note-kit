"""
subfolder_housekeeping.py
==============================

Deterministic structural housekeeping the agents must not have to judge
(CONFIG § Subfolders): prune an empty subfolder and an empty
index. Creation of a needed subfolder or index is the filing/action agent's job
on filing to canon; this script only removes what has gone empty, so the vault
keeps no rigid pre-built tree and no stub folders.

Run:
    python subfolder_housekeeping.py --vault-root <vault>   # apply
    python subfolder_housekeeping.py --vault-root <vault> --dry-run

Invoked inline by audit.py each janitor run; manual invocation supported. Prints
one fixed-field line per action (CONFIG § Log files):
    timestamp | subfolder-housekeeping | <code> | <target> | <value>
with `timestamp` supplied by the caller (audit) or left as `-` on a manual run,
so the module never reads the clock itself.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import (  # noqa: E402
    FOLDER_ROUTING,
    is_excluded_dir,
    _folder_by_semantic,
)

# Roots and operational folders that are never pruned even when momentarily empty.
_PROTECTED_BASENAMES = {row.folder.split("/")[-1] for row in FOLDER_ROUTING.values()}


def _is_index(path: Path) -> bool:
    """A markdown file whose frontmatter declares `type: index`."""
    if path.suffix.lower() != ".md":
        return False
    try:
        head = path.read_text(encoding="utf-8")[:600]
    except OSError:
        return False
    return "\ntype: index" in ("\n" + head) or head.startswith("type: index")


def _index_links(path: Path) -> int:
    """Count `[[wikilink]]` child links in an index body (its reason to exist)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return 0
    return text.count("[[")


def _dir_is_empty(path: Path) -> bool:
    """True when a directory holds no files and no non-empty subdirectories."""
    for child in path.iterdir():
        if child.is_dir():
            if not _dir_is_empty(child):
                return False
        else:
            return False
    return True


def sweep(vault_root: Path, apply: bool) -> list[tuple[str, str, str]]:
    """Return a list of (code, target, value) actions; perform them when `apply`."""
    actions: list[tuple[str, str, str]] = []
    archive = vault_root / _folder_by_semantic("archive")
    history_names = {".history"}

    # Walk bottom-up so a parent emptied by pruning its last child is caught too.
    all_dirs = sorted(
        (p for p in vault_root.rglob("*") if p.is_dir()),
        key=lambda p: len(p.parts),
        reverse=True,
    )

    for d in all_dirs:
        # Skip excluded/cold/operational trees and the seven roots themselves.
        parts = d.relative_to(vault_root).parts
        if any(is_excluded_dir(seg) or seg in history_names for seg in parts):
            continue
        if archive in d.parents or d == archive:
            continue
        if len(parts) == 1 and d.name in _PROTECTED_BASENAMES:
            continue

        # An index alone in its folder (no siblings, no child links) indexes
        # nothing — remove it; the now-empty folder is caught on this same pass.
        children = list(d.iterdir())
        md_children = [c for c in children if c.is_file() and c.suffix.lower() == ".md"]
        if len(children) == 1 and md_children and _is_index(md_children[0]):
            if _index_links(md_children[0]) == 0:
                actions.append(("empty-index", str(md_children[0].relative_to(vault_root)), "removed"))
                if apply:
                    md_children[0].unlink()

        # An empty subfolder (now possibly emptied above) is pruned.
        if d.exists() and _dir_is_empty(d) and d.name not in _PROTECTED_BASENAMES:
            actions.append(("empty-subfolder", str(d.relative_to(vault_root)), "pruned"))
            if apply:
                try:
                    d.rmdir()
                except OSError:
                    # Non-empty after a race, or permission — skip, report on next run.
                    actions[-1] = ("empty-subfolder", str(d.relative_to(vault_root)), "skip-nonempty")

    return actions


def main() -> int:
    ap = argparse.ArgumentParser(description="Prune empty subfolders and empty indexes.")
    ap.add_argument("--vault-root", required=True, type=Path)
    ap.add_argument("--dry-run", action="store_true", help="detect only; make no changes")
    ns = ap.parse_args()

    vault_root = ns.vault_root.resolve()
    if not vault_root.is_dir():
        print(f"vault root not found: {vault_root}", file=sys.stderr)
        return 2

    actions = sweep(vault_root, apply=not ns.dry_run)
    for code, target, value in actions:
        print(f"- | subfolder-housekeeping | {code} | {target} | {value}")
    if not actions:
        print("- | subfolder-housekeeping | none | - | nothing to prune")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
