#!/usr/bin/env python3
"""
archive_first.py
================

The kit's one archive-first step. Every script that deletes, renames, or
overwrites a FILED file (a note living outside `<inbox>`) calls
`archive_preimage(path)` first; the call returns only when a byte-identical
pre-image is on disk, and raises otherwise, so the caller aborts BEFORE it
mutates anything.

CONFIG § Versioning and archiving discipline, as one importable function:

    1. Resolve the destination `<archive>/<source-parent-path>/<date>-<filename>`
       (`archive_dest_for`, the shared substrate — the same layout every other
       kit archive write already uses). `<date>` is derived at call time, UTC.
    2. PROBE for a free name before writing: a taken destination steps to the
       first free `<stem>-vNNN<suffix>` beside it. An archive file is never
       overwritten, and nothing is ever written under one name and renamed to
       another — the probe happens first, the single write lands on the probed
       name.
    3. Copy the source bytes to that name.
    4. Verify the copy by sha256 against the bytes that were read.
    5. Return the archive path.

Any failure at any step raises `ArchiveFirstError`. The contract is the whole
point: a caller wraps its mutation as

    pre_image = archive_preimage(path)      # raises -> nothing is mutated
    path.unlink()                           # safe: the bytes survive

so a crash, a watcher, or a bad rename leaves the content recoverable.

CONFIG § Helper-script automation registers this module; `audit.py` emits
`archive-first-bypass` for a mutating call site in the kit's own scripts that
neither imports it nor routes through an equivalent run-bundle pre-image.

Ruling recorded 2026-07-26: a PURE, content-preserving relocate that already
copies, verifies by content hash, deletes the source, and writes a
`<archive>/Rename-Log.md` entry (`rename_with_link_integrity.rename_with_links`)
satisfies archive-first on its own — the destination IS the preserved copy. Such
a mover calls this helper for its other mutating paths (an overwrite of a filed
file that is not the moved file), never for the move itself.

Public API
----------
archive_preimage(path, slug=None, *, vault_root=None, archive_root=None,
                 mirror_tree=False, date=None) -> Path
free_archive_name(dest, *, max_probes=999) -> Path
is_filed(path, *, vault_root=None) -> bool
ArchiveFirstError

Stdlib only (plus the kit's own `config_variables` / `frontmatter_helpers`, both
stdlib-only at import time).

Run as a script to execute its unit tests against a temp fixture:
    python scripts/archive_first.py
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import (  # noqa: E402
    _folder_by_semantic,
    resolve_vault_root,
)
from frontmatter_helpers import (  # noqa: E402
    OutsideVaultError,
    archive_dest_for,
)

# How many `-vNNN` names the probe tries beside a taken destination before it
# gives up. 998 distinct pre-images of one file on one date is already far past
# any real run; the cap keeps a pathological loop bounded.
MAX_VERSION_PROBES = 999


class ArchiveFirstError(RuntimeError):
    """The pre-image could not be written and verified.

    A caller that sees this aborts its mutation: raising here means the file's
    current bytes exist in exactly one place, so deleting, renaming, or
    overwriting them would be unrecoverable.
    """


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def free_archive_name(dest: Path, *, max_probes: int = MAX_VERSION_PROBES) -> Path:
    """`dest` when nothing holds it, else the first free `<stem>-vNNN<suffix>`.

    Probed BEFORE the write, so an archive copy never overwrites an archive file
    and never lands under a temporary name (CONFIG § Versioning and archiving
    discipline). Raises `ArchiveFirstError` when every probe is taken.
    """
    dest = Path(dest)
    if not dest.exists():
        return dest
    for n in range(2, max_probes + 1):
        candidate = dest.with_name(f"{dest.stem}-v{n:03d}{dest.suffix}")
        if not candidate.exists():
            return candidate
    raise ArchiveFirstError(
        f"no free archive name beside {dest} after {max_probes - 1} probes"
    )


def is_filed(path: Path, *, vault_root: Optional[Path] = None) -> bool:
    """True when `path` is a FILED file — inside the vault, outside `<inbox>`.

    An inbox draft is the user's working space: it is edited and replaced in
    place by design, so archive-first does not apply to it (CONFIG § Rules).
    A path outside the vault entirely is not filed vault content either.
    """
    root = _resolve_root(vault_root, path)
    try:
        rel = Path(path).resolve().relative_to(root)
    except (ValueError, OSError):
        return False
    inbox = _folder_by_semantic("inbox")
    return rel.parts[:1] != (inbox,)


def _resolve_root(vault_root: Optional[Path], path: Path) -> Path:
    """The vault root for this call: the explicit argument, else the shared
    resolver (CLI > env > installed-layout walk-up > cwd walk-up), else a
    walk-up from the source file itself."""
    if vault_root is not None:
        return Path(vault_root).resolve()
    root = resolve_vault_root()
    if root is None:
        root = resolve_vault_root(cwd_start=Path(path).resolve().parent)
    if root is None:
        raise ArchiveFirstError(
            f"cannot resolve the vault root for {path}; pass vault_root=... "
            "explicitly rather than archiving outside a vault"
        )
    return Path(root).resolve()


def archive_preimage(
    path: Path,
    slug: Optional[str] = None,
    *,
    vault_root: Optional[Path] = None,
    archive_root: Optional[Path] = None,
    mirror_tree: bool = False,
    date: Optional[str] = None,
) -> Path:
    """Write a verified pre-image of `path` and return where it landed.

    Call this BEFORE deleting, renaming, or overwriting a filed file. On any
    failure — unreadable source, source outside the vault, no free archive name,
    a write error, a hash mismatch — it raises `ArchiveFirstError` and has
    mutated nothing, so the caller aborts with the original still intact.

    Arguments:
        path         the file about to be mutated.
        slug         optional version label; lands as CONFIG's
                     `<date>-<filename>-<version>` shape. Omit for the plain
                     `<date>-<filename>` form.
        vault_root   the vault; defaults to the shared resolver.
        archive_root route the pre-image into a caller-owned directory instead
                     of `<archive>` — e.g. one per-run bundle.
        mirror_tree  with `archive_root`, mirror the exact source-relative path
                     inside it and drop the date stamp from the filename (the
                     bundle folder already carries the date).
        date         override the `YYYY-MM-DD` stamp; defaults to today, UTC,
                     read at call time.
    """
    src = Path(path)
    root = _resolve_root(vault_root, src)

    try:
        data = src.read_bytes()
    except OSError as exc:
        raise ArchiveFirstError(f"cannot read {src} for its pre-image: {exc}") from exc

    try:
        dest = archive_dest_for(
            src, root,
            archive_root=archive_root,
            version=slug,
            date=date,
            mirror_tree=mirror_tree,
        )
    except OutsideVaultError as exc:
        raise ArchiveFirstError(str(exc)) from exc

    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ArchiveFirstError(
            f"cannot create the archive directory {dest.parent}: {exc}"
        ) from exc

    dest = free_archive_name(dest)

    try:
        dest.write_bytes(data)
    except OSError as exc:
        raise ArchiveFirstError(f"pre-image write to {dest} failed: {exc}") from exc

    try:
        written = dest.read_bytes()
    except OSError as exc:
        raise ArchiveFirstError(
            f"pre-image at {dest} could not be read back for verification: {exc}"
        ) from exc

    if _sha256(written) != _sha256(data):
        raise ArchiveFirstError(
            f"pre-image at {dest} failed hash verification "
            f"({len(written)} bytes written vs {len(data)} bytes read)"
        )
    return dest


# ---- unit tests -------------------------------------------------------------


def _run_unit_tests() -> int:
    """Exercise the probe, the verify, and every raise path against a temp vault."""
    import tempfile

    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' - ' + detail) if detail else ''}")
        if not ok:
            failures.append(label)

    archive_name = _folder_by_semantic("archive")
    inbox_name = _folder_by_semantic("inbox")

    with tempfile.TemporaryDirectory(prefix="archive-first-") as tmp:
        vault = Path(tmp) / "vault"
        (vault / ".claude").mkdir(parents=True)
        (vault / archive_name).mkdir(parents=True)
        notes = vault / "Areas" / "Design"
        notes.mkdir(parents=True)
        src = notes / "A-Standard.md"
        src.write_text("---\ntype: design\n---\n\n# A Standard\n", encoding="utf-8")
        original = src.read_bytes()

        # 1. Happy path — canonical layout, verified copy, source untouched.
        dest = archive_preimage(src, vault_root=vault, date="2026-07-26")
        check(
            "canonical destination",
            dest == vault / archive_name / "Areas" / "Design" / "2026-07-26-A-Standard.md",
            str(dest.relative_to(vault)),
        )
        check("pre-image bytes identical", dest.read_bytes() == original)
        check("source untouched", src.read_bytes() == original)

        # 2. Probe — a second pre-image the same day never overwrites the first.
        src.write_text("---\ntype: design\n---\n\n# Edited\n", encoding="utf-8")
        second = archive_preimage(src, vault_root=vault, date="2026-07-26")
        check("probe stepped to a free name", second != dest, second.name)
        check("first pre-image preserved", dest.read_bytes() == original)
        check("second pre-image is the current bytes",
              second.read_bytes() == src.read_bytes())

        # 3. slug -> CONFIG's <date>-<filename>-<version> shape.
        slugged = archive_preimage(src, "pre-merge", vault_root=vault, date="2026-07-26")
        check("slug lands as the version suffix",
              slugged.name.endswith("-pre-merge"), slugged.name)

        # 4. archive_root + mirror_tree — a caller-owned run bundle.
        bundle = vault / archive_name / "2026-07-26-run-bundle"
        mirrored = archive_preimage(
            src, vault_root=vault, archive_root=bundle, mirror_tree=True,
        )
        check("mirror_tree keeps the source-relative path",
              mirrored == bundle / "Areas" / "Design" / "A-Standard.md",
              str(mirrored.relative_to(vault)))

        # 5. Raise paths — the caller must be able to abort on each.
        try:
            archive_preimage(notes / "Does-Not-Exist.md", vault_root=vault)
            check("missing source raises", False, "no exception")
        except ArchiveFirstError as exc:
            check("missing source raises", True, str(exc)[:60])

        outside = Path(tmp) / "outside.md"
        outside.write_text("stray\n", encoding="utf-8")
        try:
            archive_preimage(outside, vault_root=vault)
            check("source outside the vault raises", False, "no exception")
        except ArchiveFirstError as exc:
            check("source outside the vault raises", True, str(exc)[:60])

        try:
            free_archive_name(dest, max_probes=1)
            check("exhausted probe raises", False, "no exception")
        except ArchiveFirstError as exc:
            check("exhausted probe raises", True, str(exc)[:60])

        # 6. is_filed — inbox drafts are the user's space, filed notes are not.
        draft = vault / inbox_name / "Draft.md"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text("draft\n", encoding="utf-8")
        check("filed note is filed", is_filed(src, vault_root=vault))
        check("inbox draft is not filed", not is_filed(draft, vault_root=vault))
        check("path outside the vault is not filed", not is_filed(outside, vault_root=vault))

    print(f"\n{'ALL PASS' if not failures else str(len(failures)) + ' FAILED'}: "
          f"archive_first unit tests")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_run_unit_tests())
