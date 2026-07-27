"""
frontmatter_helpers.py
======================

The kit's one parse/split/edit/serialize API for a markdown note's YAML
frontmatter, plus the safe read/write primitives every scripted rewrite runs
through. It collapses the private per-script variants (`_split_doc`/`_fm_dict`/
`_set_fm_field`, `_read_md_text`, ad-hoc `open(..., "w")`) into a single home.

Two design commitments make it safe to adopt everywhere:

  - **Byte-exact round-trip.** `split_frontmatter(text)` keeps the opening
    fence, the raw frontmatter block, the closing fence, and the body as
    separate verbatim spans, so `Frontmatter.serialize()` with no edit returns
    the original text byte-for-byte — CRLF, blank lines, comments, and key
    order intact. A field edit is a targeted raw-block rewrite (the
    `bifurcate_plan._set_fm_field` pattern): only the edited field's line
    changes; its line terminator, every other key, comment, and quote survive.

  - **No lossy decode, no CRLF re-expansion.** Reads decode strict UTF-8 from
    raw bytes (the `rename_with_link_integrity._read_md_text` pattern): a
    non-UTF-8 file is skipped and reported, never rewritten with U+FFFD
    replacement bytes; and `read_text_strict` avoids the universal-newline
    translation `Path.read_text` performs, so `\r\n` survives into the string.
    Writes use ``newline="\n"`` so a `\n` in the string is never re-expanded to
    `\r\n` on Windows.

`structured_rewrite` is CONFIG § Versioning and archiving discipline as one
importable function: archive the source to `<archive>/<source-path>/<date>-
<filename>` first, confirm the copy, apply the write, verify a structural
invariant, and restore the archived copy on failure.

Stdlib only for parse/split/edit/serialize and the read/write primitives.
`Frontmatter.to_dict` needs PyYAML; it is imported lazily so the rest of the
module loads in a stdlib-only context (e.g. a vendor copy in the daemon).

Public API
----------
split_frontmatter(text) -> Frontmatter
Frontmatter        dataclass: .serialize / .get / .set_field / .to_dict
set_field_in_text(text, key, value) -> str
read_text_strict(path) -> str                 raises NonUtf8Error
read_text_or_none(path) -> str | None         skip-and-report variant
write_text(path, text) -> None                newline="\n"
structured_rewrite(path, new_text, *, vault_root, invariant, ...) -> Path
NonUtf8Error, StructuralInvariantError, OutsideVaultError
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class NonUtf8Error(ValueError):
    """Raised when a file's bytes are not valid UTF-8.

    A strict-decode probe raises this instead of substituting U+FFFD, so a
    caller skips the file and reports it rather than rewriting mojibake over the
    original bytes.
    """


class StructuralInvariantError(RuntimeError):
    """Raised when a structured rewrite's post-write invariant fails.

    By the time this is raised the archived pre-image has already been restored
    over the target, so the file on disk matches its pre-write bytes.
    """


class OutsideVaultError(ValueError):
    """Raised when a structured rewrite's source does not live under the vault root.

    The archive destination is `<archive>/<source-path>/…` where `<source-path>`
    is the source relative to the vault root, so a source outside the vault has
    no well-defined archive location. Raised before any write, so the target is
    untouched.
    """


_FENCE = "---"


# ---------------------------------------------------------------------------
# Split / parse / serialize
# ---------------------------------------------------------------------------

@dataclass
class Frontmatter:
    """A note split into verbatim spans that reserialize byte-for-byte.

    Fields:
        opening         opening fence line including its newline; '' when the
                        document has no frontmatter.
        inner           raw text between the fences (each line with its own
                        terminator preserved); '' when absent.
        closing         closing fence line including its newline; '' when absent.
        body            everything after the closing fence, verbatim.
        has_frontmatter True when a well-formed fenced block was found.

    ``opening + inner + closing + body`` equals the original text exactly, so
    an unedited serialize is a byte-identical round-trip.
    """

    opening: str
    inner: str
    closing: str
    body: str
    has_frontmatter: bool

    def serialize(self) -> str:
        """Reassemble the document. Byte-identical to the source when unedited."""
        return self.opening + self.inner + self.closing + self.body

    def get(self, key: str) -> Optional[str]:
        """Return the trimmed value of the first top-level ``key:`` line, or None.

        Reads the raw block, so it sees the value exactly as written (quoting,
        wikilinks). Does not descend into nested/block values.
        """
        pat = re.compile(rf"^{re.escape(key)}\s*:(.*)$", re.MULTILINE)
        m = pat.search(self.inner)
        if m is None:
            return None
        return m.group(1).strip()

    def set_field(self, key: str, value: str) -> bool:
        """Set ``key: value`` in the raw frontmatter, preserving the rest verbatim.

        Replaces the first existing ``key:`` line in place — keeping that line's
        own terminator (``\\n`` or ``\\r\\n``) — or appends a new line when the
        key is absent. Operating on the raw text (never a re-dumped dict) keeps
        key order, comments, and wikilink quoting intact, so only the edited
        field's line changes. Creates a minimal fenced block when the document
        had no frontmatter. Returns True.
        """
        if not self.has_frontmatter:
            self.opening = f"{_FENCE}\n"
            self.closing = f"{_FENCE}\n"
            self.inner = f"{key}: {value}\n"
            self.has_frontmatter = True
            return True

        lines = self.inner.splitlines(keepends=True)
        pat = re.compile(rf"^{re.escape(key)}\s*:")
        for i, line in enumerate(lines):
            if pat.match(line):
                stripped = line.rstrip("\r\n")
                terminator = line[len(stripped):]  # '', '\n', or '\r\n'
                if not terminator:
                    terminator = "\n"
                lines[i] = f"{key}: {value}{terminator}"
                self.inner = "".join(lines)
                return True
        # Append: keep the block newline-terminated before the new line.
        if self.inner and not self.inner.endswith(("\n", "\r")):
            self.inner += "\n"
        self.inner += f"{key}: {value}\n"
        return True

    def to_dict(self) -> dict:
        """Parse the frontmatter to a dict via PyYAML (imported lazily).

        Returns an empty dict when the block is empty, not a mapping, or
        malformed (a YAML parse error is swallowed to {}). Raises RuntimeError
        when PyYAML is unavailable — the byte-exact machinery does not need it,
        only this convenience does.
        """
        if not self.inner.strip():
            return {}
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError(
                "Frontmatter.to_dict requires PyYAML, which is not installed. "
                "Use .get / .set_field for stdlib-only field access."
            ) from exc
        try:
            data = yaml.safe_load(self.inner)
        except yaml.YAMLError:
            return {}
        return data if isinstance(data, dict) else {}


def split_frontmatter(text: str) -> Frontmatter:
    """Split ``text`` into fence/frontmatter/fence/body spans.

    A frontmatter block is recognized only when the document's first line is a
    bare ``---`` fence and a later line is exactly ``---``. Splitting keeps every
    line's terminator (via ``splitlines(keepends=True)``) so the spans
    concatenate back to the source byte-for-byte. When no block is found the
    whole text is the body and ``has_frontmatter`` is False.
    """
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != _FENCE:
        return Frontmatter("", "", "", text, has_frontmatter=False)

    closing_idx: Optional[int] = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            closing_idx = i
            break
    if closing_idx is None:
        # An opening fence with no close is not frontmatter — treat as body.
        return Frontmatter("", "", "", text, has_frontmatter=False)

    return Frontmatter(
        opening=lines[0],
        inner="".join(lines[1:closing_idx]),
        closing=lines[closing_idx],
        body="".join(lines[closing_idx + 1:]),
        has_frontmatter=True,
    )


def set_field_in_text(text: str, key: str, value: str) -> str:
    """Convenience: split ``text``, set ``key: value``, reserialize.

    Only the edited field's line (plus a created fence block when the note had
    no frontmatter) differs from the input.
    """
    fm = split_frontmatter(text)
    fm.set_field(key, value)
    return fm.serialize()


# ---------------------------------------------------------------------------
# Safe read / write primitives
# ---------------------------------------------------------------------------

def read_text_strict(path: Path) -> str:
    """Return the file's text, decoded strict UTF-8 from raw bytes.

    Reads bytes and decodes rather than calling ``Path.read_text`` so no
    universal-newline translation happens: ``\\r\\n`` survives verbatim into the
    string, which the byte-exact round-trip depends on. Raises NonUtf8Error when
    the bytes are not valid UTF-8 — never substitutes replacement characters.
    """
    path = Path(path)
    data = path.read_bytes()
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NonUtf8Error(f"{path}: not valid UTF-8 ({exc})") from exc


def read_text_or_none(path: Path) -> Optional[str]:
    """Strict-UTF-8 text of ``path``, or None when the file cannot be read as
    text — its bytes are not valid UTF-8, or it is no longer there to read.

    The skip-and-report variant: a caller collects the None-returning paths and
    surfaces them, so a note the rewrite pass could not safely write back is
    never scanned with a lossy decode.

    A vanished file returns None like any other unreadable one. A vault under an
    external sync actor loses and replays files mid-walk, so the gap between
    listing a path and reading it is real: the 2026-07-26 19:22Z janitor pass
    died on a ``FileNotFoundError`` from a file deleted inside that gap and wrote
    no run-end at all. ``OSError`` covers the whole family — the file gone
    (``FileNotFoundError``), its parent directory gone (``NotADirectoryError``),
    or the read refused (``PermissionError``) — and each one is a skip, which is
    this function's contract, rather than the end of the pass.
    """
    try:
        return read_text_strict(path)
    except NonUtf8Error:
        return None
    except OSError:
        return None


def write_text(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` as UTF-8 with ``newline="\\n"``.

    ``newline="\\n"`` disables newline translation, so a ``\\n`` in the string is
    written as a single LF (never re-expanded to ``\\r\\n`` on Windows) and any
    existing ``\\r\\n`` in the string is preserved as-is.
    """
    with open(Path(path), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)


# ---------------------------------------------------------------------------
# Structured rewrite — CONFIG § Versioning and archiving discipline
# ---------------------------------------------------------------------------

def _archive_dirname() -> str:
    """The archive folder literal from CONFIG, or 'Archive' when unresolvable."""
    try:
        import config_variables
        return config_variables._folder_by_semantic("archive")
    except Exception:
        return "Archive"


def archive_dest_for(
    source: Path,
    vault_root: Path,
    *,
    archive_root: Optional[Path] = None,
    version: Optional[str] = None,
    date: Optional[str] = None,
    mirror_tree: bool = False,
) -> Path:
    """Resolve the archive destination for ``source`` per CONFIG § Versioning.

    Default: ``<archive>/<source-path>/<date>-<filename>`` where ``<source-path>``
    is the source's parent directory relative to the vault root. A ``version``
    appends ``-<version>`` (CONFIG's full ``<date>-<filename>-<version>`` shape).
    ``date`` defaults to today (UTC, ``YYYY-MM-DD``).

    ``mirror_tree`` (opt-in) mirrors the EXACT source-relative path under
    ``archive_root`` with no date/version stamp on the filename — for a caller
    that routes many pre-images into one already-dated bundle directory (e.g. a
    per-run ``<archive>/<date>-audit-apply/`` bundle): the source-relative parent
    prevents basename collisions, and the bundle folder already carries the date,
    so a second ``<date>-`` prefix would be redundant. Requires ``archive_root``;
    default ``mirror_tree=False`` preserves the dated per-source-path layout, so
    every existing caller is unchanged.
    """
    source = Path(source).resolve()
    vault_root = Path(vault_root).resolve()
    if archive_root is None:
        archive_root = vault_root / _archive_dirname()
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        rel = source.relative_to(vault_root)
    except ValueError as exc:
        raise OutsideVaultError(
            f"{source}: source must live under the vault root {vault_root} "
            f"for an archive-first rewrite; it does not."
        ) from exc
    if mirror_tree:
        # Exact source-relative path inside the bundle (no date/version stamp).
        return archive_root / rel
    name = f"{date}-{source.name}" + (f"-{version}" if version else "")
    return archive_root / rel.parent / name


def structured_rewrite(
    path: Path,
    new_text: str,
    *,
    vault_root: Path,
    invariant: Callable[[str], bool],
    archive_root: Optional[Path] = None,
    version: Optional[str] = None,
    date: Optional[str] = None,
    mirror_tree: bool = False,
    preserve_existing: bool = False,
) -> Path:
    """Archive-first, write, verify, restore-on-failure — one importable rewrite.

    Steps (CONFIG § Versioning and archiving discipline):
      1. Copy the source bytes to the archive destination (default
         ``<archive>/<source-path>/<date>-<filename>``; with ``archive_root`` +
         ``mirror_tree`` the pre-image mirrors the exact source-relative path
         inside a per-run bundle) and confirm the copy exists and its size
         matches before proceeding.
      2. Write ``new_text`` through :func:`write_text` (``newline="\\n"``).
      3. Call ``invariant(new_text)``; a False return or a raised exception is a
         verification failure.
      4. On failure, restore the target to its pre-write state (byte-identical)
         and raise StructuralInvariantError.

    ``preserve_existing`` (first-write-wins): when the archive destination already
    holds a pre-image for this source (the common case of a per-run bundle path
    written by an earlier rewrite of the same file in the same run), keep it — do
    NOT overwrite the pristine original with a later intermediate. Whole-run
    rollback then restores the true starting state. Restore-on-failure always
    reads the PER-CALL in-memory pre-image (``original``), never the on-disk
    archive, so a later write's rollback restores that write's own pre-state even
    when the bundle retains an earlier file. Default False preserves the prior
    behaviour (every call writes its own archive dest).

    Returns the archive destination path on success.
    """
    path = Path(path).resolve()
    original = path.read_bytes()

    dest = archive_dest_for(
        path, vault_root, archive_root=archive_root, version=version, date=date,
        mirror_tree=mirror_tree,
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    if preserve_existing and dest.is_file():
        # First-write-wins: the bundle already holds this source's pristine
        # pre-image from an earlier rewrite this run; retain it untouched.
        pass
    else:
        dest.write_bytes(original)
        if not dest.is_file() or dest.stat().st_size != len(original):
            raise StructuralInvariantError(
                f"{path}: archive copy to {dest} failed size verification "
                f"({dest.stat().st_size if dest.is_file() else 'missing'} vs "
                f"{len(original)} bytes) — refusing to write."
            )

    write_text(path, new_text)

    try:
        ok = bool(invariant(new_text))
        reason = "invariant returned False"
    except Exception as exc:  # invariant may raise its own diagnostic
        ok = False
        reason = f"invariant raised {exc!r}"

    if not ok:
        path.write_bytes(original)  # restore the per-call in-memory pre-image
        raise StructuralInvariantError(
            f"{path}: post-write invariant failed ({reason}); "
            f"restored to pre-write state."
        )

    return dest
