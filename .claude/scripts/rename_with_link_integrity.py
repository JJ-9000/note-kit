#!/usr/bin/env python3
"""
rename_with_link_integrity.py
===================================

Rename or relocate a vault file while preserving every inbound wikilink. The
original is preserved through the new location (copy-before-delete); link
references across the vault are rewritten in one pass; the operation is
idempotent (re-running on a completed rename succeeds silently) and it never
leaves a link dangling — a mid-run abort restores every file it touched.

Resolution pattern:
    1. Validate: source exists, dest does not. If dest already exists and
       source is gone, return a no-op success (re-run on completed rename).
    2. Enumerate inbound wikilinks: glob `**/*.md` under `vault_root`, find
       every `[[<source-basename>]]`, `[[<source-basename>|alias]]`, or
       `[[<source-basename>#section|alias]]` outside the `<archive>` root and outside
       every dot-directory (CONFIG § Folders / Scan exclusions — `.claude`,
       `.obsidian`, `.trash`, …). A file that is not valid UTF-8 is skipped
       and reported on the result, never decoded lossily and written back.
    3. Copy source -> dest (the new location IS the preserved copy — never
       move first, per the never-delete rule).
    4. Update every inbound wikilink that resolves to the SOURCE — a plain
       `[[basename]]` link or a path-qualified link at the source's old location
       — to point to the new basename, keeping any `#section` anchor and `|alias`.
       A path-qualified link to a DIFFERENT note of the same basename (a homonym
       on another path) is left byte-identical, never redirected. Each rewritten
       file's pre-rewrite text is held in memory so a later abort restores it.
    5. Verify zero stragglers: re-scan for any surviving link that resolves to
       the source. On a basename change a surviving plain `[[old]]` would
       misresolve, so it counts; on a same-basename relocate a plain `[[old]]`
       still resolves to the moved file, so only a surviving source-pointing
       path link counts. A homonym on another path is excluded on both. On any
       straggler, restore every rewritten inbound file to its held text (verified
       byte-identical) and remove the dest copy, so nothing dangles.
    6. Remove the original source only after the destination is verified by
       content hash to match the source. A hash mismatch rolls back exactly as
       a straggler does.
    7. Append a log entry to `<archive>/Rename-Log.md` — for a completed rename
       and for an aborted run alike (source, dest, every updated file, or the
       abort reason and the roll-back result).

Archive-first ruling (recorded 2026-07-26)
------------------------------------------
The MOVE itself needs no separate pre-image. Steps 3, 6, and 7 already are
archive-first: the destination copy is written before anything is removed, the
copy is verified against the source by content hash, the source is deleted only
after that verification passes, and the `<archive>/Rename-Log.md` entry records
the operation. The destination IS the preserved copy, so a second copy in
`<archive>` would duplicate a file that never stopped existing.

The inbound-link rewrites in step 4 are a different act: they OVERWRITE filed
notes that are not the moved file, and their only rollback was the pre-rewrite
text held in memory — a copy that dies with the process. Each of those files
therefore takes a verified pre-image through `archive_first.archive_preimage`
before its first write (CONFIG § Helper-script automation). A pre-image that
cannot be written aborts the rename through the ordinary rollback path, so a
link rewrite never lands with nothing behind it. An `<inbox>` draft is the
user's working space and takes no pre-image (`archive_first.is_filed`).

Public API:
    rename_with_links(source, dest, vault_root, log_path=None) -> RenameResult
        -> status, source, dest, wikilink_updates, error

Run as a script to execute the integration tests against a temp fixture:
    python scripts/rename_with_link_integrity.py
"""
from __future__ import annotations

import hashlib
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from archive_first import ArchiveFirstError, archive_preimage, is_filed  # noqa: E402
from config_variables import _folder_by_semantic, is_excluded_dir  # noqa: E402
from frontmatter_helpers import (  # noqa: E402
    read_text_or_none,
    read_text_strict,
    write_text,
)
from wikilink_helpers import (  # noqa: E402
    WIKILINK_RE,
    matches_basename,
)


ARCHIVE_PREFIX = _folder_by_semantic("archive")
PROJECTS_PREFIX = _folder_by_semantic("projects")
DEFAULT_LOG_RELATIVE = f"{ARCHIVE_PREFIX}/Rename-Log.md"


@dataclass
class RenameResult:
    """Outcome of a rename pass.

    status: "renamed" (full rename ran), "no-op" (dest already in place),
            "aborted" (validation, straggler, or dest-verify failure — every
            change made during the run is rolled back before the abort returns).
    wikilink_updates: list of (path, replacement_count) for each touched file;
            empty on an aborted run, whose rewrites were rolled back.
    rolled_back: inbound files restored to their pre-run text on an abort.
    restore_failures: any rolled-back file whose on-disk text did not match the
            held pre-run text after restore (should always be empty).
    """

    status: str
    source: Path
    dest: Path
    wikilink_updates: list[tuple[Path, int]] = field(default_factory=list)
    error: str | None = None
    unreadable_files: list[Path] = field(default_factory=list)
    rolled_back: list[Path] = field(default_factory=list)
    restore_failures: list[Path] = field(default_factory=list)


def _sha256(path: Path) -> str:
    """Content hash of a file — the dest-vs-source verify the copy comment claims."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


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


def _target_of(interior: str) -> str:
    """The link-target portion of a wikilink interior — before any `|alias` and
    `#anchor`. `Folder/Name#Sec|Alias` -> `Folder/Name`."""
    return interior.split("|", 1)[0].split("#", 1)[0]


def _link_points_to_source(interior_target: str, source_rel_stem: str) -> bool:
    """True when a PATH-qualified link target resolves to the source's OLD
    vault-relative location.

    Obsidian resolves a path-qualified link as a path *suffix* of a vault file, so
    `[[Pkg/Name]]`, `[[Projects/Pkg/Name]]`, and `[[Projects/Pkg/Name.md]]` all
    resolve to `Projects/Pkg/Name.md`. This returns True only for such a link,
    never for a bare basename (a plain link, handled separately) and never for a
    link naming a DIFFERENT path — a homonym `[[Projects/Other/Name]]` when the
    source is `Projects/Pkg/Name.md` is left alone, not rewritten.
    """
    t = interior_target.strip().replace("\\", "/").strip("/")
    if t.lower().endswith(".md"):
        t = t[:-3]
    if "/" not in t:
        return False  # bare basename — a plain link, not a path-qualified one
    src = source_rel_stem.replace("\\", "/").strip("/")
    return src == t or src.endswith("/" + t)


def _rewrite_inbound_links(
    text: str,
    old_basename: str,
    new_basename: str,
    source_rel_stem: str,
    *,
    rewrite_embeds: bool,
) -> tuple[str, int]:
    """Rewrite inbound wikilinks that resolve to the SOURCE to `new_basename`,
    preserving any `#section` and `|alias` and dropping the now-stale path.

    A link resolves to the source when it is plain (`[[old]]`) or path-qualified
    at the source's old location. A path-qualified link that resolves to a
    DIFFERENT note of the same basename — a homonym on another path — is left
    byte-identical, on both the basename-CHANGE and the same-basename RELOCATE
    path (it points at a note that did not move or rename). On a same-basename
    relocate `new_basename == old_basename`, so a plain link rebuilds to itself
    (no change) and only a source-pointing path-qualified link collapses.

    ``rewrite_embeds`` controls whether `![[...]]` embeds are rewritten: the
    basename-change path rewrites them (preserving the `!`); the same-basename
    relocate path leaves them (a parked gap). Returns (new_text, count), where
    count is the number of links whose text actually changed.
    """
    count = 0

    def _replace(m: "object") -> str:
        nonlocal count
        embed, interior = m.group(1), m.group(2)
        if not matches_basename(interior, old_basename):
            return m.group(0)
        if embed and not rewrite_embeds:
            return m.group(0)
        target = _target_of(interior)
        if "/" in target and not _link_points_to_source(target, source_rel_stem):
            return m.group(0)  # homonym on another path — leave byte-identical
        if "|" in interior:
            before_alias, alias = interior.split("|", 1)
            alias_part = f"|{alias}"
        else:
            before_alias, alias_part = interior, ""
        section_part = f"#{before_alias.split('#', 1)[1]}" if "#" in before_alias else ""
        new_form = f"{embed}[[{new_basename}{section_part}{alias_part}]]"
        if new_form != m.group(0):
            count += 1
        return new_form

    return WIKILINK_RE.sub(_replace, text), count


def _find_inbound(
    vault_root: Path,
    basename: str,
    exclude: Path,
    unreadable: list[Path] | None = None,
) -> list[tuple[Path, int]]:
    """Return (file, count) for every file whose body contains at least one
    wikilink that resolves to `basename` under Obsidian's rules.

    A file that is not valid UTF-8 is skipped and appended to `unreadable`.
    """
    exclude_resolved = exclude.resolve()
    hits: list[tuple[Path, int]] = []
    for md in _iter_vault_md(vault_root):
        if md.resolve() == exclude_resolved:
            continue
        text = read_text_or_none(md)
        if text is None:
            if unreadable is not None:
                unreadable.append(md)
            continue
        count = sum(
            1 for embed, interior in WIKILINK_RE.findall(text)
            if not embed and matches_basename(interior, basename)
        )
        if count:
            hits.append((md, count))
    return hits


def _find_straggler_inbound(
    vault_root: Path,
    old_basename: str,
    exclude: Path,
    source_rel_stem: str,
    *,
    count_plain: bool,
) -> list[tuple[Path, int]]:
    """Inbound files still holding a wikilink that resolves to the SOURCE after
    the rewrite pass — a link that would dangle or misresolve.

    A link resolves to the source when it is plain (`[[old]]`) or path-qualified
    at the source's old location; a homonym on another path is excluded on both
    paths (it points at a note that did not move or rename). ``count_plain`` is
    True on a basename CHANGE — a surviving plain `[[old]]` should have become the
    new name, so it is a straggler — and False on a same-basename RELOCATE, where
    a plain `[[old]]` still resolves to the moved file by basename. Step 4
    rewrites every source-resolving link, so a healthy pass finds none.
    """
    exclude_resolved = exclude.resolve()
    hits: list[tuple[Path, int]] = []
    for md in _iter_vault_md(vault_root):
        if md.resolve() == exclude_resolved:
            continue
        text = read_text_or_none(md)
        if text is None:
            continue
        count = 0
        for embed, interior in WIKILINK_RE.findall(text):
            if embed or not matches_basename(interior, old_basename):
                continue
            target = _target_of(interior)
            if "/" in target:
                if _link_points_to_source(target, source_rel_stem):
                    count += 1  # a surviving source-pointing path link
            elif count_plain:
                count += 1  # a surviving plain link on a basename change
        if count:
            hits.append((md, count))
    return hits


def _restore_rewritten(rewritten: dict[Path, str]) -> list[Path]:
    """Restore each rewritten inbound file to its held pre-rewrite text.

    Exception-safe: a write or read failure on one file is caught and that file
    is recorded as a restore failure, so the loop always restores every other
    file and never propagates mid-restore (which would strand later files with no
    log line). Returns the paths whose on-disk text does not match the held text
    (or could not be written) after the restore.
    """
    failures: list[Path] = []
    for path, before in rewritten.items():
        try:
            write_text(path, before)
            if read_text_strict(path) != before:
                failures.append(path)
        except OSError:
            failures.append(path)
    return failures


def _rollback_abort(
    source: Path,
    dest: Path,
    vault_root: Path,
    log_path: Path,
    *,
    rewritten: dict[Path, str],
    unreadable: list[Path],
    error: str,
) -> RenameResult:
    """Restore every rewritten inbound file, remove the dest copy, and ALWAYS
    write a Rename-Log line.

    The log write is in a ``finally`` so a failure while removing the dest copy
    never suppresses the ledger entry, and the entry records exactly which files
    restored and which did not — the log tells the truth about partial state.
    """
    restore_failures = _restore_rewritten(rewritten)
    result = RenameResult(
        status="aborted",
        source=source,
        dest=dest,
        unreadable_files=unreadable,
        rolled_back=list(rewritten.keys()),
        restore_failures=restore_failures,
        error=error,
    )
    try:
        if dest.exists():
            try:
                dest.unlink()
            except OSError as exc:
                result.error = f"{error}; dest copy removal failed: {exc}"
    finally:
        _append_log(log_path, result, vault_root)
    return result


def _append_log(
    log_path: Path, result: RenameResult, vault_root: Path
) -> None:
    """Append a dated entry to the rename log (completed and aborted runs alike)."""
    log_path = Path(log_path)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        write_text(
            log_path,
            "# Rename Log\n\n"
            "Every rename done via `rename_with_link_integrity.py` lands here.\n",
        )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _rel(p: Path) -> Path:
        try:
            return p.resolve().relative_to(vault_root.resolve())
        except ValueError:
            return p

    lines = [
        f"\n## {stamp} — {result.status}",
        f"- source: `{_rel(result.source)}`",
        f"- dest:   `{_rel(result.dest)}`",
    ]
    if result.wikilink_updates:
        lines.append(f"- updated wikilinks in {len(result.wikilink_updates)} file(s):")
        for path, count in result.wikilink_updates:
            lines.append(
                f"    - `{_rel(path)}` ({count} replacement{'s' if count != 1 else ''})"
            )
    elif result.status == "renamed":
        lines.append("- no inbound wikilinks found")
    if result.unreadable_files:
        lines.append(
            f"- skipped {len(result.unreadable_files)} file(s) that are not valid "
            f"UTF-8 — any inbound link inside them is unrewritten:"
        )
        for path in result.unreadable_files:
            lines.append(f"    - `{_rel(path)}`")
    if result.rolled_back:
        if result.restore_failures:
            lines.append(
                f"- rolled back {len(result.rolled_back)} rewritten file(s); "
                f"{len(result.restore_failures)} FAILED to restore byte-identical:"
            )
            for path in result.restore_failures:
                lines.append(f"    - `{_rel(path)}`")
        else:
            lines.append(
                f"- rolled back {len(result.rolled_back)} rewritten file(s) to "
                f"pre-run state (all byte-identical)"
            )
    if result.error:
        lines.append(f"- error: {result.error}")
    with open(log_path, "a", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")


def rename_with_links(
    source: Path,
    dest: Path,
    vault_root: Path,
    log_path: Path | None = None,
    *,
    _after_copy_hook: Optional[Callable[[], None]] = None,
    _after_rewrite_hook: Optional[Callable[[], None]] = None,
) -> RenameResult:
    """Rename or relocate `source` to `dest` and update every inbound wikilink.

    Idempotent: re-running after a successful rename detects `dest` already in
    place with `source` gone and returns a no-op.

    The two `_after_*_hook` seams are test-only injection points (mid-run
    corruption / straggler injection) exercised by the integration suite; they
    default to None and callers never pass them.
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
        # Idempotent re-run: rename already complete. Silent — no log entry.
        return RenameResult(status="no-op", source=source, dest=dest)
    if src_exists and dst_exists:
        result = RenameResult(
            status="aborted",
            source=source,
            dest=dest,
            error=f"destination already exists: {dest}",
        )
        _append_log(log_path, result, vault_root)
        return result
    if not src_exists and not dst_exists:
        result = RenameResult(
            status="aborted",
            source=source,
            dest=dest,
            error=f"source does not exist: {source}",
        )
        _append_log(log_path, result, vault_root)
        return result

    old_basename = source.stem
    new_basename = dest.stem
    same_basename = old_basename == new_basename
    # The source's OLD vault-relative path stem, used to tell a link that points
    # at the moved file from a homonym on another path (same-basename relocate).
    try:
        source_rel_stem = str(
            source.resolve().relative_to(vault_root.resolve()).with_suffix("")
        ).replace("\\", "/")
    except ValueError:
        source_rel_stem = old_basename

    # --- 2. Enumerate inbound wikilinks -----------------------------------
    unreadable: list[Path] = []
    inbound = _find_inbound(
        vault_root, old_basename, exclude=source, unreadable=unreadable
    )

    # --- 3. Copy source -> dest (the new location IS the preserved copy) --
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    if _after_copy_hook is not None:
        _after_copy_hook()

    # --- 4. Update every inbound wikilink; hold pre-rewrite text for rollback
    updates: list[tuple[Path, int]] = []
    rewritten: dict[Path, str] = {}  # path -> pre-rewrite text
    for path, _ in inbound:
        before = read_text_strict(path)
        # Both branches resolve a path-qualified link against the source's old
        # location: a homonym of the same basename on ANOTHER path is left
        # byte-identical, never redirected to the renamed/moved file.
        after, count = _rewrite_inbound_links(
            before, old_basename, new_basename, source_rel_stem,
            rewrite_embeds=not same_basename,
        )
        if after != before:
            # Archive-first: this is an overwrite of a FILED note that is not the
            # moved file, so its pre-rewrite bytes go to `<archive>` before the
            # write. Without it the only rollback was the in-memory `before`,
            # which a crash takes with it.
            if is_filed(path, vault_root=vault_root):
                try:
                    archive_preimage(path, vault_root=vault_root)
                except ArchiveFirstError as exc:
                    return _rollback_abort(
                        source, dest, vault_root, log_path,
                        rewritten=rewritten,
                        unreadable=unreadable,
                        error=(
                            f"archive-first pre-image failed for inbound file "
                            f"{path}: {exc}"
                        ),
                    )
            rewritten[path] = before
            write_text(path, after)
            updates.append((path, count))

    if _after_rewrite_hook is not None:
        _after_rewrite_hook()

    # --- 5. Verify zero stragglers ----------------------------------------
    stragglers = _find_straggler_inbound(
        vault_root, old_basename, exclude=source,
        source_rel_stem=source_rel_stem, count_plain=not same_basename,
    )
    if stragglers:
        # Roll back every rewritten inbound file, remove the dest copy, log.
        return _rollback_abort(
            source, dest, vault_root, log_path,
            rewritten=rewritten,
            unreadable=unreadable,
            error=(
                f"{len(stragglers)} wikilink straggler(s) after rewrite: "
                + ", ".join(str(p) for p, _ in stragglers[:3])
                + ("..." if len(stragglers) > 3 else "")
            ),
        )

    # --- 6. Verify dest by content hash, then remove the original ---------
    if not dest.exists() or _sha256(dest) != _sha256(source):
        return _rollback_abort(
            source, dest, vault_root, log_path,
            rewritten=rewritten,
            unreadable=unreadable,
            error="destination verify (content hash) failed before source deletion",
        )
    source.unlink()

    result = RenameResult(
        status="renamed",
        source=source,
        dest=dest,
        wikilink_updates=updates,
        unreadable_files=unreadable,
    )

    # --- 7. Log -----------------------------------------------------------
    _append_log(log_path, result, vault_root)
    return result


# ---- integration tests ------------------------------------------------------


def _run_integration_tests() -> None:
    """Spin up a temp vault, exercise rename + re-run + straggler-abort +
    same-basename relocate + forced-abort rollback + dest hash-verify."""
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
        # A Windows-1252 file (0x92 = curly apostrophe) is not valid UTF-8. A
        # strict read of it aborts the whole rename — this crashed janitor runs
        # #61 and #62. It must be skipped and reported, its bytes preserved.
        cp1252 = vault / PROJECTS_PREFIX / "cp1252-note.md"
        cp1252.write_bytes("It’s a note, no links here.\n".encode("cp1252"))
        cp1252_before = cp1252.read_bytes()

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

        # The non-UTF-8 file must have been skipped, reported, and left byte-identical.
        if cp1252.read_bytes() != cp1252_before:
            print("FAIL non-UTF-8 file was rewritten — its bytes must be preserved")
            failures += 1
        if cp1252 not in result.unreadable_files:
            print("FAIL non-UTF-8 file was not reported on result.unreadable_files")
            failures += 1
        else:
            print("OK   case A+: non-UTF-8 file skipped, reported, bytes intact")

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

        # --- Case E: same-basename relocate (folder-to-folder move) -----
        # The reviewed-reset move clause and the CONFIG § Actions relocate path
        # both keep the basename; a plain [[basename]] link must survive and a
        # path-qualified link to the old folder must be rewritten.
        (vault / PROJECTS_PREFIX / "Pkg").mkdir()
        (vault / PROJECTS_PREFIX / "Relocated").mkdir()
        e_src = vault / PROJECTS_PREFIX / "Pkg" / "Methodology.md"
        e_src.write_text(
            "---\ntype: reference\n---\n\n# Methodology\n\nbody\n", encoding="utf-8"
        )
        e_dst = vault / PROJECTS_PREFIX / "Relocated" / "Methodology.md"
        e_plain = vault / PROJECTS_PREFIX / "e-plain.md"
        e_plain.write_text("Uses [[Methodology]] plainly.\n", encoding="utf-8")
        e_plain_before = e_plain.read_bytes()
        e_pathq = vault / PROJECTS_PREFIX / "e-pathq.md"
        e_pathq.write_text(
            f"Uses [[{PROJECTS_PREFIX}/Pkg/Methodology|the method]] via path.\n",
            encoding="utf-8",
        )
        res_e = rename_with_links(e_src, e_dst, vault, log)
        if res_e.status != "renamed":
            print(f"FAIL E same-basename relocate status={res_e.status} error={res_e.error}")
            failures += 1
        if e_src.exists() or not e_dst.exists():
            print("FAIL E relocate did not move the file")
            failures += 1
        # Plain [[Methodology]] link is left byte-identical (still resolves).
        if e_plain.read_bytes() != e_plain_before:
            print("FAIL E plain [[Methodology]] link was rewritten (must stay as-is)")
            failures += 1
        # Path-qualified link is collapsed to the plain form.
        e_pathq_after = e_pathq.read_text(encoding="utf-8")
        if "[[Methodology|the method]]" not in e_pathq_after or f"{PROJECTS_PREFIX}/Pkg/Methodology" in e_pathq_after:
            print(f"FAIL E path-qualified link not rewritten: {e_pathq_after!r}")
            failures += 1
        if "renamed" not in log.read_text(encoding="utf-8"):
            print("FAIL E relocate not recorded in the Rename-Log")
            failures += 1
        if res_e.status == "renamed" and e_plain.read_bytes() == e_plain_before:
            print("OK   case E: same-basename relocate — plain link kept, path link rewritten, logged")

        # --- Case F: forced mid-run straggler abort restores every file --
        f_src = vault / PROJECTS_PREFIX / "Old-Name-F.md"
        f_src.write_text("---\ntype: project\n---\n\n# F\n\nbody\n", encoding="utf-8")
        f_dst = vault / PROJECTS_PREFIX / "New-Name-F.md"
        f_ref1 = vault / PROJECTS_PREFIX / "f-ref1.md"
        f_ref1.write_text("Link [[Old-Name-F]] one.\n", encoding="utf-8")
        f_ref2 = vault / PROJECTS_PREFIX / "Other" / "f-ref2.md"
        f_ref2.write_text("Link [[Old-Name-F|alias]] two.\n", encoding="utf-8")
        f_ref1_before = f_ref1.read_bytes()
        f_ref2_before = f_ref2.read_bytes()
        f_injected = vault / PROJECTS_PREFIX / "f-injected-straggler.md"

        def _inject_straggler() -> None:
            # Simulate a link the rewrite pass could not reach (e.g. a file
            # written by another process mid-run): a fresh [[Old-Name-F]] appears
            # after step 4, so the straggler scan trips and forces a mid-run abort.
            f_injected.write_text("Sneaky [[Old-Name-F]] straggler.\n", encoding="utf-8")

        res_f = rename_with_links(
            f_src, f_dst, vault, log, _after_rewrite_hook=_inject_straggler
        )
        if res_f.status != "aborted":
            print(f"FAIL F forced-abort status={res_f.status} (expected aborted)")
            failures += 1
        if not f_src.exists():
            print("FAIL F source removed despite abort")
            failures += 1
        if f_dst.exists():
            print("FAIL F dest copy left behind after abort")
            failures += 1
        if f_ref1.read_bytes() != f_ref1_before or f_ref2.read_bytes() != f_ref2_before:
            print("FAIL F rewritten inbound files not restored byte-identical")
            failures += 1
        if res_f.restore_failures:
            print(f"FAIL F restore reported failures: {res_f.restore_failures}")
            failures += 1
        if not res_f.rolled_back:
            print("FAIL F abort did not record rolled-back files")
            failures += 1
        # The abort must leave a log line.
        if log.read_text(encoding="utf-8").count("aborted") < 1:
            print("FAIL F abort did not write a log line")
            failures += 1
        if (
            res_f.status == "aborted"
            and f_src.exists()
            and not f_dst.exists()
            and f_ref1.read_bytes() == f_ref1_before
            and f_ref2.read_bytes() == f_ref2_before
            and not res_f.restore_failures
        ):
            print("OK   case F: forced mid-run abort restored every file byte-identical + logged")

        # --- Case G: dest hash-verify catches same-size corruption ------
        g_src = vault / PROJECTS_PREFIX / "Old-Name-G.md"
        g_src.write_text("---\ntype: project\n---\n\n# G\n\nbody\n", encoding="utf-8")
        g_dst = vault / PROJECTS_PREFIX / "New-Name-G.md"
        g_ref = vault / PROJECTS_PREFIX / "g-ref.md"
        g_ref.write_text("Link [[Old-Name-G]] here.\n", encoding="utf-8")
        g_ref_before = g_ref.read_bytes()

        def _corrupt_dest_same_size() -> None:
            # Overwrite the dest copy with same-length but different bytes: the
            # size check the old code used would pass; the hash check must fail.
            n = g_src.stat().st_size
            g_dst.write_bytes(b"Z" * n)

        res_g = rename_with_links(
            g_src, g_dst, vault, log, _after_copy_hook=_corrupt_dest_same_size
        )
        if res_g.status != "aborted":
            print(f"FAIL G same-size corruption status={res_g.status} (expected aborted)")
            failures += 1
        if not g_src.exists():
            print("FAIL G source removed despite corrupt dest")
            failures += 1
        if g_ref.read_bytes() != g_ref_before:
            print("FAIL G inbound file not restored after dest-verify abort")
            failures += 1
        if res_g.status == "aborted" and g_src.exists() and g_ref.read_bytes() == g_ref_before:
            print("OK   case G: same-size dest corruption caught by hash verify, rolled back")

        # --- Case H: same-basename relocate with a HOMONYM (M1) ----------
        # A path-qualified link to a DIFFERENT file of the same basename must
        # survive byte-identical; only the link to the moved file collapses.
        (vault / PROJECTS_PREFIX / "Pkg2").mkdir()
        (vault / PROJECTS_PREFIX / "OtherDir").mkdir()
        (vault / PROJECTS_PREFIX / "Reloc2").mkdir()
        h_src = vault / PROJECTS_PREFIX / "Pkg2" / "Homonym.md"
        h_src.write_text("---\ntype: reference\n---\n\n# Homonym\n\nbody\n", encoding="utf-8")
        # The homonym — a different file of the same basename — stays put.
        h_twin = vault / PROJECTS_PREFIX / "OtherDir" / "Homonym.md"
        h_twin.write_text("---\ntype: reference\n---\n\n# Other\n\nbody\n", encoding="utf-8")
        h_dst = vault / PROJECTS_PREFIX / "Reloc2" / "Homonym.md"
        h_plain = vault / PROJECTS_PREFIX / "h-plain.md"
        h_plain.write_text("Uses [[Homonym]] plainly.\n", encoding="utf-8")
        h_plain_before = h_plain.read_bytes()
        h_moved = vault / PROJECTS_PREFIX / "h-moved.md"
        h_moved.write_text(
            f"Uses [[{PROJECTS_PREFIX}/Pkg2/Homonym|the moved]] here.\n", encoding="utf-8"
        )
        h_homo = vault / PROJECTS_PREFIX / "h-homonym.md"
        h_homo.write_text(
            f"Uses [[{PROJECTS_PREFIX}/OtherDir/Homonym|the other]] here.\n", encoding="utf-8"
        )
        h_homo_before = h_homo.read_bytes()
        res_h = rename_with_links(h_src, h_dst, vault, log)
        if res_h.status != "renamed":
            print(f"FAIL H homonym relocate status={res_h.status} error={res_h.error}")
            failures += 1
        if h_plain.read_bytes() != h_plain_before:
            print("FAIL H plain [[Homonym]] link was rewritten")
            failures += 1
        h_moved_after = h_moved.read_text(encoding="utf-8")
        if "[[Homonym|the moved]]" not in h_moved_after or f"{PROJECTS_PREFIX}/Pkg2/Homonym" in h_moved_after:
            print(f"FAIL H moved-file link not collapsed: {h_moved_after!r}")
            failures += 1
        if h_homo.read_bytes() != h_homo_before:
            print(f"FAIL H homonym link CORRUPTED: {h_homo.read_text(encoding='utf-8')!r}")
            failures += 1
        if (
            res_h.status == "renamed"
            and h_plain.read_bytes() == h_plain_before
            and h_homo.read_bytes() == h_homo_before
            and "[[Homonym|the moved]]" in h_moved_after
        ):
            print("OK   case H: homonym path-link survives byte-identical, moved-file link collapses, plain untouched")

        # --- Case I: a restore failure is recorded, log line still writes (A2)
        i_src = vault / PROJECTS_PREFIX / "Old-Name-I.md"
        i_src.write_text("---\ntype: project\n---\n\n# I\n\nbody\n", encoding="utf-8")
        i_dst = vault / PROJECTS_PREFIX / "New-Name-I.md"
        i_ok = vault / PROJECTS_PREFIX / "i-ok.md"
        i_ok.write_text("Link [[Old-Name-I]] ok.\n", encoding="utf-8")
        i_lock = vault / PROJECTS_PREFIX / "i-locked.md"
        i_lock.write_text("Link [[Old-Name-I]] locked.\n", encoding="utf-8")
        i_ok_before = i_ok.read_bytes()
        i_inject = vault / PROJECTS_PREFIX / "i-injected.md"

        _orig_write = globals()["write_text"]
        _write_counts: dict[str, int] = {}

        def _flaky_write(path, text):
            # Fail only on the SECOND write to i-locked.md — the restore — so
            # step 4's rewrite of it succeeds and only the roll-back fails.
            if Path(path).name == "i-locked.md":
                _write_counts["n"] = _write_counts.get("n", 0) + 1
                if _write_counts["n"] >= 2:
                    raise PermissionError("simulated restore write failure")
            return _orig_write(path, text)

        def _inject_i() -> None:
            i_inject.write_text("Sneaky [[Old-Name-I]] straggler.\n", encoding="utf-8")

        log_len_before = len(log.read_text(encoding="utf-8"))
        globals()["write_text"] = _flaky_write
        try:
            res_i = rename_with_links(i_src, i_dst, vault, log, _after_rewrite_hook=_inject_i)
        finally:
            globals()["write_text"] = _orig_write
        log_now = log.read_text(encoding="utf-8")
        if res_i.status != "aborted":
            print(f"FAIL I restore-failure status={res_i.status} (expected aborted)")
            failures += 1
        if i_lock not in res_i.restore_failures:
            print(f"FAIL I locked file not recorded as restore failure: {res_i.restore_failures}")
            failures += 1
        if i_ok.read_bytes() != i_ok_before:
            print("FAIL I the OTHER inbound file was not restored despite the failure")
            failures += 1
        if not i_src.exists():
            print("FAIL I source removed despite abort")
            failures += 1
        if len(log_now) <= log_len_before or "FAILED to restore" not in log_now:
            print("FAIL I abort log line missing or does not record the restore failure")
            failures += 1
        if (
            res_i.status == "aborted"
            and i_lock in res_i.restore_failures
            and i_ok.read_bytes() == i_ok_before
            and len(log_now) > log_len_before
            and "FAILED to restore" in log_now
        ):
            print("OK   case I: restore failure recorded truthfully, abort log line still written")

        # --- Case J: basename CHANGE must not redirect a homonym's path link
        # (P1 gate MUST-FIX). Renaming A/Meeting -> A/Team-Sync while B has its
        # own Meeting: B's path-qualified link stays byte-identical; the source's
        # own path-qualified and plain links rewrite to the new name.
        (vault / PROJECTS_PREFIX / "A").mkdir()
        (vault / PROJECTS_PREFIX / "B").mkdir()
        j_src = vault / PROJECTS_PREFIX / "A" / "Meeting.md"
        j_src.write_text("---\ntype: note\n---\n\n# Meeting\n\nbody\n", encoding="utf-8")
        # B's own Meeting — a different note of the same basename — stays put.
        (vault / PROJECTS_PREFIX / "B" / "Meeting.md").write_text(
            "---\ntype: note\n---\n\n# B Meeting\n\nbody\n", encoding="utf-8"
        )
        j_dst = vault / PROJECTS_PREFIX / "A" / "Team-Sync.md"
        j_b = vault / PROJECTS_PREFIX / "j-b-link.md"
        j_b.write_text(
            f"See [[{PROJECTS_PREFIX}/B/Meeting|the B team]] for that.\n", encoding="utf-8"
        )
        j_b_before = j_b.read_bytes()
        j_srcpath = vault / PROJECTS_PREFIX / "j-src-path.md"
        j_srcpath.write_text(
            f"See [[{PROJECTS_PREFIX}/A/Meeting|the A team]] here.\n", encoding="utf-8"
        )
        j_plain = vault / PROJECTS_PREFIX / "j-plain.md"
        j_plain.write_text("See [[Meeting]] plainly.\n", encoding="utf-8")
        res_j = rename_with_links(j_src, j_dst, vault, log)
        if res_j.status != "renamed":
            print(f"FAIL J change-rename status={res_j.status} error={res_j.error}")
            failures += 1
        if j_b.read_bytes() != j_b_before:
            print(f"FAIL J homonym B link CORRUPTED: {j_b.read_text(encoding='utf-8')!r}")
            failures += 1
        j_srcpath_after = j_srcpath.read_text(encoding="utf-8")
        if "[[Team-Sync|the A team]]" not in j_srcpath_after or f"{PROJECTS_PREFIX}/A/Meeting" in j_srcpath_after:
            print(f"FAIL J source path-qualified link not rewritten: {j_srcpath_after!r}")
            failures += 1
        j_plain_after = j_plain.read_text(encoding="utf-8")
        if "[[Team-Sync]]" not in j_plain_after:
            print(f"FAIL J plain link not rewritten to new name: {j_plain_after!r}")
            failures += 1
        if (
            res_j.status == "renamed"
            and j_b.read_bytes() == j_b_before
            and "[[Team-Sync|the A team]]" in j_srcpath_after
            and "[[Team-Sync]]" in j_plain_after
        ):
            print("OK   case J: basename change rewrites source's own links, leaves homonym's path link byte-identical")

    if failures:
        print(f"\n{failures} integration test failure(s).")
        sys.exit(1)
    print("\nAll integration tests passed.")


if __name__ == "__main__":
    _run_integration_tests()
