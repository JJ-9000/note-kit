#!/usr/bin/env python3
"""
rename_with_link_integrity.py
===================================

Rename a vault file while preserving every inbound wikilink. The original is
preserved through the new location (copy-before-delete); link references
across the vault are rewritten in one pass; the operation is idempotent
(re-running on a completed rename succeeds silently).

Resolution pattern:
    1. Validate: source exists, dest does not. If dest already exists and
       source is gone, return a no-op success (re-run on completed rename).
    2. Enumerate inbound wikilinks: glob `**/*.md` under `vault_root`, find
       every `[[<source-basename>]]`, `[[<source-basename>|alias]]`, or
       `[[<source-basename>#section|alias]]` outside the `<archive>` root and outside
       every dot-directory (CONFIG § Folders / Scan exclusions — `.claude`,
       `.obsidian`, `.trash`, …).
    3. Copy source -> dest (the new location IS the preserved copy — never
       move first, per the never-delete rule).
    4. Update every inbound wikilink to point to the new basename, keeping
       any `#section` anchor and `|alias` display text.
    5. Verify zero stragglers: re-grep for `[[<source-basename>]]` outside
       the `<archive>` root. Anything left aborts the operation.
    6. Remove the original source only after the destination is verified to
       exist with matching content.
    7. Append a log entry to `<archive>/Rename-Log.md` (source, dest, every
       updated file, replacement count).

Public API:
    rename_with_links(source, dest, vault_root, log_path=None) -> RenameResult
        -> status, source, dest, wikilink_updates, error

Run as a script to execute the integration tests against a temp fixture:
    python scripts/rename_with_link_integrity.py
"""
from __future__ import annotations

import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import _folder_by_semantic, is_excluded_dir  # noqa: E402
from wikilink_helpers import (  # noqa: E402
    WIKILINK_RE,
    normalize_link_target,
    extract_wikilinks,
    matches_basename,
    rewrite_wikilink_interior,
)


ARCHIVE_PREFIX = _folder_by_semantic("archive")
PROJECTS_PREFIX = _folder_by_semantic("projects")
DEFAULT_LOG_RELATIVE = f"{ARCHIVE_PREFIX}/Rename-Log.md"


@dataclass
class RenameResult:
    """Outcome of a rename pass.

    status: "renamed" (full rename ran), "no-op" (dest already in place),
            "aborted" (validation or straggler failure — no changes written).
    wikilink_updates: list of (path, replacement_count) for each touched file.
    """

    status: str
    source: Path
    dest: Path
    wikilink_updates: list[tuple[Path, int]] = field(default_factory=list)
    error: str | None = None


def _iter_vault_md(vault_root: Path) -> list[Path]:
    """All `.md` files under `vault_root` outside the `<archive>` root and outside any
    dot-directory.

    Dot-directories (CONFIG § Folders / Scan exclusions — the kit's own
    `.claude/` install dir, `.obsidian`, `.trash`, and any future dot-directory
    at any depth) are tooling/config, never vault content: a `.md` under one is
    skipped so the rename never reads or rewrites a kit/operational file.
    """
    archive = (vault_root / ARCHIVE_PREFIX).resolve()
    out: list[Path] = []
    for p in vault_root.rglob("*.md"):
        # Scan-exclusion: drop any path that descends through a dot-directory.
        if any(is_excluded_dir(part) for part in p.parts):
            continue
        try:
            p.resolve().relative_to(archive)
        except ValueError:
            out.append(p)
    return out


def _find_inbound(
    vault_root: Path, basename: str, exclude: Path
) -> list[tuple[Path, int]]:
    """Return (file, count) for every file whose body contains at least one
    wikilink that resolves to `basename` under Obsidian's rules.
    """
    exclude_resolved = exclude.resolve()
    hits: list[tuple[Path, int]] = []
    for md in _iter_vault_md(vault_root):
        if md.resolve() == exclude_resolved:
            continue
        text = md.read_text(encoding="utf-8")
        count = sum(
            1 for embed, interior in WIKILINK_RE.findall(text)
            if not embed and matches_basename(interior, basename)
        )
        if count:
            hits.append((md, count))
    return hits


def _rewrite_inbound(
    file: Path, old_basename: str, new_basename: str
) -> int:
    """Rewrite every wikilink that resolves to `old_basename` so its target
    becomes `new_basename`, preserving section anchors and aliases. Returns
    the count of substitutions made.
    """
    text = file.read_text(encoding="utf-8")
    new_text, count = rewrite_wikilink_interior(text, old_basename, new_basename)
    if new_text == text:
        return 0
    file.write_text(new_text, encoding="utf-8")
    return count


def _append_log(
    log_path: Path, result: RenameResult, vault_root: Path
) -> None:
    """Append a dated entry to the rename log."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        log_path.write_text(
            "# Rename Log\n\n"
            "Every rename done via `rename_with_link_integrity.py` lands here.\n",
            encoding="utf-8",
        )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        src_rel = result.source.resolve().relative_to(vault_root.resolve())
    except ValueError:
        src_rel = result.source
    try:
        dst_rel = result.dest.resolve().relative_to(vault_root.resolve())
    except ValueError:
        dst_rel = result.dest
    lines = [
        f"\n## {stamp} — {result.status}",
        f"- source: `{src_rel}`",
        f"- dest:   `{dst_rel}`",
    ]
    if result.wikilink_updates:
        lines.append(f"- updated wikilinks in {len(result.wikilink_updates)} file(s):")
        for path, count in result.wikilink_updates:
            try:
                rel = path.resolve().relative_to(vault_root.resolve())
            except ValueError:
                rel = path
            lines.append(f"    - `{rel}` ({count} replacement{'s' if count != 1 else ''})")
    else:
        lines.append("- no inbound wikilinks found")
    if result.error:
        lines.append(f"- error: {result.error}")
    with log_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def rename_with_links(
    source: Path,
    dest: Path,
    vault_root: Path,
    log_path: Path | None = None,
) -> RenameResult:
    """Rename `source` to `dest` and update every inbound wikilink.

    Idempotent: re-running after a successful rename detects `dest` already in
    place with `source` gone and returns a no-op.
    """
    source = Path(source)
    dest = Path(dest)
    vault_root = Path(vault_root)
    if log_path is None:
        log_path = vault_root / DEFAULT_LOG_RELATIVE
    log_path = Path(log_path)

    src_exists = source.exists()
    dst_exists = dest.exists()

    # --- 1. Validate -------------------------------------------------------
    if not src_exists and dst_exists:
        # Idempotent re-run: rename already complete.
        return RenameResult(status="no-op", source=source, dest=dest)
    if src_exists and dst_exists:
        return RenameResult(
            status="aborted",
            source=source,
            dest=dest,
            error=f"destination already exists: {dest}",
        )
    if not src_exists and not dst_exists:
        return RenameResult(
            status="aborted",
            source=source,
            dest=dest,
            error=f"source does not exist: {source}",
        )

    old_basename = source.stem
    new_basename = dest.stem

    # --- 2. Enumerate inbound wikilinks -----------------------------------
    inbound = _find_inbound(vault_root, old_basename, exclude=source)

    # --- 3. Copy source -> dest (the new location IS the preserved copy) --
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)

    # --- 4. Update every inbound wikilink ---------------------------------
    updates: list[tuple[Path, int]] = []
    for path, _ in inbound:
        count = _rewrite_inbound(path, old_basename, new_basename)
        if count > 0:
            updates.append((path, count))

    # --- 5. Verify zero stragglers ----------------------------------------
    stragglers = _find_inbound(vault_root, old_basename, exclude=source)
    if stragglers:
        # Roll back: remove the dest copy; the source is still in place.
        dest.unlink()
        return RenameResult(
            status="aborted",
            source=source,
            dest=dest,
            wikilink_updates=updates,
            error=(
                f"{len(stragglers)} wikilink straggler(s) after rewrite: "
                + ", ".join(str(p) for p, _ in stragglers[:3])
                + ("..." if len(stragglers) > 3 else "")
            ),
        )

    # --- 6. Remove the original (only after dest is verified) -------------
    if not dest.exists() or dest.stat().st_size != source.stat().st_size:
        return RenameResult(
            status="aborted",
            source=source,
            dest=dest,
            wikilink_updates=updates,
            error="destination verify failed before source deletion",
        )
    source.unlink()

    result = RenameResult(
        status="renamed",
        source=source,
        dest=dest,
        wikilink_updates=updates,
    )

    # --- 7. Log -----------------------------------------------------------
    _append_log(log_path, result, vault_root)
    return result


# ---- integration tests ------------------------------------------------------


def _run_integration_tests() -> None:
    """Spin up a temp vault, exercise rename + re-run + straggler-abort."""
    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        (vault / PROJECTS_PREFIX).mkdir()
        (vault / PROJECTS_PREFIX / "Other").mkdir()
        (vault / ARCHIVE_PREFIX).mkdir()
        # A dot-directory holding a kit file that references the source — the
        # rename must NOT descend into it (scan-exclusion).
        (vault / ".claude").mkdir()

        source = vault / PROJECTS_PREFIX / "Old-Name.md"
        source.write_text(
            "---\ntype: project\n---\n\n# Old Name\n\nbody\n",
            encoding="utf-8",
        )
        (vault / PROJECTS_PREFIX / "ref-bare.md").write_text(
            "See [[Old-Name]] for details.\n", encoding="utf-8"
        )
        (vault / PROJECTS_PREFIX / "ref-alias.md").write_text(
            "See [[Old-Name|the old project]] for context.\n", encoding="utf-8"
        )
        (vault / PROJECTS_PREFIX / "Other" / "ref-section.md").write_text(
            "Per [[Old-Name#Architecture|design]], the layout was foo.\n",
            encoding="utf-8",
        )
        # Path-prefixed reference (Obsidian disambiguation form).
        (vault / PROJECTS_PREFIX / "ref-path-prefix.md").write_text(
            f"See [[{PROJECTS_PREFIX}/Old-Name]] for full path form.\n",
            encoding="utf-8",
        )
        # .md-suffixed reference.
        (vault / PROJECTS_PREFIX / "ref-md-suffix.md").write_text(
            "See [[Old-Name.md]] with explicit extension.\n",
            encoding="utf-8",
        )
        # Path-prefixed + section anchor + alias (compound form).
        (vault / PROJECTS_PREFIX / "ref-path-section.md").write_text(
            f"Per [[{PROJECTS_PREFIX}/Old-Name#Architecture|the spec]], proceed.\n",
            encoding="utf-8",
        )
        # Archive reference should NOT be touched.
        (vault / ARCHIVE_PREFIX / "old-session.md").write_text(
            "Notes about [[Old-Name]] from earlier.\n", encoding="utf-8"
        )
        # Dot-directory (kit) reference must NOT be touched — it is config, not
        # vault content. Were the walk to descend here, this would be rewritten
        # (a kit-corruption bug) AND would register as a straggler that aborts.
        (vault / ".claude" / "kit-ref.md").write_text(
            "Kit doc mentioning [[Old-Name]].\n", encoding="utf-8"
        )

        dest = vault / PROJECTS_PREFIX / "New-Name.md"
        log = vault / ARCHIVE_PREFIX / "Rename-Log.md"

        # --- Case A: fresh rename ---------------------------------------
        result = rename_with_links(source, dest, vault, log)
        if result.status != "renamed":
            print(f"FAIL fresh rename status={result.status} error={result.error}")
            failures += 1
        if source.exists():
            print(f"FAIL source still present after rename: {source}")
            failures += 1
        if not dest.exists():
            print(f"FAIL dest missing after rename: {dest}")
            failures += 1
        if len(result.wikilink_updates) != 6:
            print(
                f"FAIL expected 6 wikilink updates "
                f"(bare/alias/section/path-prefix/md-suffix/path-section), got "
                f"{len(result.wikilink_updates)}"
            )
            failures += 1
        # Verify each ref file now points at New-Name (canonical form — path
        # prefix and .md suffix are dropped on rewrite since they were only
        # disambiguation for the now-renamed file).
        for name, expect in [
            ("ref-bare.md", "[[New-Name]]"),
            ("ref-alias.md", "[[New-Name|the old project]]"),
            ("Other/ref-section.md", "[[New-Name#Architecture|design]]"),
            ("ref-path-prefix.md", "[[New-Name]]"),
            ("ref-md-suffix.md", "[[New-Name]]"),
            ("ref-path-section.md", "[[New-Name#Architecture|the spec]]"),
        ]:
            content = (vault / PROJECTS_PREFIX / name).read_text(encoding="utf-8")
            if expect not in content:
                print(f"FAIL {name} missing {expect}")
                failures += 1
            if "[[Old-Name" in content or f"[[{PROJECTS_PREFIX}/Old-Name" in content:
                print(f"FAIL {name} still contains [[Old-Name... or [[{PROJECTS_PREFIX}/Old-Name...")
                failures += 1
        # Archive reference must remain untouched
        archive_content = (vault / ARCHIVE_PREFIX / "old-session.md").read_text(
            encoding="utf-8"
        )
        if "[[Old-Name]]" not in archive_content:
            print("FAIL archive reference was rewritten — must stay as-is")
            failures += 1
        # Dot-directory (kit) reference must remain untouched — the walk must
        # never have descended into `.claude/`.
        kit_content = (vault / ".claude" / "kit-ref.md").read_text(encoding="utf-8")
        if "[[Old-Name]]" not in kit_content:
            print("FAIL kit file under .claude/ was rewritten — scan-exclusion breached")
            failures += 1
        else:
            print("OK   case A+: dot-directory (.claude) reference left untouched")

        if log.exists() and "renamed" in log.read_text(encoding="utf-8"):
            print("OK   case A: fresh rename ran, log entry written")
        else:
            print("FAIL case A: log entry missing")
            failures += 1

        # --- Case B: idempotent re-run ----------------------------------
        log_before = log.read_text(encoding="utf-8")
        result2 = rename_with_links(source, dest, vault, log)
        if result2.status != "no-op":
            print(f"FAIL re-run status={result2.status} (expected no-op)")
            failures += 1
        log_after = log.read_text(encoding="utf-8")
        if log_after != log_before:
            print("FAIL re-run wrote to the log (should be silent)")
            failures += 1
        else:
            print("OK   case B: idempotent re-run produced no log entry")

        # --- Case C: missing source, missing dest -----------------------
        result3 = rename_with_links(
            vault / PROJECTS_PREFIX / "Nonexistent.md",
            vault / PROJECTS_PREFIX / "Whatever.md",
            vault,
            log,
        )
        if result3.status != "aborted":
            print(f"FAIL missing source status={result3.status} (expected aborted)")
            failures += 1
        else:
            print("OK   case C: missing source aborted as expected")

        # --- Case D: dest already exists, source still present ---------
        src_d = vault / PROJECTS_PREFIX / "D-Source.md"
        dst_d = vault / PROJECTS_PREFIX / "D-Dest.md"
        src_d.write_text("source\n", encoding="utf-8")
        dst_d.write_text("dest\n", encoding="utf-8")
        result4 = rename_with_links(src_d, dst_d, vault, log)
        if result4.status != "aborted":
            print(f"FAIL dest-collision status={result4.status} (expected aborted)")
            failures += 1
        else:
            print("OK   case D: dest-collision aborted as expected")
        # cleanup
        src_d.unlink()
        dst_d.unlink()

    if failures:
        print(f"\n{failures} integration test failure(s).")
        sys.exit(1)
    print("\nAll integration tests passed.")


if __name__ == "__main__":
    _run_integration_tests()
