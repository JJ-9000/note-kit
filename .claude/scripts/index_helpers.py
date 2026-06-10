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
    added = add_child_link_to_index(Path('03-Reference/Houdini-Index.md'), 'New-Note')
"""
from __future__ import annotations

import re
from pathlib import Path

from wikilink_helpers import normalize_link_target, WIKILINK_RE


# ---- exceptions -------------------------------------------------------------


class MalformedIndexError(Exception):
    """Raised when a file has unclosed frontmatter or is otherwise
    structurally invalid for insertion operations.
    """


# ---- low-level wikilink helpers (non-embed) ---------------------------------

_INLINE_LINK_RE = re.compile(r"(?<!!)\[\[([^\[\]]+?)\]\]")


# ---- frontmatter parsing ----------------------------------------------------

def _split_frontmatter(lines: list[str]) -> tuple[int, int]:
    """Return (fm_start, body_start) line indices.

    fm_start = index of opening '---' (0 if present, else -1 for no FM).
    body_start = first line after closing '---'.

    Raises MalformedIndexError if opening '---' exists but closing '---' does
    not appear within the first 200 lines.
    """
    if not lines or lines[0].rstrip() != '---':
        return -1, 0
    for i in range(1, min(len(lines), 200)):
        if lines[i].rstrip() == '---':
            return 0, i + 1
    raise MalformedIndexError(
        "Unclosed frontmatter block: opening '---' found but no closing '---' "
        "within first 200 lines."
    )


# ---- add_child_link_to_index ------------------------------------------------

def add_child_link_to_index(index_file: Path, child_link: str) -> bool:
    """Insert child_link as a wikilink bullet into the index's top-level list.

    The top-level list is defined as bullet lines (`- [[...]]`) that appear
    in the body above the first H2 (`##`) heading. If no H2 exists, the entire
    body is treated as the list zone.

    Insertion is in case-insensitive sorted order by basename. Returns True if
    a new line was inserted; False if child_link is already present (idempotent,
    comparison via normalize_link_target — case-insensitive).

    Handles edge cases:
      - Empty file: writes link as first body line.
      - Frontmatter-only: writes body block below closing '---'.
      - No H2 in body: appends among existing list items (sorted).
      - Link already present (case-insensitive): returns False.
      - Malformed file (unclosed frontmatter): raises MalformedIndexError.
    """
    index_file = Path(index_file)
    if not index_file.exists():
        # Empty-file case: create with just the link.
        index_file.write_text(f"- [[{child_link}]]\n", encoding="utf-8")
        return True

    content = index_file.read_text(encoding="utf-8")
    had_trailing_newline = content.endswith("\n")
    lines = content.split("\n")
    if had_trailing_newline and lines and lines[-1] == "":
        lines.pop()

    _, body_start = _split_frontmatter(lines)  # raises MalformedIndexError if unclosed

    # Find list zone: body lines before first H2
    h2_idx = None
    for i in range(body_start, len(lines)):
        if re.match(r'^#{2}\s+', lines[i]):
            h2_idx = i
            break
    zone_end = h2_idx if h2_idx is not None else len(lines)

    # Normalise the incoming basename for comparison
    incoming_stem = normalize_link_target(child_link).lower()

    # Idempotency check: scan zone for any wikilink that normalises to same basename
    for i in range(body_start, zone_end):
        for m in _INLINE_LINK_RE.finditer(lines[i]):
            if normalize_link_target(m.group(1)).lower() == incoming_stem:
                return False

    # Collect existing bullet lines in the zone
    bullet_indices = [
        i for i in range(body_start, zone_end)
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

    # If zone was empty (no bullets) and body_start < zone_end, insert at body_start
    if not bullet_indices:
        insert_at = body_start

    lines.insert(insert_at, new_line)

    out = "\n".join(lines)
    if had_trailing_newline:
        out += "\n"
    index_file.write_text(out, encoding="utf-8")
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

    content = index_path.read_text(encoding="utf-8")
    had_trailing_newline = content.endswith("\n")
    lines = content.split("\n")
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

    out = "\n".join(lines)
    if had_trailing_newline:
        out += "\n"
    index_path.write_text(out, encoding="utf-8")
    return True, f"inserted into '{section_heading}' at line {insert_at + 1}"


def find_best_section(index_path: Path | str, child_type: str) -> str | None:
    """Return the first existing section heading in `index_path` that matches
    one of the preferred sections for `child_type`. Returns None if none match.
    """
    index_path = Path(index_path)
    if not index_path.exists():
        return None
    content = index_path.read_text(encoding="utf-8")
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
