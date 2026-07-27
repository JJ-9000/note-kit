#!/usr/bin/env python3
"""
index_helpers.py
=====================

Structure-aware index insertion helpers for the filing agent.

The first-pass filing agent appended index entries at the end of the file,
which placed them AFTER sections like Glossary and the dataview block. The
correct behavior is to insert into the appropriate section (Reference Notes,
Reference, Sub-maps, etc.) before the next `##` header.

Public API:
    MalformedIndexError
        Raised when a file has unclosed frontmatter or is otherwise
        structurally invalid for insertion.

    normalize_link_target(raw: str) -> str
        Re-exported from wikilink_helpers. Strips path prefix, alias,
        heading anchor, and .md suffix from a wikilink interior.

    add_child_link_to_index(index_file: Path, child_link: str) -> bool
        Insert child_link (a wikilink basename) into the index's top-level
        wikilink list in sorted order. Returns True if added, False if already
        present (idempotent). Raises MalformedIndexError for unclosed
        frontmatter.

    insert_into_section(index_path, section_heading, entry_line, *, check_name=None)
        -> (inserted: bool, reason: str)
        Insert entry_line into the named section of index_path.

    find_best_section(index_path, child_type) -> str | None
        Return the first existing section heading that matches the preferred
        sections for child_type ('reference', 'index', 'snippet', 'session').

    ensure_entry(index_path, child_name, description, *, child_type, fallback_section)
        -> (inserted: bool, reason: str)
        Best-effort entry insertion using find_best_section + insert_into_section.

Usage from a filing script:
    from index_helpers import add_child_link_to_index, MalformedIndexError
    added = add_child_link_to_index(Path('References/Topic-Index.md'), 'New-Note')
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from frontmatter_helpers import read_text_strict, split_frontmatter, write_text
from wikilink_helpers import normalize_link_target


# ---- exceptions -------------------------------------------------------------


class MalformedIndexError(Exception):
    """Raised when a file has unclosed frontmatter or is otherwise
    structurally invalid for insertion operations.
    """


# ---- low-level wikilink helpers (non-embed) ---------------------------------

_INLINE_LINK_RE = re.compile(r"(?<!!)\[\[([^\[\]]+?)\]\]")


# ---- frontmatter parsing ----------------------------------------------------

def _body_start_index(content: str) -> int:
    """First body line index (into ``content.split("\\n")``) after any frontmatter.

    Delegates the fence detection to the shared frontmatter splitter, then counts
    the newlines the header span consumes. Raises MalformedIndexError when an
    opening ``---`` fence has no closing fence: the shared splitter treats that as
    a bodyless document, but index insertion needs the malformed block flagged
    (the contract the retired line-scanning `_split_frontmatter` carried).
    """
    fm = split_frontmatter(content)
    if fm.has_frontmatter:
        header = fm.opening + fm.inner + fm.closing
        return header.count("\n")
    first = content.splitlines()[:1]
    if first and first[0].strip() == "---":
        raise MalformedIndexError(
            "Unclosed frontmatter block: opening '---' found but no closing '---'."
        )
    return 0


# ---- add_child_link_to_index ------------------------------------------------

def add_child_link_to_index(index_file: Path, child_link: str) -> bool:
    """Insert child_link as a wikilink bullet into the index's top-level list.

    The top-level list is the bullet lines (`- [[...]]`) between the note's H1
    title and the first H2 (`##`) heading. When the body carries no H1, the zone
    opens at the body start; when it carries no H2, the zone runs to the end.
    Anchoring on the H1 keeps an inserted link below the title — defining the
    zone from the body start instead puts every insertion above the heading,
    which is how four covers came to carry link blocks above their own titles.

    Presence is checked across the WHOLE body, not only the insert zone, so a
    child already listed under a `##` section (or in a stray block above the
    title) is never added a second time.

    Insertion is in case-insensitive sorted order by basename. Returns True if
    a new line was inserted; False if child_link is already present (idempotent,
    comparison via normalize_link_target — case-insensitive).

    Handles edge cases:
      - Missing file: creates it, seeding minimal index frontmatter (type: index,
        tags, date) so the kit's own created artifact is not flagged
        `missing-frontmatter`, then the link as the first body line.
      - Frontmatter-only: writes body block below closing '---'.
      - No H2 in body: appends among existing list items (sorted).
      - Link already present (case-insensitive): returns False.
      - Malformed file (unclosed frontmatter): raises MalformedIndexError.
    """
    index_file = Path(index_file)
    if not index_file.exists():
        # Missing-file case: create with seeded minimal frontmatter + the link.
        date = datetime.now().strftime("%Y-%m-%d")
        write_text(
            index_file,
            "---\n"
            "type: index\n"
            "tags:\n"
            "  - index\n"
            f"date: {date}\n"
            "---\n\n"
            f"- [[{child_link}]]\n",
        )
        return True

    content = read_text_strict(index_file)
    body_start = _body_start_index(content)  # raises MalformedIndexError if unclosed
    # Split and rejoin on the file's dominant ending so an insertion into a CRLF
    # index never yields mixed line endings (the inserted line inherits the file's).
    nl = "\r\n" if "\r\n" in content else "\n"
    had_trailing_newline = content.endswith("\n")
    lines = content.split(nl)
    if had_trailing_newline and lines and lines[-1] == "":
        lines.pop()

    # The zone opens below the H1 title when there is one, so an inserted link
    # lands under the heading rather than above it.
    zone_start = body_start
    for i in range(body_start, len(lines)):
        if re.match(r'^#\s+', lines[i]):
            zone_start = i + 1
            break

    # Find list zone end: first H2 at or after the zone start
    h2_idx = None
    for i in range(zone_start, len(lines)):
        if re.match(r'^#{2}\s+', lines[i]):
            h2_idx = i
            break
    zone_end = h2_idx if h2_idx is not None else len(lines)

    # Normalise the incoming basename for comparison
    incoming_stem = normalize_link_target(child_link).lower()

    # Idempotency check: scan the WHOLE body — a child listed under a `##`
    # section, or in a block above the title, already counts as registered.
    for i in range(body_start, len(lines)):
        for m in _INLINE_LINK_RE.finditer(lines[i]):
            if normalize_link_target(m.group(1)).lower() == incoming_stem:
                return False

    # Collect existing bullet lines in the zone
    bullet_indices = [
        i for i in range(zone_start, zone_end)
        if lines[i].lstrip().startswith('- ') or lines[i].lstrip().startswith('* ')
    ]

    new_line = f"- [[{child_link}]]"

    # Find sorted insertion position among bullet lines
    insert_at = zone_end  # default: append at end of zone
    for bi in bullet_indices:
        line_stem = ''
        m = _INLINE_LINK_RE.search(lines[bi])
        if m:
            line_stem = normalize_link_target(m.group(1)).lower()
        if line_stem > incoming_stem:
            insert_at = bi
            break
        insert_at = bi + 1  # comes after this bullet

    # If the zone holds no bullets yet, open the list at the top of the zone
    # (below the H1 when there is one).
    if not bullet_indices:
        insert_at = zone_start

    lines.insert(insert_at, new_line)

    out = nl.join(lines)
    if had_trailing_newline:
        out += nl
    write_text(index_file, out)
    return True


# ---- section-aware helpers --------------------------------------------------

# Preferred section headings per child type, in priority order.
# Matched case-insensitively; first match wins.
DEFAULT_SECTIONS = {
    "reference": ["Reference Notes", "Reference", "References", "Notes"],
    "index": ["Sub-maps", "Sub-Indexes", "Sub-maps / Sub-Indexes"],
    "snippet": ["Python snippets", "Snippets", "Code snippets"],
    "session": ["Session Logs", "Sessions"],
}


def _find_section_bounds(lines: list[str], heading: str) -> tuple[int, int] | None:
    """Return (heading_line_idx, section_end_idx) for the named section.

    Section end = the line just before the next `##` or `#` header at the same
    depth, or end-of-file if no following header. Excludes trailing blank lines.
    """
    heading_lc = heading.lower().strip()
    heading_idx = None
    heading_depth = 2
    for i, line in enumerate(lines):
        m = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if not m:
            continue
        depth = len(m.group(1))
        title = m.group(2).strip().lower()
        if title == heading_lc:
            heading_idx = i
            heading_depth = depth
            break
    if heading_idx is None:
        return None

    end_idx = len(lines)
    for j in range(heading_idx + 1, len(lines)):
        m = re.match(r"^(#{1,6})\s+", lines[j])
        if m and len(m.group(1)) <= heading_depth:
            end_idx = j
            break

    # Trim trailing blank lines from the section
    while end_idx > heading_idx + 1 and not lines[end_idx - 1].strip():
        end_idx -= 1

    return heading_idx, end_idx


def insert_into_section(
    index_path: Path | str,
    section_heading: str,
    entry_line: str,
    *,
    check_name: str | None = None,
) -> tuple[bool, str]:
    """Insert `entry_line` into the named section of `index_path`.

    Idempotent: if a line in the section already contains `[[check_name]]`
    (or, if check_name is omitted, any wikilink from entry_line), skip insert.

    Returns (inserted, reason).
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return False, f"Index not found: {index_path}"

    content = read_text_strict(index_path)
    # Split and rejoin on the file's dominant ending so an insertion into a CRLF
    # index never yields mixed line endings (the inserted line inherits the file's).
    nl = "\r\n" if "\r\n" in content else "\n"
    had_trailing_newline = content.endswith("\n")
    lines = content.split(nl)
    if had_trailing_newline and lines and lines[-1] == "":
        lines.pop()

    bounds = _find_section_bounds(lines, section_heading)
    if bounds is None:
        return False, f"section not found: '{section_heading}'"
    heading_idx, end_idx = bounds

    # Idempotency: compare normalized basenames across every wikilink form so
    # `[[Foo]]`, `[[Foo#Section]]`, `[[02-Areas/Foo]]`, and `[[Foo.md|alias]]`
    # all dedup against each other. Without normalization, two of these forms
    # in the same section produce duplicate entries.
    if check_name is None:
        m = _INLINE_LINK_RE.search(entry_line)
        check_name = normalize_link_target(m.group(1)) if m else None
    if check_name:
        for k in range(heading_idx + 1, end_idx):
            for link_m in _INLINE_LINK_RE.finditer(lines[k]):
                existing = normalize_link_target(link_m.group(1))
                if existing == check_name:
                    return False, f"already listed in '{section_heading}': [[{check_name}]]"

    # Find insert position: after the last existing bullet in the section,
    # or immediately after the heading if the section has no bullets yet.
    insert_at = heading_idx + 1
    for k in range(heading_idx + 1, end_idx):
        stripped = lines[k].lstrip()
        if stripped.startswith("- ") or stripped.startswith("* "):
            insert_at = k + 1

    lines.insert(insert_at, entry_line)

    out = nl.join(lines)
    if had_trailing_newline:
        out += nl
    write_text(index_path, out)
    return True, f"inserted into '{section_heading}' at line {insert_at + 1}"


def find_best_section(index_path: Path | str, child_type: str) -> str | None:
    """Return the first existing section heading in `index_path` that matches
    one of the preferred sections for `child_type`. Returns None if none match.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return None
    content = read_text_strict(index_path)
    headings = {
        m.group(1).strip().lower(): m.group(1).strip()
        for m in re.finditer(r"^#{2,6}\s+(.+?)\s*$", content, re.MULTILINE)
    }
    for candidate in DEFAULT_SECTIONS.get(child_type, []):
        if candidate.lower() in headings:
            return headings[candidate.lower()]
    return None


def ensure_entry(
    index_path: Path | str,
    child_name: str,
    description: str,
    *,
    child_type: str = "reference",
    fallback_section: str | None = None,
) -> tuple[bool, str]:
    """Best-effort entry insertion: find the best matching section for the
    child_type and insert `- [[child_name]] — description`. Returns
    (inserted, reason).

    If no preferred section exists and fallback_section is provided, try that.
    If still no match, return (False, ...) rather than appending at end of
    file (which was the original bug).
    """
    entry = f"- [[{child_name}]] — {description}" if description else f"- [[{child_name}]]"

    section = find_best_section(index_path, child_type)
    if section is None and fallback_section:
        section = fallback_section
    if section is None:
        return False, (
            f"no matching section in {Path(index_path).name} for type '{child_type}'; "
            f"no fallback provided. Refusing to append at EOF."
        )
    return insert_into_section(index_path, section, entry, check_name=child_name)


if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(description="Index entry helper")
    p.add_argument("index", help="path to index file")
    p.add_argument("child_name", help="wikilink target name")
    p.add_argument("description", nargs="?", default="", help="one-line description")
    p.add_argument("--type", default="reference", choices=list(DEFAULT_SECTIONS.keys()))
    p.add_argument("--section", default=None, help="force a specific section heading")
    args = p.parse_args()

    if args.section:
        entry = f"- [[{args.child_name}]] — {args.description}" if args.description else f"- [[{args.child_name}]]"
        ok, reason = insert_into_section(args.index, args.section, entry, check_name=args.child_name)
    else:
        ok, reason = ensure_entry(args.index, args.child_name, args.description, child_type=args.type)
    print(f"{'OK' if ok else 'SKIP'}: {reason}")
