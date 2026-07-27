#!/usr/bin/env python3
"""Vendor-local copy of the kit's shared wikilink parser.

The vault-search daemon runs in its own venv, isolated from the kit's
`.claude/scripts/` tree, so it cannot import `scripts/wikilink_helpers.py`
directly. This is a stdlib-only vendor copy of that module's public API, kept
faithful to it so wikilink parsing stays identical across the kit and the
daemon. If the canonical helper changes, re-vendor this file.

Public API (mirrors scripts/wikilink_helpers.py):
    WIKILINK_RE                 embed-aware regex; group 1 = '!' embed marker
                                (or ''), group 2 = full interior string
    normalize_link_target(raw) -> str    bare basename from any interior
    extract_wikilinks(text)    -> list[str]   unique non-embed basenames
    matches_basename(interior, basename) -> bool
    rewrite_wikilink_interior(text, old, new) -> tuple[str, int]
"""
from __future__ import annotations

import re

# Embed-aware regex: matches ![[...]] and [[...]]
# Group 1: optional embed marker ('!' or empty string)
# Group 2: full interior string
WIKILINK_RE: re.Pattern = re.compile(r'(!?)\[\[([^\[\]]+)\]\]')


def normalize_link_target(raw: str) -> str:
    """Return bare basename from any wikilink interior.

    Strips: path prefix (Folder/), alias (|Alias), heading anchor (#Section),
    and explicit .md suffix.

    Examples:
      'Folder/File'       -> 'File'
      'File|Alias'        -> 'File'
      'File#Section'      -> 'File'
      'File#Section|Alt'  -> 'File'
      'File.md'           -> 'File'
    """
    # Strip path prefix
    if '/' in raw:
        raw = raw.rsplit('/', 1)[-1]
    # Strip alias
    if '|' in raw:
        raw = raw.split('|', 1)[0]
    # Strip heading anchor
    if '#' in raw:
        raw = raw.split('#', 1)[0]
    raw = raw.strip()
    # Strip explicit .md suffix
    if raw.lower().endswith('.md'):
        raw = raw[:-3]
    return raw.strip()


def _strip_code(text: str) -> str:
    """Drop fenced code blocks and inline code so example wikilinks inside
    template skeletons (e.g. the format-type notes' fenced frontmatter) are not
    mistaken for real body links."""
    text = re.sub(r"(?ms)^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", "", text)
    text = re.sub(r"`[^`\n]*`", "", text)
    return text


def extract_wikilinks(text: str) -> list[str]:
    """Return unique wikilink basenames found in text. Excludes embeds and any
    link inside a fenced code block or inline code (template/example skeletons)."""
    text = _strip_code(text)
    seen: set[str] = set()
    result: list[str] = []
    for embed_marker, interior in WIKILINK_RE.findall(text):
        if embed_marker:  # skip ![[...]] embeds
            continue
        basename = normalize_link_target(interior)
        if basename and basename not in seen:
            seen.add(basename)
            result.append(basename)
    return result


def matches_basename(wikilink_interior: str, basename: str) -> bool:
    """True if the wikilink interior refers to the given basename (case-sensitive stem match)."""
    return normalize_link_target(wikilink_interior) == basename


def rewrite_wikilink_interior(
    text: str, old_basename: str, new_basename: str
) -> tuple[str, int]:
    """Replace all wikilinks pointing at old_basename with new_basename.

    Handles both [[Old]] and ![[Old]] (embed) forms.
    Path prefix and .md suffix are dropped on rewrite — the new canonical form
    is [[New-Name#sec|alias]] because they existed only to disambiguate the
    now-renamed file.
    Returns (updated_text, replacement_count). Idempotent.
    """
    count = 0

    def _replace(m: re.Match) -> str:
        nonlocal count
        embed, interior = m.group(1), m.group(2)
        if not matches_basename(interior, old_basename):
            return m.group(0)
        # Rebuild interior: swap basename, preserve #heading and |alias
        # Split alias off first
        if '|' in interior:
            before_alias, alias = interior.split('|', 1)
            alias_part = f'|{alias}'
        else:
            before_alias, alias_part = interior, ''
        # Section anchor on target side
        if '#' in before_alias:
            _, section = before_alias.split('#', 1)
            section_part = f'#{section}'
        else:
            section_part = ''
        new_interior = f'{new_basename}{section_part}{alias_part}'
        count += 1
        return f'{embed}[[{new_interior}]]'

    updated = WIKILINK_RE.sub(_replace, text)
    return updated, count
