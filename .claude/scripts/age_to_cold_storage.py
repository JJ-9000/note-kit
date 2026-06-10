#!/usr/bin/env python3
"""
age_to_cold_storage.py
===========================

Move material out of `<archive>` and into `<history>` (cold storage) once it
ages past the retention window. CONFIG § Cold storage: `<history>` (`.history/`,
a dot-directory, scan-excluded) holds material aged out of active use; a script
moves archived content past `archive-retention` into it, and no agent prunes
inside it. The retention window is the one knob.

What it does
------------
Walk `<archive>`. For each file whose modification time is older than
`archive-retention` days, move it into `<history>`, preserving its path relative
to the archive root (so `<archive>/01-Projects/Foo/2026-01-01-bar.md` lands at
`<history>/01-Projects/Foo/2026-01-01-bar.md`). The move is archive-first by
nature (copy to the cold-store destination, confirm it, then remove the source);
nothing is deleted outright (CONFIG § Versioning and archiving discipline).

Config-driven, no hardcoded literals
-------------------------------------
The archive folder, the cold-store directory name, and the retention window are
all read from CONFIG.md via config_variables — rename the archive folder, change
the retention window, or rename the cold-store dir, and this script follows with
zero code edits.

Never descends a dot-directory on the source side, so it cannot re-walk the
cold store itself (CONFIG § Scan exclusions). Idempotent: a file already moved is
gone from the source, so a re-run does nothing to it; a destination collision is
resolved by suffixing, never by overwrite.

Trigger: daily (CONFIG § Helper-script automation).

Usage
-----
    JANITOR_VAULT_ROOT=/path/to/vault python age_to_cold_storage.py [--apply]

Without --apply it is a dry run: it prints what it would move and writes nothing.
Run with no JANITOR_VAULT_ROOT and no args to execute the self-tests.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import (  # noqa: E402
    _folder_by_semantic,
    is_excluded_dir,
    ARCHIVE_RETENTION_DAYS,
    HISTORY_DIRNAME,
)


@dataclass
class AgeResult:
    """Outcome of one aging pass."""
    moved: list[tuple[str, str]] = field(default_factory=list)   # (src_rel, dst_rel)
    skipped_recent: int = 0
    errors: list[str] = field(default_factory=list)
    dry_run: bool = True


def _iter_archive_files(archive_root: Path):
    """Yield every file under the archive, pruning dot-directories on descent so
    the walk never re-enters a cold store nested under the archive."""
    for dirpath, dirnames, filenames in os.walk(archive_root):
        dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
        for fname in filenames:
            yield Path(dirpath) / fname


def _unique_dest(dest: Path) -> Path:
    """Return `dest` or a numbered sibling if it already exists, so a move never
    overwrites an existing cold-store file (never-delete discipline)."""
    if not dest.exists():
        return dest
    stem, suffix = dest.stem, dest.suffix
    n = 2
    while True:
        candidate = dest.with_name(f"{stem}-{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


def age_archive_to_history(
    vault_root: Path,
    *,
    retention_days: int = ARCHIVE_RETENTION_DAYS,
    now: datetime | None = None,
    apply: bool = False,
) -> AgeResult:
    """Move archive files older than `retention_days` into the cold store.

    Returns an AgeResult listing what moved (or would move, when apply is False).
    """
    vault_root = Path(vault_root)
    archive_root = vault_root / _folder_by_semantic("archive")
    history_root = vault_root / HISTORY_DIRNAME
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)

    result = AgeResult(dry_run=not apply)
    if not archive_root.is_dir():
        return result

    for src in _iter_archive_files(archive_root):
        try:
            mtime = datetime.fromtimestamp(src.stat().st_mtime, tz=timezone.utc)
        except OSError as exc:
            result.errors.append(f"stat failed: {src} ({exc})")
            continue
        if mtime >= cutoff:
            result.skipped_recent += 1
            continue

        rel = src.relative_to(archive_root)
        dest = _unique_dest(history_root / rel)
        result.moved.append((
            str(src.relative_to(vault_root)).replace("\\", "/"),
            str(dest.relative_to(vault_root)).replace("\\", "/"),
        ))
        if not apply:
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Archive-first: copy to the cold store, confirm, then remove source.
            shutil.copy2(src, dest)
            if dest.exists() and dest.stat().st_size == src.stat().st_size:
                src.unlink()
            else:
                result.errors.append(f"verify failed, source kept: {src}")
        except OSError as exc:
            result.errors.append(f"move failed: {src} -> {dest} ({exc})")

    # Remove archive subdirectories emptied by the move (never the archive root).
    if apply:
        for dirpath, dirnames, filenames in os.walk(archive_root, topdown=False):
            d = Path(dirpath)
            if d == archive_root:
                continue
            if is_excluded_dir(d.name):
                continue
            try:
                if not any(d.iterdir()):
                    d.rmdir()
            except OSError:
                pass

    return result


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Age <archive> material past archive-retention into <history>."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Perform the moves. Without it, dry-run (print only).")
    args = parser.parse_args()

    vault_root_str = os.environ.get("JANITOR_VAULT_ROOT")
    if not vault_root_str:
        # No vault context: run the self-tests.
        return _run_self_tests()
    vault_root = Path(vault_root_str).resolve()
    if not vault_root.is_dir():
        sys.exit(f"Error: JANITOR_VAULT_ROOT is not a directory: {vault_root}")

    result = age_archive_to_history(vault_root, apply=args.apply)
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] retention={ARCHIVE_RETENTION_DAYS}d  cold-store={HISTORY_DIRNAME}/")
    for src_rel, dst_rel in result.moved:
        print(f"  {src_rel}  ->  {dst_rel}")
    print(f"  moved={len(result.moved)} skipped-recent={result.skipped_recent} "
          f"errors={len(result.errors)}")
    for e in result.errors:
        print(f"  ERROR: {e}", file=sys.stderr)
    return 1 if result.errors else 0


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    failures = 0
    fixed_now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)

    def fail(msg: str) -> None:
        nonlocal failures
        print(f"FAIL {msg}")
        failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        (archive / "01-Projects" / "Demo").mkdir(parents=True, exist_ok=True)
        (vault / HISTORY_DIRNAME).mkdir(parents=True, exist_ok=True)

        old = archive / "01-Projects" / "Demo" / "old-session.md"
        old.write_text("old\n", encoding="utf-8")
        recent = archive / "01-Projects" / "Demo" / "recent-session.md"
        recent.write_text("recent\n", encoding="utf-8")

        # Backdate `old` well past the window; keep `recent` fresh.
        old_ts = (fixed_now - timedelta(days=ARCHIVE_RETENTION_DAYS + 10)).timestamp()
        os.utime(old, (old_ts, old_ts))
        fresh_ts = fixed_now.timestamp()
        os.utime(recent, (fresh_ts, fresh_ts))

        # --- Case A: dry-run identifies the old file, writes nothing ---------
        dry = age_archive_to_history(vault, now=fixed_now, apply=False)
        if len(dry.moved) != 1 or not dry.moved[0][0].endswith("old-session.md"):
            fail(f"A dry-run moved set wrong: {dry.moved}")
        if not old.exists() or (vault / HISTORY_DIRNAME / "01-Projects" / "Demo" / "old-session.md").exists():
            fail("A dry-run mutated the filesystem")
        else:
            print("OK   case A: dry-run identifies the aged file, writes nothing")

        # --- Case B: apply moves the old file, preserves the recent one ------
        res = age_archive_to_history(vault, now=fixed_now, apply=True)
        moved_dest = vault / HISTORY_DIRNAME / "01-Projects" / "Demo" / "old-session.md"
        if old.exists():
            fail("B old file still in archive after apply")
        if not moved_dest.exists():
            fail("B old file not in cold store after apply")
        if not recent.exists():
            fail("B recent file was wrongly aged out")
        if res.skipped_recent != 1:
            fail(f"B skipped-recent={res.skipped_recent} (want 1)")
        if not failures:
            print("OK   case B: apply moves aged file, keeps recent, preserves subpath")

        # --- Case C: idempotent re-run is a no-op ----------------------------
        res2 = age_archive_to_history(vault, now=fixed_now, apply=True)
        if res2.moved:
            fail(f"C re-run moved {res2.moved} (should be empty)")
        else:
            print("OK   case C: re-run is a no-op")

        # --- Case D: never descends the cold store / dot-dirs ----------------
        # A dot-directory accidentally under the archive must not be walked.
        dotdir = archive / ".staging"
        dotdir.mkdir(parents=True, exist_ok=True)
        hidden = dotdir / "kept.md"
        hidden.write_text("hidden\n", encoding="utf-8")
        os.utime(hidden, (old_ts, old_ts))
        res3 = age_archive_to_history(vault, now=fixed_now, apply=True)
        if any(".staging" in m[0] for m in res3.moved) or not hidden.exists():
            fail("D walked into a dot-directory under the archive")
        else:
            print("OK   case D: dot-directories under the archive are not walked")

    if failures:
        print(f"\n{failures} self-test failure(s).")
        return 1
    print("\nAll self-tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
