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
Walk `<archive>`. An item ages when four things hold together:

1. **Its archival stamp is past the window.** The clock is the date stamp the
   archive itself carries — in the filename or in a parent folder
   (`<archive>/Projects/Foo/2026-01-01-bar.md`, `<archive>/Logs/2026-06-20-x/`).
   The latest stamp on the path wins, because the outermost dated container
   records when the material was archived. **mtime is not the clock**: an
   archived copy preserves its source's modification time, so mtime reads the
   content's age, not the archival age, and it misreads in both directions. An
   item carrying no stamp anywhere on its path never ages — it is reported, and
   a human decides.
2. **It is cold.** mtime does hold one veto: material touched inside the window
   is live work, whatever its stamp says, and stays.
3. **No live document cites it.** A vault-relative path written in a live
   document, or a wikilink that resolves to the archived copy because no live
   file carries that basename, holds the item where the citation can follow it.
4. **It is neither a live surface nor a retained ledger.** Live surfaces are the
   named control files and each agent's live log head. Retained ledgers are the
   rows of § Cold storage's retained-ledger table — skipped permanently,
   whatever their stamp.

Everything that survives all four moves to `<history>`, preserving its path
(copy → verify by content hash → delete), and lands one line in the sweep
manifest.

The manifest
------------
Every sweep — dry run or apply — writes one line to
`<logs>/manual-operations/<date>-retention-sweep.md`, plus one line per item it
moves (CONFIG § Helper-script automation: "logs `<archive>` item age past
`archive-retention` with a manifest line per sweep"). A flag pass that writes
nothing to the archive still records what it found, so the analyst who judges
the move reads a record rather than a memory.

Held items are NAMED, not counted: every citation-held item and every unstamped
item gets its own manifest line, because each is a file a person decides about
and a count on the sweep line names no path to act on.

Config-driven, no hardcoded literals
-------------------------------------
The archive folder, the cold-store directory name, the retention window, and the
retained-ledger rows are all read from CONFIG.md via config_variables — rename
the archive folder, change the retention window, rename the cold-store dir, or
add a retained ledger, and this script follows with zero code edits.

The retained-ledger read fails CLOSED: a § Cold storage that names retained
ledgers and yields no parsed row aborts the sweep loudly rather than sweeping
with an empty retained set — a heading reword must never make the queue's
decision history ageable.

Never descends a dot-directory on the source side, so it cannot re-walk the
cold store itself (CONFIG § Scan exclusions). Idempotent: a file already moved is
gone from the source, so a re-run does nothing to it; a destination collision is
resolved by suffixing, never by overwrite.

Trigger: daily (CONFIG § Helper-script automation).

Usage
-----
    JANITOR_VAULT_ROOT=/path/to/vault python age_to_cold_storage.py [--apply]
    python age_to_cold_storage.py --vault-root <vault> --apply \
        --sweep-folder 2026-07-26-retention-sweep

Without --apply it is a dry run: it prints what it would move and touches
nothing but the manifest. `--sweep-folder NAME` nests the moved tree under
`<history>/NAME/`, so one approved sweep lands as one reviewable unit.
Run with no vault root and no args to execute the self-tests.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
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
    _CONFIG_TEXT,
    _folder_by_semantic,
    folder_for_wildcard,
    is_excluded_dir,
    token_path,
    ARCHIVE_RETENTION_DAYS,
    HISTORY_DIRNAME,
)


@dataclass
class AgeResult:
    """Outcome of one aging pass."""
    moved: list[tuple[str, str]] = field(default_factory=list)   # (src_rel, dst_rel)
    eligible: list[tuple[str, str, int]] = field(default_factory=list)  # rel, stamp, bytes
    held_cited: list[tuple[str, str]] = field(default_factory=list)     # rel, why
    # Every archive file the sweep passed over for want of an archival date
    # stamp. A count alone names no path, so nobody can act on it; the manifest
    # carries one line per item.
    unstamped: list[str] = field(default_factory=list)
    moved_bytes: int = 0
    eligible_bytes: int = 0
    skipped_recent: int = 0
    skipped_live: int = 0
    skipped_retained: int = 0
    skipped_unstamped: int = 0
    skipped_hot: int = 0
    errors: list[str] = field(default_factory=list)
    manifest: str = ""
    dry_run: bool = True


# Live operational surfaces, protected by exact name (CONFIG § Operational
# documents). Everything else under the archive ages normally — including a
# CLOSED log segment (`<agent>-<YYYY-MM>.md`), which is the whole point of the
# rotation convention in § Log files: the head stays, the segment ages.
_LIVE_SURFACE_NAMES = frozenset({
    "Vault-State-Index.md",
    "run-lease.md",
    "fix-verification.md",
    # The settled-convention store the state index reads at detect time
    # (§ Log files) — a live lookup surface, never aged.
    "Conventions.md",
    # Sync-Log.md is a live control file (build_state_index CONTROL_FILES) that
    # sync_config reads back via _last_log_hash() for change detection — aging it
    # would silently reset sync's idempotence, so it is protected by name too.
    "Sync-Log.md",
})

# Live text the citation scan reads. A binary or an asset cites nothing.
_LIVE_TEXT_SUFFIXES = (".md", ".canvas", ".base", ".txt")
_KIT_TEXT_SUFFIXES = (".md", ".py", ".json", ".txt")

# An archival stamp on a path segment: `2026-06-20-thing`, `2026-06` (a closed
# log segment), `Logs/2026-06-19-queue-scope-fix/`.
_PATH_STAMP_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})-(\d{2})(?:-(\d{2}))?(?!\d)")
_WIKILINK_RE = re.compile(r"\[\[([^\]\|#]+)")


def _is_live_surface(path: Path) -> bool:
    """True for a protected live surface: a named control file, or an agent's
    live log HEAD (`<logs>/<agent>/<agent>.md`, whose stem equals its folder).

    A rotated segment (`janitor-agent-2026-06.md`) has a stem that differs from
    its folder, so it is ageable — the distinction hunk 4 of the Lifecycle
    window narrowed the script-skip to express.
    """
    if path.name in _LIVE_SURFACE_NAMES:
        return True
    return path.suffix == ".md" and path.stem == path.parent.name


# ---------------------------------------------------------------------------
# § Cold storage — the retained-ledger table
# ---------------------------------------------------------------------------

def _section_text(text: str, heading: str) -> str:
    """The body of an H2 section, empty when the section is absent."""
    m = re.search(r"^## " + re.escape(heading) + r"\s*$", text, re.MULTILINE)
    if m is None:
        return ""
    after = text[m.end():]
    nxt = re.search(r"^## ", after, re.MULTILINE)
    return after[: nxt.start()] if nxt else after


def _expand_tokens(pattern: str) -> str:
    """Resolve `<token>` references in a CONFIG path pattern to real folders."""
    def sub(m: re.Match) -> str:
        token = m.group(0)
        resolved = folder_for_wildcard(token)
        if resolved:
            return resolved
        return token_path(token, token)
    return re.sub(r"<[\w-]+>", sub, pattern).strip("/")


class RetainedLedgerParseError(RuntimeError):
    """§ Cold storage names retained ledgers and the parse read none of them."""


# The retained-ledger table header, however CONFIG spells it.
_RETAINED_HEADERS = frozenset({"retained ledger", "retained ledgers"})
# The block marker in the section's prose — the same words CONFIG uses to
# introduce the table, so a heading reword still trips the fail-closed gate.
_RETAINED_BLOCK = re.compile(r"retained\s+ledgers?", re.IGNORECASE)


def _header_cell(raw: str) -> str:
    """A table cell reduced to its words: backticks, bold marks, and repeated
    whitespace dropped, lowercased. `**Retained Ledgers**` reads as
    `retained ledgers`."""
    return " ".join(raw.replace("`", "").replace("*", "").split()).lower()


def parse_retained_ledgers(text: str = _CONFIG_TEXT) -> tuple[str, ...]:
    """Vault-relative glob patterns from § Cold storage's retained-ledger table.

    § Cold storage carries two tables — the settings table first, the retained
    ledgers second — so the rows are read from the table whose header names the
    retained ledger, never from whichever table happens to come first. The
    header match is case-insensitive and takes singular or plural.

    FAIL CLOSED: a § Cold storage that names retained ledgers and yields zero
    rows raises RetainedLedgerParseError. The alternative is the K6 failure —
    a heading reword empties the retained set in silence and the next sweep
    ages the queue's decision history into the cold store.
    """
    section = _section_text(text, "Cold storage")
    patterns: list[str] = []
    in_table = False
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not cells:
            continue
        first = cells[0].strip("`").strip()
        if _header_cell(cells[0]) in _RETAINED_HEADERS:
            in_table = True
            continue
        if not in_table:
            continue
        if not first or set(first) <= set("-: "):
            continue                      # the header separator row
        patterns.append(_expand_tokens(first))
    resolved = tuple(p for p in patterns if p)
    if not resolved and _RETAINED_BLOCK.search(section):
        raise RetainedLedgerParseError(
            "CONFIG § Cold storage names retained ledgers but no row parsed — "
            "the retained set would be empty and every ledger ageable. Sweep "
            "aborted. Restore the `retained ledger | reason` table header in "
            "§ Cold storage, or run again once the section parses."
        )
    return resolved


def _translate_glob(body: str) -> str:
    """Glob body to regex body: `**` spans separators, `*` and `?` stay inside
    one segment."""
    out: list[str] = []
    i = 0
    while i < len(body):
        ch = body[i]
        if body.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    return "".join(out)


def _glob_to_regex(pattern: str) -> re.Pattern:
    """Compile a `**`-aware path glob against a vault-relative posix path.

    `A/B/**` covers everything under `A/B` and `A/B` itself, so naming a folder
    as a retained ledger retains the folder.
    """
    if pattern.endswith("/**"):
        return re.compile("^" + _translate_glob(pattern[:-3]) + "(?:/.*)?$")
    return re.compile("^" + _translate_glob(pattern) + "$")


class RetainedLedgers:
    """The § Cold storage rows this sweep never ages."""

    def __init__(self, patterns: tuple[str, ...]):
        self.patterns = patterns
        self._res = [_glob_to_regex(p) for p in patterns]

    def matches(self, vault_rel: str) -> str | None:
        for pattern, rx in zip(self.patterns, self._res):
            if rx.match(vault_rel):
                return pattern
        return None


# ---------------------------------------------------------------------------
# The archival clock — the date stamp on the path
# ---------------------------------------------------------------------------

def path_stamp(vault_rel: str) -> datetime | None:
    """The archival date stamp on a path, or None when it carries none.

    Every segment is read, and the LATEST stamp wins: a dated container records
    when its contents were archived, and a file re-stamped inside it is newer
    still. A bare `YYYY-MM` (a closed log segment) reads as the first of that
    month.
    """
    best: datetime | None = None
    for segment in vault_rel.replace("\\", "/").split("/"):
        for m in _PATH_STAMP_RE.finditer(segment):
            year, month, day = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
            try:
                stamped = datetime(year, month, day, tzinfo=timezone.utc)
            except ValueError:
                continue
            if best is None or stamped > best:
                best = stamped
    return best


# ---------------------------------------------------------------------------
# Live-document citations
# ---------------------------------------------------------------------------

class LiveCitations:
    """What the live vault points at, read once per sweep.

    A live document is any file outside `<archive>` and outside every
    dot-directory; the kit's own text (`.claude/`) joins the scan for path
    mentions, because a script or SKILL naming an archived path breaks the same
    way a note does.
    """

    def __init__(self, vault_root: Path, archive_root: Path):
        self.blob = ""
        self.wikilinks: set[str] = set()
        self.basenames: set[str] = set()
        chunks: list[str] = []
        for dirpath, dirnames, filenames in os.walk(vault_root):
            dirnames[:] = [d for d in dirnames if not is_excluded_dir(d)]
            here = Path(dirpath)
            if here == vault_root:
                dirnames[:] = [d for d in dirnames
                               if (here / d) != archive_root]
            for name in filenames:
                self.basenames.add(name.lower())
                if name.lower().endswith(_LIVE_TEXT_SUFFIXES):
                    chunks.append(_read_text(here / name))
        live_text = "\n".join(chunks)
        self.wikilinks = {m.group(1).strip().lower()
                          for m in _WIKILINK_RE.finditer(live_text)}
        kit_root = vault_root / ".claude"
        kit_chunks: list[str] = []
        if kit_root.is_dir():
            for dirpath, dirnames, filenames in os.walk(kit_root):
                dirnames[:] = [d for d in dirnames
                               if d not in ("__pycache__", "vault-search", ".git")]
                for name in filenames:
                    if name.lower().endswith(_KIT_TEXT_SUFFIXES):
                        kit_chunks.append(_read_text(Path(dirpath) / name))
        self.blob = live_text + "\n" + "\n".join(kit_chunks)

    def cites(self, vault_rel: str, path: Path) -> str | None:
        """Why a live document holds this archived file, or None."""
        if vault_rel in self.blob:
            return "path-cited"
        stem = path.stem.lower()
        if (path.suffix.lower() == ".md" and stem in self.wikilinks
                and f"{stem}.md" not in self.basenames):
            return "wikilink-resolves-here"
        return None


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# The sweep
# ---------------------------------------------------------------------------

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


def _file_md5(path: Path) -> str:
    """md5 of a file's bytes, read in chunks. The copy-verify gate before an
    unlink compares this, never the size — the cache-key rule applied to the
    last check standing between a file and its deletion."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def age_archive_to_history(
    vault_root: Path,
    *,
    retention_days: int = ARCHIVE_RETENTION_DAYS,
    now: datetime | None = None,
    apply: bool = False,
    sweep_folder: str | None = None,
    log: bool = True,
    retained: RetainedLedgers | None = None,
) -> AgeResult:
    """Move archive files past the retention window into the cold store.

    Returns an AgeResult listing what moved (or would move, when apply is False)
    and what was held back, and writes the sweep's manifest line.
    """
    vault_root = Path(vault_root)
    archive_root = vault_root / _folder_by_semantic("archive")
    history_root = vault_root / HISTORY_DIRNAME
    if sweep_folder:
        history_root = history_root / sweep_folder
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=retention_days)
    retained = retained if retained is not None else RetainedLedgers(
        parse_retained_ledgers())

    result = AgeResult(dry_run=not apply)
    if not archive_root.is_dir():
        if log:
            _write_manifest(vault_root, result, now, retention_days,
                            sweep_folder, retained)
        return result

    candidates: list[tuple[Path, str, datetime, int]] = []
    for src in _iter_archive_files(archive_root):
        vault_rel = str(src.relative_to(vault_root)).replace("\\", "/")
        if _is_live_surface(src):
            result.skipped_live += 1
            continue
        if retained.matches(vault_rel):
            result.skipped_retained += 1
            continue
        stamped = path_stamp(vault_rel)
        if stamped is None:
            result.skipped_unstamped += 1
            result.unstamped.append(vault_rel)
            continue
        if stamped >= cutoff:
            result.skipped_recent += 1
            continue
        try:
            stat = src.stat()
        except OSError as exc:
            result.errors.append(f"stat failed: {src} ({exc})")
            continue
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc)
        if mtime >= cutoff:
            # Touched inside the window: live work whatever its stamp reads.
            result.skipped_hot += 1
            continue
        candidates.append((src, vault_rel, stamped, stat.st_size))

    if candidates:
        citations = LiveCitations(vault_root, archive_root)
        for src, vault_rel, stamped, size in candidates:
            why = citations.cites(vault_rel, src)
            if why:
                result.held_cited.append((vault_rel, why))
                continue
            result.eligible.append((vault_rel, stamped.date().isoformat(), size))
            result.eligible_bytes += size

    for vault_rel, stamp_text, size in result.eligible:
        src = vault_root / vault_rel
        if sweep_folder:
            dest = _unique_dest(history_root / vault_rel)
        else:
            dest = _unique_dest(history_root / src.relative_to(archive_root))
        dest_rel = str(dest.relative_to(vault_root)).replace("\\", "/")
        if not apply:
            result.moved.append((vault_rel, dest_rel))
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # Archive-first: copy to the cold store, confirm, then remove source.
            # The gate before an unlink reads the CONTENT — a matching size is a
            # coincidence a truncated or half-flushed copy passes.
            shutil.copy2(src, dest)
            if dest.exists() and _file_md5(dest) == _file_md5(src):
                src.unlink()
                result.moved.append((vault_rel, dest_rel))
                result.moved_bytes += size
            else:
                result.errors.append(
                    f"copy verify failed (content hash mismatch), source kept: "
                    f"{src}")
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

    if log:
        _write_manifest(vault_root, result, now, retention_days, sweep_folder,
                        retained)
    return result


# ---------------------------------------------------------------------------
# The manifest
# ---------------------------------------------------------------------------

_MANIFEST_ACTOR = "age-to-cold-storage"


def manifest_path(vault_root: Path, now: datetime) -> Path:
    """`<logs>/manual-operations/<date>-retention-sweep.md`."""
    logs = Path(vault_root) / token_path("logs", "Archive/Logs")
    return (logs / "manual-operations"
            / f"{now.strftime('%Y-%m-%d')}-retention-sweep.md")


def _write_manifest(
    vault_root: Path,
    result: AgeResult,
    now: datetime,
    retention_days: int,
    sweep_folder: str | None,
    retained: RetainedLedgers,
) -> None:
    """Append this sweep's manifest: one sweep line, one line per moved item."""
    path = manifest_path(Path(vault_root), now)
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    mode = "apply" if not result.dry_run else "dry-run"
    dest_root = HISTORY_DIRNAME + (f"/{sweep_folder}" if sweep_folder else "")
    lines = [
        f"{stamp} | {_MANIFEST_ACTOR} | sweep | "
        f"{_folder_by_semantic('archive')} | mode={mode} retention={retention_days}d "
        f"clock=path-stamp dest={dest_root}/ eligible={len(result.eligible)} "
        f"({result.eligible_bytes} B) moved={len(result.moved) if not result.dry_run else 0} "
        f"({result.moved_bytes} B) held-cited={len(result.held_cited)} "
        f"retained-ledger={result.skipped_retained} unstamped={result.skipped_unstamped} "
        f"hot={result.skipped_hot} within-window={result.skipped_recent} "
        f"live-surface={result.skipped_live} errors={len(result.errors)}"
    ]
    if not result.dry_run:
        for src_rel, dest_rel in result.moved:
            lines.append(f"{stamp} | {_MANIFEST_ACTOR} | moved | {src_rel} | "
                         f"-> {dest_rel}")
    # Held and unstamped items are named, not merely counted: each is a file a
    # person decides about — stamp it, file it, or leave it — and a count on the
    # sweep line names no path to act on. They log on every sweep, dry-run and
    # apply alike, because the decision does not wait for a move.
    for rel, why in result.held_cited:
        lines.append(f"{stamp} | {_MANIFEST_ACTOR} | held-cited | {rel} | {why}")
    for rel in result.unstamped:
        lines.append(f"{stamp} | {_MANIFEST_ACTOR} | unstamped | {rel} | "
                     f"no archival date stamp on the path — stamp it "
                     f"(YYYY-MM-DD-) or file it to make it ageable")
    for err in result.errors:
        lines.append(f"{stamp} | {_MANIFEST_ACTOR} | move-failed | - | {err}")

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            prior = path.read_bytes()
            newline = b"\r\n" if prior.count(b"\r\n") > (
                prior.count(b"\n") - prior.count(b"\r\n")) else b"\n"
            head = b"" if prior.endswith(newline) or not prior else newline
        else:
            newline = b"\n"
            head = (
                f"# Retention sweep — {now.strftime('%Y-%m-%d')}\n\n"
                f"<!-- {_MANIFEST_ACTOR} manifest — append-only, one line per "
                f"action: timestamp | actor | code | target | value -->\n\n"
            ).encode("utf-8")
        body = head + newline.join(line.encode("utf-8") for line in lines) + newline
        with open(path, "ab") as fh:
            fh.write(body)
        result.manifest = str(path.relative_to(Path(vault_root))).replace("\\", "/")
    except OSError as exc:
        result.errors.append(f"manifest write failed: {path} ({exc})")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main() -> int:
    parser = argparse.ArgumentParser(
        description="Age <archive> material past archive-retention into <history>."
    )
    parser.add_argument("--apply", action="store_true",
                        help="Perform the moves. Without it, dry-run (print only).")
    parser.add_argument("--vault-root", type=Path, default=None)
    parser.add_argument("--sweep-folder", default=None, metavar="NAME",
                        help="Nest the moved tree under <history>/NAME/.")
    parser.add_argument("--no-log", action="store_true",
                        help="Skip the manifest line (self-tests and rehearsals).")
    args = parser.parse_args()

    vault_root_str = args.vault_root or os.environ.get("JANITOR_VAULT_ROOT")
    if not vault_root_str:
        # No vault context: run the self-tests.
        return _run_self_tests()
    vault_root = Path(vault_root_str).resolve()
    if not vault_root.is_dir():
        sys.exit(f"Error: vault root is not a directory: {vault_root}")

    try:
        retained = RetainedLedgers(parse_retained_ledgers())
    except RetainedLedgerParseError as exc:
        # Fail closed: with an empty retained set every ledger is ageable, so the
        # sweep stops here rather than moving anything.
        print(f"ABORTED: {exc}", file=sys.stderr)
        return 2
    result = age_archive_to_history(
        vault_root, apply=args.apply, sweep_folder=args.sweep_folder,
        log=not args.no_log, retained=retained,
    )
    mode = "APPLIED" if args.apply else "DRY-RUN"
    dest = HISTORY_DIRNAME + (f"/{args.sweep_folder}" if args.sweep_folder else "")
    print(f"[{mode}] retention={ARCHIVE_RETENTION_DAYS}d  clock=path-stamp  "
          f"cold-store={dest}/")
    print(f"  retained ledgers: {', '.join(retained.patterns) or '(none)'}")
    for src_rel, dst_rel in result.moved:
        print(f"  {src_rel}  ->  {dst_rel}")
    for rel, why in result.held_cited:
        print(f"  HELD {rel}  ({why})")
    for rel in result.unstamped:
        print(f"  UNSTAMPED {rel}  (no archival date stamp — never ageable)")
    print(f"  eligible={len(result.eligible)} ({result.eligible_bytes} B) "
          f"moved={len(result.moved)} ({result.moved_bytes} B) "
          f"held-cited={len(result.held_cited)} "
          f"retained-ledger={result.skipped_retained} "
          f"unstamped={result.skipped_unstamped} hot={result.skipped_hot} "
          f"within-window={result.skipped_recent} "
          f"live-surface={result.skipped_live} errors={len(result.errors)}")
    if result.manifest:
        print(f"  manifest: {result.manifest}")
    for e in result.errors:
        print(f"  ERROR: {e}", file=sys.stderr)
    return 1 if result.errors else 0


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    failures = 0
    fixed_now = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)
    old_day = (fixed_now - timedelta(days=ARCHIVE_RETENTION_DAYS + 10)).date()
    new_day = (fixed_now - timedelta(days=2)).date()

    def fail(msg: str) -> None:
        nonlocal failures
        print(f"FAIL {msg}")
        failures += 1

    def backdate(path: Path) -> None:
        ts = (fixed_now - timedelta(days=ARCHIVE_RETENTION_DAYS + 10)).timestamp()
        os.utime(path, (ts, ts))

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        (archive / "Projects" / "Demo").mkdir(parents=True, exist_ok=True)
        (vault / HISTORY_DIRNAME).mkdir(parents=True, exist_ok=True)

        old = archive / "Projects" / "Demo" / f"{old_day}-old-session.md"
        old.write_text("old — aged out\n", encoding="utf-8")
        recent = archive / "Projects" / "Demo" / f"{new_day}-recent-session.md"
        recent.write_text("recent\n", encoding="utf-8")
        unstamped = archive / "Projects" / "Demo" / "No-Stamp-Anywhere.md"
        unstamped.write_text("no stamp\n", encoding="utf-8")
        for p in (old, recent, unstamped):
            backdate(p)

        # --- Case A: dry-run identifies the aged file, moves nothing ---------
        dry = age_archive_to_history(vault, now=fixed_now, apply=False, log=False)
        if len(dry.moved) != 1 or not dry.moved[0][0].endswith("old-session.md"):
            fail(f"A dry-run moved set wrong: {dry.moved}")
        if not old.exists() or (vault / HISTORY_DIRNAME / "Projects" / "Demo"
                                / old.name).exists():
            fail("A dry-run mutated the filesystem")
        else:
            print("OK   case A: dry-run identifies the aged file, moves nothing")

        # --- Case B: the stamp is the clock, not mtime ------------------------
        if dry.skipped_unstamped != 1:
            fail(f"B unstamped skip={dry.skipped_unstamped} (want 1)")
        if dry.skipped_recent != 1:
            fail(f"B within-window skip={dry.skipped_recent} (want 1)")
        if not failures:
            print("OK   case B: an ancient mtime never ages an unstamped or "
                  "recently-stamped file")

        # --- Case C: apply moves the aged file, keeps the rest ---------------
        res = age_archive_to_history(vault, now=fixed_now, apply=True, log=False)
        moved_dest = vault / HISTORY_DIRNAME / "Projects" / "Demo" / old.name
        if old.exists():
            fail("C aged file still in archive after apply")
        if not moved_dest.exists():
            fail("C aged file not in cold store after apply")
        if not (recent.exists() and unstamped.exists()):
            fail("C a held file was wrongly aged out")
        if res.moved_bytes <= 0:
            fail("C moved_bytes not recorded")
        if not failures:
            print("OK   case C: apply moves the aged file, preserves the subpath")

        # --- Case D: idempotent re-run is a no-op ----------------------------
        res2 = age_archive_to_history(vault, now=fixed_now, apply=True, log=False)
        if res2.moved:
            fail(f"D re-run moved {res2.moved} (should be empty)")
        else:
            print("OK   case D: re-run is a no-op")

        # --- Case E: never descends the cold store / dot-dirs ----------------
        dotdir = archive / ".staging"
        dotdir.mkdir(parents=True, exist_ok=True)
        hidden = dotdir / f"{old_day}-kept.md"
        hidden.write_text("hidden\n", encoding="utf-8")
        backdate(hidden)
        res3 = age_archive_to_history(vault, now=fixed_now, apply=True, log=False)
        if any(".staging" in m[0] for m in res3.moved) or not hidden.exists():
            fail("E walked into a dot-directory under the archive")
        else:
            print("OK   case E: dot-directories under the archive are not walked")

    with tempfile.TemporaryDirectory() as tmp:
        # --- Case F: a live citation holds its archived file -----------------
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        (archive / "Sessions").mkdir(parents=True, exist_ok=True)
        (vault / "Projects").mkdir(parents=True, exist_ok=True)
        cited = archive / "Sessions" / f"{old_day}-cited-session.md"
        cited.write_text("cited\n", encoding="utf-8")
        linked = archive / "Sessions" / f"{old_day}-Linked-Note.md"
        linked.write_text("linked\n", encoding="utf-8")
        free = archive / "Sessions" / f"{old_day}-free-session.md"
        free.write_text("free\n", encoding="utf-8")
        for p in (cited, linked, free):
            backdate(p)
        live = vault / "Projects" / "Live.md"
        live.write_text(
            f"See {_folder_by_semantic('archive')}/Sessions/{cited.name} "
            f"and [[{linked.stem}]] — both still in play.\n", encoding="utf-8")

        res = age_archive_to_history(vault, now=fixed_now, apply=True, log=False)
        held = {rel for rel, _ in res.held_cited}
        if not any(r.endswith(cited.name) for r in held):
            fail(f"F a path-cited file was not held: {res.held_cited}")
        if not any(r.endswith(linked.name) for r in held):
            fail(f"F a wikilink-cited file was not held: {res.held_cited}")
        if not cited.exists() or not linked.exists():
            fail("F a cited file was moved")
        if free.exists():
            fail("F the uncited file was not moved")
        if not failures:
            print("OK   case F: a path mention and a resolving wikilink each "
                  "hold their archived file; the uncited one moves")

        # --- Case G: a live file of the same basename frees the wikilink -----
        twin = archive / "Sessions" / f"{old_day}-Twin-Note.md"
        twin.write_text("twin\n", encoding="utf-8")
        backdate(twin)
        (vault / "Projects" / f"{old_day}-Twin-Note.md").write_text(
            "the live twin\n", encoding="utf-8")
        live.write_text(f"[[{twin.stem}]]\n", encoding="utf-8")
        res = age_archive_to_history(vault, now=fixed_now, apply=True, log=False)
        if twin.exists():
            fail("G a wikilink resolving to a live twin still held the archive copy")
        else:
            print("OK   case G: a wikilink with a live namesake resolves live, "
                  "so the archived copy ages")

    with tempfile.TemporaryDirectory() as tmp:
        # --- Case H: retained ledgers and live surfaces never age ------------
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        ledger_rel = "Inbox/User-Queue"
        (archive / ledger_rel).mkdir(parents=True, exist_ok=True)
        ledger = archive / ledger_rel / f"{old_day}-queue-snapshot.md"
        ledger.write_text("queue history\n", encoding="utf-8")
        backdate(ledger)
        logs_dir = vault / token_path("logs", "Archive/Logs") / "demo-agent"
        logs_dir.mkdir(parents=True, exist_ok=True)
        head = logs_dir / "demo-agent.md"
        head.write_text(f"{old_day}T10:00Z | demo-agent | run-start | x | y\n",
                        encoding="utf-8")
        segment = logs_dir / "demo-agent-2026-06.md"
        segment.write_text("2026-06-01T10:00Z | demo-agent | run-end | x | y\n",
                           encoding="utf-8")
        for p in (head, segment):
            backdate(p)

        retained = RetainedLedgers((f"{_folder_by_semantic('archive')}/"
                                    f"{ledger_rel}/**",))
        res = age_archive_to_history(vault, now=fixed_now, apply=True, log=False,
                                     retained=retained)
        if not ledger.exists():
            fail("H a retained-ledger file was aged out")
        if res.skipped_retained != 1:
            fail(f"H retained skip={res.skipped_retained} (want 1)")
        if not head.exists():
            fail("H a live log head was aged out")
        if segment.exists():
            fail("H a closed log segment was not aged")
        if not failures:
            print("OK   case H: retained ledgers and live heads stay; a closed "
                  "segment ages")

        # --- Case I: the retained-ledger table parses out of CONFIG ----------
        parsed = parse_retained_ledgers()
        if not parsed:
            fail("I no retained-ledger row parsed from CONFIG")
        elif any("<" in p for p in parsed):
            fail(f"I an unexpanded token survived: {parsed}")
        else:
            print(f"OK   case I: retained ledgers from CONFIG — {list(parsed)}")

    with tempfile.TemporaryDirectory() as tmp:
        # --- Case J: every sweep writes its manifest -------------------------
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        (archive / "Sessions").mkdir(parents=True, exist_ok=True)
        item = archive / "Sessions" / f"{old_day}-manifest-demo.md"
        item.write_text("demo\n", encoding="utf-8")
        backdate(item)

        dry = age_archive_to_history(vault, now=fixed_now, apply=False)
        mpath = manifest_path(vault, fixed_now)
        if not mpath.exists():
            fail("J the dry-run sweep wrote no manifest")
        else:
            text = mpath.read_text(encoding="utf-8")
            if "| sweep |" not in text or "mode=dry-run" not in text:
                fail(f"J dry-run sweep line missing: {text}")
            if "| moved |" in text:
                fail("J a dry run logged a moved line")
        if not item.exists():
            fail("J the dry run moved a file")

        res = age_archive_to_history(vault, now=fixed_now, apply=True,
                                     sweep_folder="2026-07-26-retention-sweep")
        text = mpath.read_text(encoding="utf-8")
        if text.count("| sweep |") != 2:
            fail(f"J the applied sweep did not append its own sweep line: {text}")
        if f"| moved | {res.moved[0][0]} |" not in text:
            fail(f"J no per-item moved line: {text}")
        nested = (vault / HISTORY_DIRNAME / "2026-07-26-retention-sweep"
                  / _folder_by_semantic("archive") / "Sessions" / item.name)
        if not nested.exists():
            fail(f"J --sweep-folder did not nest the move: {res.moved}")
        if not failures:
            print("OK   case J: every sweep writes one manifest line, apply adds "
                  "one per moved item, --sweep-folder nests the tree")

    # --- Case L: the retained-ledger read fails CLOSED --------------------
    # A § Cold storage that still names retained ledgers but whose table header
    # no longer parses must abort the sweep, never return an empty retained set.
    reworded = _CONFIG_TEXT.replace("| retained ledger |", "| ledger kept |")
    if reworded == _CONFIG_TEXT:
        fail("L the retained-ledger header was not found to reword")
    else:
        try:
            got = parse_retained_ledgers(reworded)
            fail(f"L a reworded header parsed silently to {got!r} (fail-open)")
        except RetainedLedgerParseError:
            pass
    # A tolerant header still parses: case and plural do not matter.
    for variant in ("| Retained Ledgers |", "| RETAINED LEDGER |"):
        alt = _CONFIG_TEXT.replace("| retained ledger |", variant)
        if not parse_retained_ledgers(alt):
            fail(f"L the header variant {variant!r} did not parse")
    # A CONFIG with no retained-ledger block at all is empty, not an abort.
    if parse_retained_ledgers("## Cold storage\n\nnothing here.\n") != ():
        fail("L a section with no retained-ledger block did not read empty")
    if not failures:
        print("OK   case L: a reworded retained-ledger header aborts the sweep; "
              "case and plural still parse")

    # --- Case M: the copy-verify gate reads content, not size --------------
    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        (archive / "Sessions").mkdir(parents=True, exist_ok=True)
        item = archive / "Sessions" / f"{old_day}-hash-demo.md"
        item.write_text("the real content\n", encoding="utf-8")
        backdate(item)
        real_copy = shutil.copy2

        def corrupting(src, dst, *a, **kw):      # same size, different bytes
            real_copy(src, dst, *a, **kw)
            Path(dst).write_bytes(b"X" * Path(src).stat().st_size)
            return dst

        shutil.copy2 = corrupting                # noqa: B010
        try:
            res = age_archive_to_history(vault, now=fixed_now, apply=True,
                                         log=False)
        finally:
            shutil.copy2 = real_copy              # noqa: B010
        if res.moved:
            fail(f"M a corrupted copy passed the verify gate: {res.moved}")
        if not item.exists():
            fail("M the source was deleted after a corrupted copy")
        if not any("verify failed" in e for e in res.errors):
            fail(f"M the verify failure was not reported: {res.errors}")
        if not failures:
            print("OK   case M: a same-size corrupted copy fails the verify "
                  "gate and the source survives")

    # --- Case N: unstamped and held items are named in the manifest --------
    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        archive = vault / _folder_by_semantic("archive")
        (archive / "Sessions").mkdir(parents=True, exist_ok=True)
        (vault / "Projects").mkdir(parents=True, exist_ok=True)
        nostamp = archive / "Sessions" / "No-Stamp-Here.md"
        nostamp.write_text("unstamped\n", encoding="utf-8")
        cited = archive / "Sessions" / f"{old_day}-cited-demo.md"
        cited.write_text("cited\n", encoding="utf-8")
        (vault / "Projects" / "Live.md").write_text(
            f"see {_folder_by_semantic('archive')}/Sessions/"
            f"{old_day}-cited-demo.md\n", encoding="utf-8")
        for p in (nostamp, cited):
            backdate(p)
        res = age_archive_to_history(vault, now=fixed_now, apply=False)
        text = manifest_path(vault, fixed_now).read_text(encoding="utf-8")
        if res.skipped_unstamped != 1 or res.unstamped != [
                f"{_folder_by_semantic('archive')}/Sessions/No-Stamp-Here.md"]:
            fail(f"N unstamped not collected: {res.unstamped}")
        if "| unstamped | " not in text or "No-Stamp-Here.md" not in text:
            fail(f"N no per-item unstamped manifest line: {text}")
        if "| held-cited | " not in text or "cited-demo.md" not in text:
            fail(f"N no per-item held-cited manifest line: {text}")
        if f"unstamped={res.skipped_unstamped}" not in text:
            fail("N the summary count left the sweep line")
        if not failures:
            print("OK   case N: every unstamped and held item is named in the "
                  "manifest, the summary count stays")

    # --- Case K: the glob matcher ------------------------------------------
    rx = RetainedLedgers(("Archive/Inbox/User-Queue/**",))
    if not rx.matches("Archive/Inbox/User-Queue/2026-06-01-x.md"):
        fail("K a file under a retained folder did not match")
    if not rx.matches("Archive/Inbox/User-Queue"):
        fail("K the retained folder itself did not match")
    if rx.matches("Archive/Inbox/User-Queue-Notes/x.md"):
        fail("K a sibling folder matched a retained pattern")
    if not failures:
        print("OK   case K: `**` covers the folder and its contents, not siblings")

    if failures:
        print(f"\n{failures} self-test failure(s).")
        return 1
    print("\nAll self-tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
