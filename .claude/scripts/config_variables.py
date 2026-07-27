"""
config_variables.py
========================

Parses CONFIG.md at import time and exposes typed Python constants for the
note-kit scripts. Hard-fails on any parse error — no silent fallbacks.

The ## Folders table carries a `wildcard` column (`<inbox>`, `<archive>`, …):
_parse_folders expects that column and RoutingRow stores it, and
_folder_by_semantic resolves against the wildcard first, then falls back to the
numeric-prefix-stripped basename. The doc-only CONFIG tables (Agent
responsibilities, Helper-script automation) are intentionally not parsed here —
of the documentation tables, only the File-handling frontmatter-exception table
feeds FILE_HANDLING.

Module-level constants
----------------------
FILE_HANDLING : FileHandling
TYPES         : dict[str, TypeRow]       keyed by canonical type key
FOLDER_ROUTING: dict[str, RoutingRow]   keyed by folder name string
SUBFOLDERS    : dict[str, SubfolderRow] keyed by subfolder name
CONTENT_LIFECYCLE : dict[str, ContentLifecycleRow] keyed by transient class (§ Content lifecycle)
TAGS          : dict[str, TagRow]       keyed by canonical tag
ACTIONS       : dict[str, ActionRow]    keyed by action name
SKILL_SLUGS   : dict[str, SkillSlugRow] keyed by slug

CANONICAL_TYPE_KEYS : frozenset[str]
CANONICAL_TAG_KEYS  : frozenset[str]
SCAN_EXCLUDE_DIRS   : frozenset[str]     named dot-directories the walk skips

Helpers
-------
normalize_tag(value: str) -> str | None
levenshtein_le1(a: str, b: str) -> bool
folder_for_wildcard(wildcard: str) -> str | None
is_excluded_dir(name: str) -> bool       dot-directory scan-exclusion test
token_path(token: str, fallback: str | None = None) -> str
                                         vault-relative path for a § Folders
                                         token-table token (`<user-queue>`,
                                         `<logs>`, `<inbox-assets>`, …)
resolve_vault_root(cli_root=None, ...) -> Path | None
                                         the one vault-root resolver: cli arg,
                                         NOTE_KIT_VAULT_ROOT, JANITOR_VAULT_ROOT,
                                         then an installed-layout walk-up

Module-level surfaces
---------------------
PARSE_SKIPS : list[str]                  malformed table rows _parse_table
                                         skipped, reported not silently dropped
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_KIT_ROOT = _SCRIPTS_DIR.parent
# Allow an explicit override so the parser can be pointed at any CONFIG.md
# (e.g. to validate a candidate config against a reference copy). Falls back to
# the CONFIG.md beside this script's kit root.
_CONFIG_OVERRIDE = os.environ.get("NOTE_KIT_CONFIG")
_CONFIG = Path(_CONFIG_OVERRIDE).resolve() if _CONFIG_OVERRIDE else (_KIT_ROOT / "CONFIG.md")


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileException:
    pattern: str
    required_frontmatter: tuple[str, ...]  # empty tuple when none
    reason: str


@dataclass(frozen=True)
class FileHandling:
    global_frontmatter: tuple[str, ...]    # e.g. ('type', 'tags', 'date')
    inbox_additions: dict[str, str]        # e.g. {'reviewed': 'false', 'status': 'draft'}
    orphan_asset_folder: Path
    exceptions: tuple[FileException, ...]


@dataclass(frozen=True)
class TypeRow:
    key: str
    pattern: str
    additional_frontmatter: tuple[str, ...]
    description: str
    default_home: str = ""   # `default-home` cell (v005+); '' when absent


@dataclass(frozen=True)
class RoutingRow:
    folder: str
    wildcard: str                          # e.g. '<inbox>' ('' if unset)
    type_defaults: tuple[str, ...]
    hands_off_patterns: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class SubfolderRow:
    subfolder: str
    type_defaults: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class ContentLifecycleRow:
    """One § Content lifecycle class: when it retires, to where, and who moves it."""
    cls: str
    trigger: str
    destination: str
    actor: str
    cadence: str


@dataclass(frozen=True)
class TagRow:
    tag: str
    aliases: tuple[str, ...]
    scope: str
    description: str


@dataclass(frozen=True)
class ActionRow:
    action: str
    shape: str
    authorized_agents: tuple[str, ...]
    description: str


@dataclass(frozen=True)
class SkillSlugRow:
    slug: str
    inbox_container: bool   # True if this skill writes a multi-file container
    description: str


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class ConfigLookupError(Exception):
    """Raised when a semantic lookup in config data fails."""


# ---------------------------------------------------------------------------
# Skip surface — malformed table rows are reported, never silently dropped
# ---------------------------------------------------------------------------

# Every malformed row _parse_table skips (cell count disagreeing with the
# header) is recorded here at import time so a caller can surface it. A silent
# drop shrinks a parsed vocabulary — a lost tag, action, or slug — which a later
# fuzzy match can then rewrite to a wrong neighbor; recording the skip makes the
# shrink visible. The kit's own clean CONFIG.md leaves this empty (the __main__
# self-test asserts it), and config_shape.py refuses the same rows at sync time.
PARSE_SKIPS: list[str] = []


# ---------------------------------------------------------------------------
# General-purpose markdown table parser
# ---------------------------------------------------------------------------

def _parse_table(
    text: str,
    section_heading: str,
    expected_columns: list[str],
    skips: Optional[list[str]] = None,
) -> list[dict]:
    """Find an H2 section by exact heading and parse its first markdown table.

    Reads rows from the header until the table ends — a blank line or a non-pipe
    line (markdown terminates a table there) — so a section holding more than one
    table (e.g. § Folders' roots table followed by its token table) yields only
    the first table's rows, and a recorded skip means a genuinely malformed row
    inside that table, never a row belonging to a later table. Requires every
    name in `expected_columns` to be PRESENT in the header (a subset check, not
    exact equality) and parses each row against the table's own header, so a
    CONFIG table that gains an extra column — e.g. `## Types` adding
    `default-home` in v005 — still parses: the caller indexes the columns it
    needs by name and ignores the rest. A renamed or removed required column
    still hard-fails. Returns a list of dicts keyed by the actual header names.

    A data row whose cell count disagrees with the header is skipped and the
    skip is recorded — appended to the module-level PARSE_SKIPS and, when given,
    to the caller's `skips` list — so the dropped row is reported rather than
    silently shrinking the parsed vocabulary.
    """
    # Locate the section
    heading_pattern = re.compile(
        r"^## " + re.escape(section_heading) + r"\s*$",
        re.MULTILINE,
    )
    m = heading_pattern.search(text)
    if m is None:
        raise RuntimeError(f"Section '## {section_heading}' not found in CONFIG.md")

    # Extract text until the next H2 or EOF
    after = text[m.end():]
    next_h2 = re.search(r"^## ", after, re.MULTILINE)
    section_text = after[: next_h2.start()] if next_h2 else after

    # Find the table header row
    lines = section_text.splitlines()
    header_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            # First pipe-row is the header
            header_idx = i
            break

    if header_idx is None:
        raise RuntimeError(
            f"No markdown table found in section '## {section_heading}'"
        )

    def parse_row(line: str) -> list[str]:
        # Strip outer pipes and split
        inner = line.strip()
        if inner.startswith("|"):
            inner = inner[1:]
        if inner.endswith("|"):
            inner = inner[:-1]
        return [cell.strip() for cell in inner.split("|")]

    header_cells = parse_row(lines[header_idx])
    missing = [c for c in expected_columns if c not in header_cells]
    if missing:
        raise RuntimeError(
            f"Section '## {section_heading}': header {header_cells!r} is missing "
            f"required column(s) {missing!r}"
        )

    # Skip the separator row (line of dashes)
    data_start = header_idx + 2  # +1 for separator

    rows = []
    for line in lines[data_start:]:
        stripped = line.strip()
        if not stripped or not stripped.startswith("|"):
            # A blank or non-pipe line ends the table (markdown rule); stop here
            # so a later table in the same section is not swept into this parse.
            break
        cells = parse_row(stripped)
        if len(cells) != len(header_cells):
            # Skip a malformed row (cell count must match the header), but
            # report it — never a silent vocabulary shrink.
            msg = (
                f"§ {section_heading}: skipped a row with {len(cells)} cell(s) "
                f"(header has {len(header_cells)}): {stripped!r}"
            )
            PARSE_SKIPS.append(msg)
            if skips is not None:
                skips.append(msg)
            continue
        rows.append(dict(zip(header_cells, cells)))

    return rows


def _strip_backticks(value: str) -> str:
    """Drop surrounding backticks from a markdown-code cell (e.g. `<inbox>`)."""
    return value.strip().strip("`").strip()


def _table_header(text: str, section_heading: str) -> list[str]:
    """Return the column names of the first markdown table under an H2 section.

    Lets a parser adapt to a column-renamed or column-reordered table rather
    than hard-failing: the caller reads the actual header, maps it onto the
    layout it knows, and passes it back to _parse_table as the expected columns.
    """
    heading_pattern = re.compile(
        r"^## " + re.escape(section_heading) + r"\s*$", re.MULTILINE
    )
    m = heading_pattern.search(text)
    if m is None:
        raise RuntimeError(f"Section '## {section_heading}' not found in CONFIG.md")
    after = text[m.end():]
    next_h2 = re.search(r"^## ", after, re.MULTILINE)
    section_text = after[: next_h2.start()] if next_h2 else after
    for line in section_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("|") and stripped.endswith("|"):
            inner = stripped[1:-1]
            return [cell.strip() for cell in inner.split("|")]
    raise RuntimeError(
        f"No markdown table found in section '## {section_heading}'"
    )


# ---------------------------------------------------------------------------
# Section parsers
# ---------------------------------------------------------------------------

def _parse_types(text: str) -> dict[str, TypeRow]:
    columns = ["type", "pattern", "additional-frontmatter", "description"]
    rows = _parse_table(text, "Types", columns)
    result: dict[str, TypeRow] = {}
    for row in rows:
        key = row["type"].lower().strip()
        additional = tuple(
            f.strip() for f in row["additional-frontmatter"].split(",")
            if f.strip()
        )
        result[key] = TypeRow(
            key=key,
            pattern=row["pattern"],
            additional_frontmatter=additional,
            description=row["description"],
            default_home=row.get("default-home", "").strip(),
        )
    return result


def _parse_folders(text: str) -> dict[str, RoutingRow]:
    """Parse the ## Folders table into RoutingRow records keyed by literal folder.

    The table's columns and column order are read from its own header row, not
    assumed, so a rename of a column heading in CONFIG.md does not silently
    mis-map cells. Two layouts are accepted:

      v005:  | wildcard | literal | type-defaults | hands-off          | description |
      older: | folder   | wildcard | type-defaults | hands-off-patterns | description |

    In both, the on-disk folder name (`literal` in v005, `folder` in the older
    layout) becomes RoutingRow.folder and the dict key; the `<token>` wildcard
    becomes RoutingRow.wildcard. Backticks are stripped from every cell —
    CONFIG-v005 writes the literal, wildcard, and each hands-off token wrapped in
    backticks (e.g. `Inbox`, `<inbox>`, `*`), and the hands-off matchers
    downstream compare against the bare token (`*`, `<name>`, `Sessions/`).
    """
    # Discover which columns this CONFIG actually presents, then map the two
    # known layouts onto the canonical (literal, wildcard) pair.
    header = _table_header(text, "Folders")
    if "literal" in header:
        # v005 layout: the literal folder name lives in the `literal` column.
        literal_col = "literal"
    elif "folder" in header:
        # Older layout: the literal name lived in the `folder` column.
        literal_col = "folder"
    else:
        raise RuntimeError(
            f"Section '## Folders': no folder-name column found in header {header!r} "
            "(expected 'literal' or 'folder')."
        )
    # The skip-glob column: v006 renames it `script-skip` (paths the deterministic
    # scripts never touch); older layouts called it `hands-off` / `hands-off-patterns`.
    handsoff_col = next(
        (c for c in ("script-skip", "hands-off", "hands-off-patterns") if c in header),
        "hands-off",
    )
    expected = [
        c for c in header
        if c in (literal_col, "wildcard", "type-defaults", handsoff_col, "description")
    ]
    # Require the four load-bearing columns plus description, in any order.
    required = {literal_col, "wildcard", "type-defaults", handsoff_col, "description"}
    missing = required - set(header)
    if missing:
        raise RuntimeError(
            f"Section '## Folders': header {header!r} is missing required "
            f"column(s) {sorted(missing)!r}."
        )

    rows = _parse_table(text, "Folders", header)
    result: dict[str, RoutingRow] = {}
    for row in rows:
        folder = _strip_backticks(row[literal_col])
        wildcard = _strip_backticks(row["wildcard"])
        type_defaults = tuple(
            t.strip() for t in row["type-defaults"].split(",")
            if t.strip()
        )
        hands_off = tuple(
            _strip_backticks(p) for p in row[handsoff_col].split(",")
            if _strip_backticks(p)
        )
        result[folder] = RoutingRow(
            folder=folder,
            wildcard=wildcard,
            type_defaults=type_defaults,
            hands_off_patterns=hands_off,
            description=row["description"],
        )
    return result


def _parse_content_lifecycle(text: str) -> dict[str, ContentLifecycleRow]:
    """Parse § Content lifecycle into one row per transient class.

    Absent section returns {} — the table is additive canon, so a CONFIG that
    predates it stays parseable rather than raising.
    """
    try:
        rows = _parse_table(
            text, "Content lifecycle",
            ["class", "trigger", "destination", "actor", "cadence"],
        )
    except Exception:
        return {}
    result: dict[str, ContentLifecycleRow] = {}
    for row in rows:
        name = _strip_backticks(row["class"]).strip()
        if not name:
            continue
        result[name] = ContentLifecycleRow(
            cls=name,
            trigger=row["trigger"].strip(),
            destination=row["destination"].strip(),
            actor=row["actor"].strip(),
            cadence=row["cadence"].strip(),
        )
    return result


def _parse_subfolders(text: str) -> dict[str, SubfolderRow]:
    # CONFIG names this column `type-role` (the lookup key, per § Subfolders);
    # an older layout called it `type-defaults`. Accept either, the same way
    # _parse_folders tolerates the `literal`/`folder` rename.
    header = _table_header(text, "Subfolders")
    role_col = "type-role" if "type-role" in header else "type-defaults"
    columns = ["subfolder", role_col, "description"]
    rows = _parse_table(text, "Subfolders", columns)
    result: dict[str, SubfolderRow] = {}
    for row in rows:
        subfolder = _strip_backticks(row["subfolder"])
        # A parenthesized role (e.g. `(asset)`) marks a non-type structural slot,
        # not a `type:` value — keep it out of the type-keyed defaults.
        type_defaults = tuple(
            t.strip() for t in row[role_col].split(",")
            if t.strip() and not t.strip().startswith("(")
        )
        result[subfolder] = SubfolderRow(
            subfolder=subfolder,
            type_defaults=type_defaults,
            description=row["description"],
        )
    return result


def _parse_tags(text: str) -> dict[str, TagRow]:
    # CONFIG.md uses 'alias' (singular column name) but cell may be comma-separated
    columns = ["tag", "alias", "scope", "description"]
    rows = _parse_table(text, "Tags", columns)
    result: dict[str, TagRow] = {}
    for row in rows:
        tag = row["tag"].lower().strip()
        aliases = tuple(
            a.strip() for a in row["alias"].split(",")
            if a.strip()
        )
        result[tag] = TagRow(
            tag=tag,
            aliases=aliases,
            scope=row["scope"].strip(),
            description=row["description"],
        )
    return result


def _parse_actions(text: str) -> dict[str, ActionRow]:
    columns = ["action", "shape", "authorized-agents", "description"]
    rows = _parse_table(text, "Actions", columns)
    result: dict[str, ActionRow] = {}
    for row in rows:
        action = row["action"].strip()
        agents = tuple(
            a.strip() for a in row["authorized-agents"].split(",")
            if a.strip()
        )
        result[action] = ActionRow(
            action=action,
            shape=row["shape"],
            authorized_agents=agents,
            description=row["description"],
        )
    return result


def _parse_skill_slugs(text: str) -> dict[str, SkillSlugRow]:
    """Parse the ## Skill slugs table from CONFIG.md.

    Exposes the canonical skill-slug list so consumers (e.g. the unprocessed-
    content detector in audit.py) can distinguish a real skill working-set
    container from a stray inbox subfolder.
    """
    columns = ["slug", "inbox-container", "description"]
    rows = _parse_table(text, "Skill slugs", columns)
    result: dict[str, SkillSlugRow] = {}
    for row in rows:
        slug = row["slug"].strip().lower()
        raw_container = row["inbox-container"].strip().lower()
        inbox_container = raw_container in ("yes", "true", "1")
        result[slug] = SkillSlugRow(
            slug=slug,
            inbox_container=inbox_container,
            description=row["description"],
        )
    return result


def _parse_scan_exclude_dirs(text: str) -> frozenset[str]:
    """Parse the named directories from the ### Scan exclusions table.

    The table sits inside the ## Folders section (an H3 subsection), so the
    general H2-anchored _parse_table cannot reach it. We locate it by its own
    header row `| excluded-dir | reason |` and collect the first column.

    The canonical exclusion rule is the dot-directory convention (any name
    starting with '.'); this list is the named, documented subset. It is parsed
    so consumers can surface the explicit names, but the dot-directory test in
    is_excluded_dir is what every walk actually prunes against.
    """
    header_re = re.compile(
        r"^\| excluded-dir \| reason \|",
        re.MULTILINE,
    )
    m = header_re.search(text)
    names: list[str] = []
    if m:
        section = text[m.end():]
        in_table = False
        for line in section.splitlines():
            stripped = line.strip()
            if not stripped:
                if in_table:
                    break
                continue
            if not stripped.startswith("|"):
                break  # end of table
            in_table = True
            if re.match(r"^\|[-| ]+\|$", stripped):
                continue  # separator row
            parts = [c.strip() for c in stripped.strip("|").split("|")]
            if not parts or not parts[0]:
                continue
            name = _strip_backticks(parts[0])
            if name:
                names.append(name)
    return frozenset(names)


# ---------------------------------------------------------------------------
# File-handling section parser (mixed-format)
# ---------------------------------------------------------------------------

def _parse_file_handling(text: str) -> "FileHandling":
    """Parse the mixed-format ## File handling section."""

    # --- 1. Global frontmatter fields ---
    # The label may be bold or plain prose, end with a colon or not:
    #   "Required frontmatter on every vault `.md`: `type`, `tags`, `date`, ..."
    # Match the label, then take the run of text up to end-of-line and pull the
    # first three backtick-quoted fields from it (later prose like "plus the
    # per-type keys" carries no backticks, so it is ignored).
    label_match = re.search(
        r"\**Required frontmatter on every vault[^\n]*?:\**\s*([^\n]*)",
        text,
    )
    if label_match is None:
        raise RuntimeError(
            "Could not find 'Required frontmatter on every vault' line in ## File handling"
        )
    line_rest = label_match.group(1)
    fields = re.findall(r"`([^`]+)`", line_rest)
    # Drop any leading `.md`-style file token (the label sometimes quotes `.md`)
    # and keep the frontmatter keys.
    fields = [f for f in fields if not f.lower().startswith(".")]
    if not fields:
        fields = [f.strip() for f in line_rest.split(",") if f.strip()]
    global_frontmatter = tuple(fields[:3]) if len(fields) >= 3 else tuple(fields)

    # --- 2. Inbox additions ---
    # Prose form, bold-optional: "Inbox drafts also carry `reviewed: false`,
    # `status: draft`." An older CONFIG wrote this as a table; still supported.
    inbox_additions: dict[str, str] = {}
    inbox_line = re.search(
        r"\**Inbox drafts also carry:?\**\s*([^\n]*)",
        text,
    )
    if inbox_line:
        # Pull `key: value` pairs from backtick-quoted spans on the same line.
        for span in re.findall(r"`([^`]+)`", inbox_line.group(1)):
            if ":" in span:
                k, v = span.split(":", 1)
                inbox_additions[k.strip()] = v.strip()
    if not inbox_additions:
        # Table form fallback (older CONFIG format).
        inbox_match = re.search(
            r"\**Inbox drafts also carry:?\**\s*\n(.*?)(?=\n\n|\Z)",
            text,
            re.DOTALL,
        )
        if inbox_match:
            table_text = inbox_match.group(1)
            for line in table_text.splitlines():
                stripped = line.strip()
                if not stripped.startswith("|") or "field" in stripped.lower():
                    continue
                if re.match(r"^\|[-| ]+\|$", stripped):
                    continue
                parts = [c.strip() for c in stripped.strip("|").split("|")]
                if len(parts) == 2 and parts[0]:
                    inbox_additions[parts[0]] = parts[1]
    if not inbox_additions:
        # Minimal fallback based on vault conventions
        inbox_additions = {"reviewed": "false", "status": "draft"}

    # --- 3. Orphan asset folder (resolved after FOLDER_ROUTING is built) ---
    orphan_asset_folder = Path(_folder_by_semantic("inbox"))

    # --- 4. Exceptions table ---
    # Find header row "| pattern | required-frontmatter | reason |"
    exc_header_re = re.compile(
        r"^\| pattern \| required-frontmatter \| reason \|",
        re.MULTILINE,
    )
    exc_header_m = exc_header_re.search(text)
    exceptions: list[FileException] = []
    if exc_header_m:
        exc_section = text[exc_header_m.end():]
        # Iterate lines; skip empty lines and the separator row; stop at
        # first non-empty, non-pipe line (end of table or new paragraph)
        in_table = False
        for line in exc_section.splitlines():
            stripped = line.strip()
            if not stripped:
                # Allow one blank line before the table starts, stop after
                if in_table:
                    break
                continue
            if not stripped.startswith("|"):
                break  # end of table
            in_table = True
            if re.match(r"^\|[-| ]+\|$", stripped):
                continue  # separator row
            parts = [c.strip() for c in stripped.strip("|").split("|")]
            if len(parts) < 3:
                continue
            pattern, req_fm, reason = parts[0], parts[1], parts[2]
            if req_fm.lower() == "none" or not req_fm:
                fm_tuple: tuple[str, ...] = ()
            else:
                fm_tuple = tuple(f.strip() for f in req_fm.split(",") if f.strip())
            exceptions.append(FileException(
                pattern=pattern,
                required_frontmatter=fm_tuple,
                reason=reason,
            ))

    return FileHandling(
        global_frontmatter=global_frontmatter,
        inbox_additions=inbox_additions,
        orphan_asset_folder=orphan_asset_folder,
        exceptions=tuple(exceptions),
    )


# ---------------------------------------------------------------------------
# Semantic folder lookup
# ---------------------------------------------------------------------------

def is_excluded_dir(name: str) -> bool:
    """Return True if a directory with this basename must be pruned from the walk.

    The canonical rule (CONFIG.md § Folders / Scan exclusions) is the
    dot-directory convention: any directory whose name begins with '.' is
    tooling/config, never vault content — this covers `.claude`, `.git`,
    `.obsidian`, `.trash`, and any future dot-directory at any depth. The
    explicitly named members in SCAN_EXCLUDE_DIRS are also matched, so a named
    exclusion that somehow did not start with '.' would still be honored.
    """
    if not name:
        return False
    if name.startswith("."):
        return True
    return name in SCAN_EXCLUDE_DIRS


def _parse_asset_folders(text: str) -> dict[str, tuple[str, ...]]:
    """Parse the ## Asset folders table into trigger -> tuple of bare tokens.

    Each `matches` cell lists backtick-quoted tokens, collected per trigger row
    (keyword, marker, vcs, manifest, home). An absent section yields an empty
    mapping, so the feature is inert until the table exists.
    """
    result: dict[str, tuple[str, ...]] = {}
    m = re.search(r"^## Asset folders\s*$", text, re.MULTILINE)
    if m is None:
        return result
    after = text[m.end():]
    nxt = re.search(r"^## ", after, re.MULTILINE)
    section = after[: nxt.start()] if nxt else after
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2:
            continue
        trigger = _strip_backticks(cells[0]).lower()
        if trigger in ("trigger", "") or set(trigger) <= {"-", ":"}:
            continue  # header or separator row
        tokens = tuple(re.findall(r"`([^`]+)`", cells[1]))
        if trigger and tokens:
            result[trigger] = tokens
    return result


def is_asset_folder(path: Path) -> bool:
    """Return True if a directory is a classified asset unit (CONFIG § Asset
    folders): an opaque folder the deterministic walk must not descend into and
    the agents must move whole and never edit inside. Cheap — direct signals
    only, no recursive read.
    """
    low = path.name.lower()
    # Suffix match: a folder is an asset when its name *ends* with a keyword
    # (`my-repo`, `data-export`), not merely contains it — so `off-site-meeting`
    # and `my-site-redesign` are not swept up by the `-site` keyword.
    if any(low.endswith(k.lower()) for k in ASSET_KEYWORDS):
        return True
    # A folder *under* an asset home (not the home folder itself).
    if ASSET_HOME_DIRS & set(path.parts[:-1]):
        return True
    # A marker file, VCS dir, or build manifest among the direct children.
    try:
        for child in path.iterdir():
            cn = child.name
            if cn in ASSET_MARKERS:
                return True
            if cn in ASSET_VCS_DIRS and child.is_dir():
                return True
            if cn in ASSET_MANIFESTS:
                return True
    except OSError:
        pass
    return False


def _parse_cold_storage(text: str) -> tuple[int, str]:
    """Return (archive_retention_days, history_dirname) from § Cold storage.

    The retention window is read from the `archive-retention` row of the
    Cold-storage settings table (e.g. `14d`). The `<history>` directory name is
    read from the § Folders relative-token table row for `<history>` (it resolves
    to `<vault-root>/.history/`); the basename (`.history`) is returned. Both are
    config-driven so a change to the window or the cold-store name needs no code
    edit. Falls back to (14, ".history") if a row is absent.
    """
    retention_days = 14
    m = re.search(
        r"\|\s*`?archive-retention`?\s*\|\s*`?(\d+)\s*([dwy]?)`?\s*\|",
        text,
    )
    if m:
        n = int(m.group(1))
        unit = m.group(2) or "d"
        retention_days = n * {"d": 1, "w": 7, "y": 365}.get(unit, 1)

    history_dir = ".history"
    hm = re.search(
        r"\|\s*`?<history>`?\s*\|[^|]*?`([^`]*\.[A-Za-z0-9_-]+)[/`]",
        text,
    )
    if hm:
        raw = hm.group(1).strip().strip("/")
        # Take the last path segment that begins with a dot (the cold-store dir).
        for seg in reversed(raw.replace("\\", "/").split("/")):
            if seg.startswith("."):
                history_dir = seg
                break
    return retention_days, history_dir


def _parse_named_token_paths(text: str) -> dict[str, str]:
    """Parse the § Folders token table (`token | resolves to`) into raw values.

    Each parsed row's first cell is one backticked `<token>` and its value is
    the FIRST backticked span of the second cell (e.g. `<inbox>/User-Queue.md`).
    Rows whose second cell carries no backticked span — pure-prose resolutions
    like `<user-home>` — are skipped. Values stay raw here; `token_path`
    expands embedded `<wildcard>` references against § Folders.
    """
    result: dict[str, str] = {}
    row_re = re.compile(r"^\|\s*`<([\w-]+)>`\s*\|\s*([^\n]*)\|\s*$", re.MULTILINE)
    for m in row_re.finditer(text):
        token, rest = m.group(1), m.group(2)
        span = re.search(r"`([^`]+)`", rest)
        if span and token not in result:
            result[token] = span.group(1).strip()
    return result


def token_path(token: str, fallback: Optional[str] = None) -> str:
    """Vault-relative path for a § Folders token-table token.

    Accepts the token with or without angle brackets. Embedded `<wildcard>`
    references expand against the § Folders literals first (`<inbox>` →
    `Inbox`), then against the token table itself; the result is returned with
    any trailing slash stripped (e.g. token_path('<logs>') -> 'Archive/Logs').
    Raises ConfigLookupError when the token is undefined and no fallback is
    given — scripts must never silently invent a path.
    """
    key = token.strip().strip("<>")
    raw = NAMED_TOKEN_PATHS.get(key)
    if raw is None:
        if fallback is not None:
            return fallback
        raise ConfigLookupError(
            f"No `<{key}>` row in the CONFIG § Folders token table. "
            f"Known tokens: {sorted(NAMED_TOKEN_PATHS)}"
        )

    def _expand(value: str, depth: int = 0) -> str:
        if depth > 4:  # cycle guard
            return value
        def repl(m: "re.Match[str]") -> str:
            inner = m.group(1)
            by_folder = folder_for_wildcard(inner)
            if by_folder is not None:
                return by_folder
            nested = NAMED_TOKEN_PATHS.get(inner)
            if nested is not None:
                return _expand(nested, depth + 1)
            return m.group(0)
        return re.sub(r"<([\w-]+)>", repl, value)

    return _expand(raw).strip().strip("/")


def folder_for_wildcard(wildcard: str) -> Optional[str]:
    """Return the literal folder name for a `<wildcard>` token, or None.

    Resolves against the `wildcard` column (e.g. '<inbox>' -> 'Inbox').
    Accepts the token with or without angle brackets.
    """
    token = wildcard.strip()
    if not token.startswith("<"):
        token = f"<{token.strip('<>')}>"
    for folder_name, row in FOLDER_ROUTING.items():
        if row.wildcard and row.wildcard == token:
            return folder_name
    return None


def _folder_by_semantic(needle: str) -> str:
    """Resolve a semantic folder name to its vault path string.

    Resolution order:
        1. The `wildcard` column — '<inbox>' etc. (exact, unambiguous).
        2. Numeric-prefix-stripped basename equality, which supports a legacy
           CONFIG without a wildcard column (e.g. '00-Inbox' -> 'inbox').

    Returns the folder path string (as stored in FOLDER_ROUTING). Raises
    ConfigLookupError if zero or multiple rows match.

    Examples:
        'Inbox'       -> needle 'inbox'    matches
        '99-Archive'  -> needle 'archive'  matches (legacy prefixed literal)
        'Snippets'    -> needle 'snippets' matches
    """
    needle_lower = needle.lower().strip()

    # 1. Wildcard column (preferred — explicit, unambiguous).
    by_wildcard = folder_for_wildcard(needle_lower)
    if by_wildcard is not None:
        return by_wildcard

    # 2. Numeric-prefix-stripped basename (fallback for the older CONFIG format).
    matches = []
    for folder_name in FOLDER_ROUTING:
        basename = Path(folder_name).name  # handles simple names and paths
        # Strip leading numeric prefix like '00-', '99-', '01-'
        stripped = re.sub(r"^\d+-", "", basename).lower()
        if stripped == needle_lower:
            matches.append(folder_name)
    if len(matches) == 1:
        return matches[0]
    if len(matches) == 0:
        raise ConfigLookupError(
            f"No folder matching semantic name '{needle}' in FOLDER_ROUTING. "
            f"Available: {list(FOLDER_ROUTING.keys())}"
        )
    raise ConfigLookupError(
        f"Ambiguous semantic name '{needle}': matched {matches}"
    )


# ---------------------------------------------------------------------------
# Vault-root resolver
# ---------------------------------------------------------------------------

def _archive_folder_name() -> str:
    """The archive folder literal from § Folders, or 'Archive' if unresolvable."""
    try:
        return _folder_by_semantic("archive")
    except ConfigLookupError:
        return "Archive"


def _walk_up_for_vault(start: Path, archive: str) -> Optional[Path]:
    """Ascend from `start`; return the first vault root recognised on the way.

    A directory is the vault root when it holds both `.claude/` and the archive
    folder as children; a `.claude` directory whose sibling is the archive folder
    resolves to its parent. Returns None when no vault root is found by the
    filesystem root.
    """
    try:
        start = start.resolve()
    except OSError:
        return None
    for d in (start, *start.parents):
        if (d / ".claude").is_dir() and (d / archive).is_dir():
            return d
        if d.name == ".claude" and (d.parent / archive).is_dir():
            return d.parent
    return None


def resolve_vault_root(
    cli_root: Optional[Path] = None,
    *,
    env: Optional[dict] = None,
    script_start: Optional[Path] = None,
    cwd_start: Optional[Path] = None,
) -> Optional[Path]:
    """The one vault-root resolver for every kit script.

    Priority order:
        1. `cli_root` — an explicit `--vault-root` argument.
        2. `NOTE_KIT_VAULT_ROOT` environment variable.
        3. `JANITOR_VAULT_ROOT` environment variable (the audit family's var).
        4. Installed-layout walk-up from this script's location — the kit root is
           a vault's `.claude/`, so its parent (holding the archive folder) is
           the vault root.
        5. Walk-up from the current working directory, so a run from anywhere
           inside a vault resolves it.

    Returns None when nothing resolves; callers decide whether that is fatal
    (never silently write outside a vault). The `env`, `script_start`, and
    `cwd_start` seams exist for testing each source in isolation; they default
    to `os.environ`, this script's directory, and `Path.cwd()`.
    """
    if cli_root is not None:
        return Path(cli_root)

    env = os.environ if env is None else env
    for var in ("NOTE_KIT_VAULT_ROOT", "JANITOR_VAULT_ROOT"):
        raw = env.get(var)
        if raw:
            return Path(raw)

    archive = _archive_folder_name()
    for start in (
        script_start if script_start is not None else _SCRIPTS_DIR,
        cwd_start if cwd_start is not None else Path.cwd(),
    ):
        found = _walk_up_for_vault(Path(start), archive)
        if found is not None:
            return found
    return None


# ---------------------------------------------------------------------------
# Tag normalization helpers
# ---------------------------------------------------------------------------

def levenshtein_le1(a: str, b: str) -> bool:
    """Return True if Levenshtein distance between a and b is at most 1."""
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(ca != cb for ca, cb in zip(a, b)) <= 1
    # length differs by 1
    shorter, longer = (a, b) if len(a) < len(b) else (b, a)
    i = 0
    while i < len(shorter) and shorter[i] == longer[i]:
        i += 1
    return shorter[i:] == longer[i + 1:]


def normalize_tag(value: str) -> Optional[str]:
    """Resolve a tag value to its canonical key, or None if unresolvable.

    Resolution order:
        1. Strip + lowercase.
        2. Exact canonical match.
        3. Alias lookup.
        4. Trailing-s plural collapse.
        5. Levenshtein-1 fuzzy match.
    """
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    if not candidate:
        return None
    if candidate in CANONICAL_TAG_KEYS:
        return candidate
    if candidate in _TAG_ALIASES:
        return _TAG_ALIASES[candidate]
    # trailing-s plural
    if candidate.endswith("s") and len(candidate) > 1 and candidate[:-1] in CANONICAL_TAG_KEYS:
        return candidate[:-1]
    # Levenshtein-1
    for canonical in CANONICAL_TAG_KEYS:
        if levenshtein_le1(candidate, canonical):
            return canonical
    return None


# ---------------------------------------------------------------------------
# Module initialization — parse at import time
# ---------------------------------------------------------------------------

if not _CONFIG.exists():
    raise FileNotFoundError(
        f"CONFIG.md not found at {_CONFIG}. "
        "Ensure .claude/CONFIG.md exists beside this script's kit root before "
        "importing this module (or set NOTE_KIT_CONFIG to an explicit path)."
    )

_CONFIG_TEXT = _CONFIG.read_text(encoding="utf-8")

# Parse in dependency order: FOLDER_ROUTING first (needed by _folder_by_semantic)
FOLDER_ROUTING: dict[str, RoutingRow] = _parse_folders(_CONFIG_TEXT)
# § Folders token table — named paths (`<user-queue>`, `<logs>`, …), raw values;
# resolve through token_path().
NAMED_TOKEN_PATHS: dict[str, str] = _parse_named_token_paths(_CONFIG_TEXT)
TYPES: dict[str, TypeRow] = _parse_types(_CONFIG_TEXT)
SUBFOLDERS: dict[str, SubfolderRow] = _parse_subfolders(_CONFIG_TEXT)
CONTENT_LIFECYCLE: dict[str, ContentLifecycleRow] = _parse_content_lifecycle(_CONFIG_TEXT)
TAGS: dict[str, TagRow] = _parse_tags(_CONFIG_TEXT)
ACTIONS: dict[str, ActionRow] = _parse_actions(_CONFIG_TEXT)
SKILL_SLUGS: dict[str, SkillSlugRow] = _parse_skill_slugs(_CONFIG_TEXT)
SCAN_EXCLUDE_DIRS: frozenset[str] = _parse_scan_exclude_dirs(_CONFIG_TEXT)

# § Asset folders — classified opaque units the walk prunes and agents move whole.
_ASSET_FOLDERS: dict[str, tuple[str, ...]] = _parse_asset_folders(_CONFIG_TEXT)
ASSET_KEYWORDS: tuple[str, ...] = _ASSET_FOLDERS.get("keyword", ())
ASSET_MARKERS: frozenset[str] = frozenset(_ASSET_FOLDERS.get("marker", ()))
ASSET_VCS_DIRS: frozenset[str] = frozenset(_ASSET_FOLDERS.get("vcs", ()))
ASSET_MANIFESTS: frozenset[str] = frozenset(_ASSET_FOLDERS.get("manifest", ()))
ASSET_HOME_DIRS: frozenset[str] = frozenset(_ASSET_FOLDERS.get("home", ()))

_TAG_ALIASES: dict[str, str] = {
    alias: row.tag
    for row in TAGS.values()
    for alias in row.aliases
}

FILE_HANDLING: FileHandling = _parse_file_handling(_CONFIG_TEXT)

CANONICAL_TYPE_KEYS: frozenset[str] = frozenset(TYPES.keys())
CANONICAL_TAG_KEYS: frozenset[str] = frozenset(TAGS.keys())

# § Cold storage — retention window and the cold-store directory name.
ARCHIVE_RETENTION_DAYS, HISTORY_DIRNAME = _parse_cold_storage(_CONFIG_TEXT)


# ---------------------------------------------------------------------------
# Self-test block
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print(f"CONFIG source: {_CONFIG}")
    print(f"FILE_HANDLING: {len(FILE_HANDLING.exceptions)} exceptions")
    print(f"  global_frontmatter: {FILE_HANDLING.global_frontmatter}")
    print(f"  inbox_additions: {FILE_HANDLING.inbox_additions}")
    print(f"  orphan_asset_folder: {FILE_HANDLING.orphan_asset_folder}")
    print(f"TYPES: {len(TYPES)}")
    print(f"FOLDER_ROUTING: {len(FOLDER_ROUTING)}")
    print(f"SUBFOLDERS: {len(SUBFOLDERS)}")
    print(f"TAGS: {len(TAGS)}")
    print(f"ACTIONS: {len(ACTIONS)}")
    print(f"SKILL_SLUGS: {len(SKILL_SLUGS)}")
    print(f"SCAN_EXCLUDE_DIRS: {sorted(SCAN_EXCLUDE_DIRS)}")
    print()

    assert TYPES, "TYPES is empty"
    for k in CANONICAL_TYPE_KEYS:
        assert k == k.lower(), f"non-lowercase key: {k}"
    for k in CANONICAL_TAG_KEYS:
        assert k == k.lower(), f"non-lowercase tag: {k}"
    for row in FOLDER_ROUTING.values():
        for t in row.type_defaults:
            assert t in CANONICAL_TYPE_KEYS, f"unknown type in folder routing: {t!r}"
    for row in SUBFOLDERS.values():
        for t in row.type_defaults:
            assert t in CANONICAL_TYPE_KEYS, f"unknown type in subfolders: {t!r}"
    assert SKILL_SLUGS, "SKILL_SLUGS is empty"
    for slug, row in SKILL_SLUGS.items():
        assert slug == slug.lower(), f"non-lowercase slug: {slug!r}"
        assert isinstance(row.inbox_container, bool), f"inbox_container not bool for {slug!r}"
    container_slugs = [s for s, r in SKILL_SLUGS.items() if r.inbox_container]
    print(f"  container slugs (inbox-container=yes): {container_slugs}")

    # Verify _folder_by_semantic works for known names
    inbox = _folder_by_semantic("inbox")
    assert inbox, "_folder_by_semantic('inbox') returned empty"
    print(f"_folder_by_semantic('inbox') -> {inbox!r}")
    archive = _folder_by_semantic("archive")
    assert archive, "_folder_by_semantic('archive') returned empty"
    print(f"_folder_by_semantic('archive') -> {archive!r}")

    # Wildcard column resolution
    print(f"folder_for_wildcard('<projects>') -> {folder_for_wildcard('<projects>')!r}")
    assert folder_for_wildcard("<archive>") == archive

    # Token-table named paths (queues, staging, logs, catchall)
    for tok in ("user-queue", "machine-queue", "inbox-assets", "logs", "catchall"):
        resolved = token_path(tok)
        print(f"token_path({tok!r}) -> {resolved!r}")
        assert resolved and "<" not in resolved, f"token_path({tok!r}) unresolved: {resolved!r}"
    assert token_path("user-queue").startswith(inbox), "user-queue must live under <inbox>"
    assert token_path("logs").startswith(archive), "<logs> must live under <archive>"
    assert token_path("no-such-token", "X") == "X", "token_path fallback failed"

    # Scan exclusions — named subset parsed, dot-directory convention applied.
    assert ".claude" in SCAN_EXCLUDE_DIRS, "SCAN_EXCLUDE_DIRS missing .claude"
    assert is_excluded_dir(".claude"), "is_excluded_dir failed on .claude"
    assert is_excluded_dir(".git") and is_excluded_dir(".obsidian"), "dot-dir test failed"
    assert is_excluded_dir(".anything-new"), "dot-directory convention not applied"
    assert not is_excluded_dir("01-Projects"), "is_excluded_dir false positive"
    assert not is_excluded_dir(""), "is_excluded_dir should be False on empty name"

    # Cold storage — retention window and history dir name from § Cold storage.
    print(f"ARCHIVE_RETENTION_DAYS: {ARCHIVE_RETENTION_DAYS}")
    print(f"HISTORY_DIRNAME: {HISTORY_DIRNAME!r}")
    assert ARCHIVE_RETENTION_DAYS >= 1, "archive-retention did not parse"
    assert HISTORY_DIRNAME.startswith("."), "history dir must be a dot-directory"
    assert is_excluded_dir(HISTORY_DIRNAME), "history dir must be scan-excluded"

    # A clean CONFIG.md drops no rows; a skip here means a malformed table row
    # shrank a parsed vocabulary and must be fixed in CONFIG, not tolerated.
    print(f"PARSE_SKIPS: {len(PARSE_SKIPS)}")
    assert not PARSE_SKIPS, f"malformed table rows were skipped: {PARSE_SKIPS}"

    # Vault-root resolver: env var wins, then the installed-layout walk-up.
    _r_env = resolve_vault_root(env={"NOTE_KIT_VAULT_ROOT": str(_KIT_ROOT.parent)})
    assert _r_env == _KIT_ROOT.parent, f"env resolve failed: {_r_env}"
    if _KIT_ROOT.name == ".claude" and (_KIT_ROOT.parent / _archive_folder_name()).is_dir():
        _r_walk = resolve_vault_root(env={})
        assert _r_walk == _KIT_ROOT.parent, f"walk-up resolve failed: {_r_walk}"
        print(f"resolve_vault_root() -> {_r_walk}")

    print()
    print("Self-tests passed.")
