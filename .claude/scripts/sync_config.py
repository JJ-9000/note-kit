"""
sync_config.py
===================

Distribution layer for the note-kit config.

Reads CONFIG.md via config_variables and regenerates two scope-limited
orientation tables — `## Session-start defaults` (every CONFIG § Types row, the
type's `default-home` as its typical-folder cell, `revision` included) and
`## Scheduled agents` (from CONFIG § Agent responsibilities) — in both CLAUDE.md
and AGENTS.md. v003 also stamps the CONFIG § Pipeline protocol master block
verbatim into each staged pipeline skill (between matching
`note-kit:sync pipeline-protocol` markers), so the skills physically carry the
shared protocol while CONFIG stays its only editable home. v004 also distributes
CONFIG § Rules: the `rule` column as a marker-bounded `## Always-on rules` block
in CLAUDE.md/AGENTS.md, and the `reminder` column (full text where a cell is
empty) as the generated `RULES.md` the cadence hook injects. The copies are never
authoritative; CONFIG.md is.

Each table is bounded by its own sentinel markers
(`<!-- note-kit:sync session-start ... -->` and
`<!-- note-kit:sync scheduled-agents ... -->`). sync rewrites ONLY the text
between a block's markers, so everything else in CLAUDE.md / AGENTS.md —
including any heading or section you add — is preserved. Edit CONFIG.md (never
a marked block), then re-run this script.

Target resolution. The installed CLAUDE.md and AGENTS.md live at
`<vault-root>/.claude/CLAUDE.md` and `<vault-root>/.claude/AGENTS.md` (Claude
Code auto-loads `CLAUDE.md` from `.claude/` vault-wide). The vault root is
resolved in order: ``--vault-root``, then the ``NOTE_KIT_VAULT_ROOT`` env var,
then the installed layout detected from this script's own location (kit root
named `.claude` whose parent holds the vault archive → that parent is the
vault root). When none of the three resolves, the script exits with an error —
it never guesses a path that could land outside a vault.

Run:
    # installed layout: a bare run detects the vault root from the script's
    # own location; --vault-root / NOTE_KIT_VAULT_ROOT override the detection.
    python <vault>/.claude/scripts/sync_config.py

    # kit-source checkout (editing the kit in place): detection refuses the
    # repo (its kit-root parent is not a vault), so name the target explicitly.
    python <repo>/.claude/scripts/sync_config.py --vault-root <repo>
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Location resolution
# ---------------------------------------------------------------------------

_SCRIPTS_DIR = Path(__file__).resolve().parent
_KIT_ROOT = _SCRIPTS_DIR.parent

sys.path.insert(0, str(_SCRIPTS_DIR))

# ---------------------------------------------------------------------------
# Shape gate + config load are deferred to main()
# ---------------------------------------------------------------------------
# config_shape validates (and safely repairs) CONFIG.md BEFORE config_variables
# parses it, so a repaired separator row or restored pipe is what the parsers
# actually read. Both the shape run (which may WRITE a repair) and the
# config_variables import (which parses CONFIG) run inside main(), never at
# module import — so `import sync_config` is side-effect-free: it neither parses
# CONFIG nor mutates it. `import config_shape` is itself side-effect-free (it
# reads CONFIG only when run() is called).
import config_shape  # noqa: E402
import frontmatter_helpers as _fh  # noqa: E402

# Names bound by _load_config_variables() at the head of main(), after the shape
# gate. Declared here so the module-level `def`s that read them resolve at call
# time (every such caller runs after the loader).
FILE_HANDLING = None
TYPES: dict = {}
FOLDER_ROUTING: dict = {}
SUBFOLDERS: dict = {}
_CONFIG_TEXT = ""
_CONFIG = None
_ARCHIVE_FOLDER = ""
_folder_by_semantic = None
_parse_table = None
token_path = None
resolve_vault_root = None

# Sync targets, set in main() once a vault root resolves.
_CLAUDE_MD = _AGENTS_MD = _SYNC_LOG = None


def _load_config_variables() -> None:
    """Import config_variables (which parses CONFIG.md) and bind the names the
    generators use as module globals.

    Deferred out of module import so `import sync_config` never triggers a CONFIG
    parse or a config_shape repair write; main() calls it only AFTER the shape
    gate has run, so the parsers read the shape-repaired CONFIG (the documented
    "shape validates before config_variables parses" ordering).
    """
    global FILE_HANDLING, TYPES, FOLDER_ROUTING, SUBFOLDERS
    global _folder_by_semantic, _CONFIG_TEXT, _parse_table, token_path
    global _CONFIG, _ARCHIVE_FOLDER, resolve_vault_root
    from config_variables import (
        FILE_HANDLING,
        TYPES,
        FOLDER_ROUTING,
        SUBFOLDERS,
        _folder_by_semantic,
        _CONFIG_TEXT,
        _parse_table,
        token_path,
        _CONFIG,
        resolve_vault_root,
    )
    _ARCHIVE_FOLDER = _folder_by_semantic("archive")


def _safe_vault_root(cli_root: Path | None) -> Path | None:
    """Vault root tolerant of an unparseable CONFIG.

    Prefers the shared resolver; falls back to the CLI arg, env vars, then the
    installed layout when config_variables cannot import (a shape refusal can
    leave CONFIG unparseable). Used only for the refusal artifact — the happy
    path calls resolve_vault_root directly.
    """
    try:
        from config_variables import resolve_vault_root as _rvr
        r = _rvr(cli_root)
        if r is not None:
            return Path(r)
    except Exception:
        pass
    if cli_root is not None:
        return Path(cli_root)
    for var in ("NOTE_KIT_VAULT_ROOT", "JANITOR_VAULT_ROOT"):
        raw = os.environ.get(var)
        if raw:
            return Path(raw)
    if _KIT_ROOT.name == ".claude":
        return _KIT_ROOT.parent
    return None


def _resolve_targets(vault_root: Path) -> tuple[Path, Path, Path]:
    """Resolve (CLAUDE.md, AGENTS.md, Sync-Log.md) under the vault root.

    The orientation pair lives at ``<vault-root>/.claude/`` and the sync log
    under the vault's archive. There is no pathless fallback: callers must
    resolve a vault root first (see ``resolve_vault_root``) so the log can never
    land outside a vault.
    """
    vault_root = vault_root.resolve()
    claude_md = vault_root / ".claude" / "CLAUDE.md"
    agents_md = vault_root / ".claude" / "AGENTS.md"
    sync_log = vault_root / token_path("logs", f"{_ARCHIVE_FOLDER}/Logs") / "Sync-Log.md"
    return claude_md, agents_md, sync_log


def _refusal_sync_log_path(cli_root: Path | None) -> Path | None:
    """The Sync-Log path resolved WITHOUT parsing CONFIG.

    A shape refusal can leave CONFIG unparseable, so the refusal artifact must
    not depend on config_variables parsing. Uses the CONFIG-driven ``<logs>``
    location when config_variables imports, else the CONFIG-default
    ``Archive/Logs``.
    """
    root = _safe_vault_root(cli_root)
    if root is None:
        return None
    logs_rel = "Archive/Logs"
    try:
        from config_variables import token_path as _tp, _folder_by_semantic as _fbs
        logs_rel = _tp("logs", f"{_fbs('archive')}/Logs")
    except Exception:
        pass
    return root.resolve() / Path(logs_rel) / "Sync-Log.md"


_NO_VAULT_ROOT_ERROR = (
    "No vault root resolved. Pass --vault-root, set NOTE_KIT_VAULT_ROOT, or run "
    "the copy installed at <vault>/.claude/scripts/ (detected by the vault "
    f"archive folder beside the kit root). Kit root: {_KIT_ROOT}. "
    "Refusing to write a sync log outside a vault."
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SESSION_ANCHOR = "## Session-start defaults"
_AGENTS_ANCHOR = "## Scheduled agents"

# The session-start Types table mirrors every CONFIG type, `revision` included:
# CLAUDE-v005's orientation table lists it, so the sync emits it too. No type is
# omitted; the table is a faithful mirror of CONFIG § Types.

_SYNC_NOTE = (
    "_Generated from CONFIG.md by sync_config — "
    "do not hand-edit; edit CONFIG.md instead._"
)

# Sentinel markers bound each auto-generated block. sync rewrites ONLY the text
# between a block's own markers, so every other part of CLAUDE.md / AGENTS.md —
# including any heading or section the user adds — is preserved.
_SYNC_START = (
    "<!-- note-kit:sync session-start — auto-generated from CONFIG.md; "
    "do not edit between these markers -->"
)
_SYNC_END = "<!-- /note-kit:sync session-start -->"

_AGENTS_SYNC_START = (
    "<!-- note-kit:sync scheduled-agents — auto-generated from CONFIG.md; "
    "do not edit between these markers -->"
)
_AGENTS_SYNC_END = "<!-- /note-kit:sync scheduled-agents -->"

# Always-on rules: CONFIG § Rules is the canon (columns: rule | reminder). The
# `rule` column lands in CLAUDE.md/AGENTS.md as orientation; the `reminder`
# column (full text where empty) becomes the generated RULES.md the cadence
# hook injects.
_RULES_ANCHOR = "## Always-on rules"
_RULES_SYNC_START = (
    "<!-- note-kit:sync always-on-rules — auto-generated from CONFIG.md; "
    "do not edit between these markers -->"
)
_RULES_SYNC_END = "<!-- /note-kit:sync always-on-rules -->"
_RULES_MD_NOTICE = (
    "<!-- generated from CONFIG.md § Rules by sync_config — "
    "edit the CONFIG table, not this file -->"
)

# Pipeline-protocol block: CONFIG § Pipeline protocol holds the master between
# these exact markers; the same markers sit in each staged pipeline skill, and
# sync stamps the block verbatim so the skills physically carry the protocol
# (self-contained at runtime) while CONFIG stays the only editable home.
_PIPE_SYNC_START = (
    "<!-- note-kit:sync pipeline-protocol — transposed from CONFIG.md "
    "§ Pipeline protocol by sync_config; edit CONFIG, not here -->"
)
_PIPE_SYNC_END = "<!-- /note-kit:sync pipeline-protocol -->"
_PIPELINE_SKILLS = ("note-kit-research", "note-kit-review", "note-kit-verify-claims")

# Resident standards: the highest-weight voice/design/format standard notes,
# distilled into a marker-bounded `## Resident standards` block in
# CLAUDE.md/AGENTS.md and into the hook-injected RULES.md, so the standards
# apply as a draft is written instead of being searched for mid-task. Ranked by
# each note's `weight` (which the handoff/analyst recurrence process bumps), so
# the resident set tracks the standards that actually keep getting violated. N
# is a constant here, not a CONFIG knob, to keep this clear of the config-shape
# gate; promote it to CONFIG § when a second consumer needs it.
_RESIDENT_TOP_N_PER_AXIS = 5
_STANDARD_TYPES = ("voice", "design", "format")
_AXIS_LABELS = (("voice", "Voice"), ("design", "Design"), ("format", "Format"))
_RESIDENT_ANCHOR = "## Resident standards"
_RESIDENT_SYNC_START = (
    "<!-- note-kit:sync resident-standards — auto-generated from the "
    "highest-weight standard notes; do not edit between these markers -->"
)
_RESIDENT_SYNC_END = "<!-- /note-kit:sync resident-standards -->"
_RESIDENT_NOTE = (
    "_The highest-weight standards on each axis, loaded so they apply without a "
    "lookup. Top 5 per axis by `weight`; regenerated by sync_config — bump a "
    "standard's weight to raise it._"
)

# A weighted standard note may carry an optional one-line `trigger:` frontmatter
# field: the note's own heaviest operative trigger — the when-this-happens detail
# its headline sentence leaves buried in the body. Where the field is present, the
# resident block renders it as a subordinate second line under that standard's
# entry, so the actionable detail is injected alongside the headline instead of
# waiting for a lookup. A note without the field keeps its single-line entry.
_RESIDENT_TRIGGER_PREFIX = "  - ↳ "
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)

# ---------------------------------------------------------------------------
# Section replacement
# ---------------------------------------------------------------------------

def _replace_section(content: str, heading: str, new_body: str) -> tuple[str, bool]:
    """Replace the body of an H2 section in markdown content.

    Finds the heading line (exact match on the heading text). Replaces
    everything between it and the next same-level (##) heading or EOF.
    Returns (updated_content, found).  If not found, returns
    (content_with_section_appended, False).

    ``new_body`` must NOT include the heading line itself.

    The emitted section always ends with exactly two newlines (``body\\n\\n``)
    so that sections are separated by a blank line in the output. This keeps
    the append-path and replace-path byte-identical and guarantees idempotency
    from the second run onward.
    """
    pattern = re.compile(
        r"(^" + re.escape(heading) + r"\s*\n)"   # heading line (group 1)
        r"(.*?)"                                   # body to replace (group 2)
        r"(?=^## |\Z)",                            # lookahead: next ## or EOF
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(content)
    if m:
        replacement = m.group(1) + new_body.rstrip("\n") + "\n\n"
        updated = content[: m.start()] + replacement + content[m.end() :]
        return updated, True

    # Section not found — append it (same two-newline tail for consistency).
    content = content.rstrip("\n") + "\n\n"
    appended = content + heading + "\n" + new_body.rstrip("\n") + "\n\n"
    return appended, False


def _replace_synced_block(
    content: str, body: str, start_marker: str, end_marker: str, anchor: str
) -> tuple[str, bool]:
    """Replace one sentinel-bounded sync block; preserve everything else.

    ``body`` already includes its sentinel markers. With both markers present,
    only the inclusive span between them is rewritten — the heading and any user
    content before or after the block stay untouched. Without them (first run
    after upgrade, or a fresh file), the block is placed under ``anchor``,
    appending that heading if it is missing.
    """
    if start_marker in content and end_marker in content:
        start = content.index(start_marker)
        end = content.index(end_marker) + len(end_marker)
        return content[:start] + body + content[end:], True
    return _replace_section(content, anchor, body)


# ---------------------------------------------------------------------------
# Table builder
# ---------------------------------------------------------------------------

def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    """Build a markdown pipe table string (no trailing newline)."""
    sep = ["-" * max(3, len(h)) for h in headers]
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(sep) + " |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _typical_folder_for(key: str, trow) -> str:
    """The type's session-start home cell.

    Prefer CONFIG § Types' own `default-home` cell (v005+) verbatim — it is the
    canonical, display-ready home (`<projects>`, `parent's <voice>/ subfolder`,
    …). Fall back to deriving from FOLDER_ROUTING / SUBFOLDERS only when a row
    carries no default-home (older CONFIG without the column).
    """
    if trow.default_home:
        return trow.default_home
    # Fallback: derive from routing/subfolder tables (pre-v005 CONFIG).
    folders = [
        f"`{r.wildcard}`" if r.wildcard else name
        for name, r in FOLDER_ROUTING.items()
        if key in r.type_defaults
    ]
    if folders:
        return ", ".join(folders)
    for sub_name, sub_row in SUBFOLDERS.items():
        if "<" in sub_name:
            continue
        if key in sub_row.type_defaults:
            return f"*/{sub_name}/"
    return ""


def _build_session_defaults_table() -> str:
    """Build the `## Session-start defaults` table (marker-bounded).

    Columns: type | typical folder | naming pattern | required frontmatter |
    description. Every CONFIG § Types row is mirrored — `revision` included — so
    the orientation table is a faithful copy of the canon.
    """
    global_fm = list(FILE_HANDLING.global_frontmatter)  # ['type', 'tags', 'date']

    rows: list[list[str]] = []
    for key, trow in TYPES.items():
        all_fm = global_fm + list(trow.additional_frontmatter)
        rows.append([
            key,
            _typical_folder_for(key, trow),
            trow.pattern,
            ", ".join(all_fm),
            trow.description,
        ])

    headers = ["type", "typical folder", "naming pattern", "required frontmatter", "description"]
    table = _md_table(headers, rows)
    return _SYNC_START + "\n" + _SYNC_NOTE + "\n\n" + table + "\n" + _SYNC_END


def _build_scheduled_agents_table() -> str:
    """Build the `## Scheduled agents` table (marker-bounded).

    Columns: agent | recommended cadence | scope. Read from CONFIG
    § Agent responsibilities (columns: agent | scope | trigger | recommended
    cadence | does); the cadence and a one-line scope are mirrored. Falls back to
    an empty table if the section is absent.
    """
    headers = ["agent", "recommended cadence", "scope"]
    rows: list[list[str]] = []
    try:
        recs = _parse_table(
            _CONFIG_TEXT, "Agent responsibilities",
            ["agent", "scope", "recommended cadence"],
        )
        for r in recs:
            rows.append([
                r["agent"].strip(),
                r["recommended cadence"].strip(),
                r["scope"].strip(),
            ])
    except Exception:
        pass
    table = _md_table(headers, rows)
    return _AGENTS_SYNC_START + "\n" + _SYNC_NOTE + "\n\n" + table + "\n" + _AGENTS_SYNC_END


def _rules_rows() -> list[dict]:
    """CONFIG § Rules rows (columns: rule | reminder). Hard-fails when the
    section or a required column is missing — the rules layer must never
    silently degrade to an empty injection."""
    rows = _parse_table(_CONFIG_TEXT, "Rules", ["rule", "reminder"])
    rows = [r for r in rows if r["rule"].strip()]
    if not rows:
        raise ValueError("CONFIG.md § Rules parsed to zero rules.")
    return rows


def _build_rules_orientation_block() -> str:
    """The `## Always-on rules` block for CLAUDE.md/AGENTS.md (marker-bounded):
    every rule at full text, read once at session start."""
    bullets = "\n".join(f"- {r['rule'].strip()}" for r in _rules_rows())
    return _RULES_SYNC_START + "\n" + _SYNC_NOTE + "\n\n" + bullets + "\n" + _RULES_SYNC_END


def _build_rules_md(resident_inner: str = "") -> str:
    """The generated RULES.md the cadence hook injects: each rule's reminder
    cell, falling back to the full rule text where the cell is empty, then the
    resident-standards block so the highest-weight standards ride every prompt
    injection alongside the rules."""
    bullets = "\n".join(
        f"- {(r['reminder'].strip() or r['rule'].strip())}" for r in _rules_rows()
    )
    out = _RULES_MD_NOTICE + "\n# Always-on rules\n\n" + bullets + "\n"
    if resident_inner:
        out += "\n" + _RESIDENT_ANCHOR + "\n\n" + resident_inner + "\n"
    return out


def _sync_rules_md(vault_root: Path, resident_inner: str = "") -> str:
    """Write the generated RULES.md. Returns written | unchanged."""
    rules_path = vault_root.resolve() / ".claude" / "RULES.md"
    body = _build_rules_md(resident_inner)
    if rules_path.exists() and rules_path.read_text(encoding="utf-8") == body:
        return "unchanged"
    _fh.write_text(rules_path, body)
    return "written"


# ---------------------------------------------------------------------------
# Resident standards — distill the highest-weight standard notes
# ---------------------------------------------------------------------------

def _frontmatter(text: str) -> dict[str, str]:
    """Parse the top-level scalar keys of a note's YAML frontmatter.

    Deliberately shallow: only `key: value` lines with a non-empty value are
    read (type, weight, reviewed), which is all the resident scan needs. List
    blocks (tags) are skipped without error.
    """
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict[str, str] = {}
    for line in m.group(1).splitlines():
        km = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if km and km.group(2).strip():
            fm[km.group(1)] = km.group(2).strip()
    return fm


def _fm_trigger(fm: dict[str, str]) -> str:
    """The note's optional one-line `trigger:`, unquoted.

    Surrounding YAML quotes are stripped so an author can quote a trigger holding
    a colon without the quote marks reaching the injected block. A missing or
    empty field yields "", which renders the standard's entry as a single line
    exactly as it did before the field existed.
    """
    raw = fm.get("trigger", "").strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        raw = raw[1:-1].strip()
    return raw


def _title_and_directive(text: str) -> tuple[str, str]:
    """The note's H1 title and the first sentence of its first body paragraph.

    Standard notes are authored directive-first (the opening sentence IS the
    instruction), so that sentence is the resident one-liner. Returns ("", "")
    when no H1 or paragraph is found.
    """
    body = _FRONTMATTER_RE.sub("", text, count=1).lstrip("\n")
    title = ""
    para: list[str] = []
    in_fence = False
    for line in body.splitlines():
        s = line.strip()
        # Skip fenced code: a Format-<Type> note is one template code block, so
        # its H1 and lead line live inside the fence and are not the standard.
        if s.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if not title:
            if s.startswith("# "):
                title = s[2:].strip()
            continue
        if not para:
            if s and not s.startswith("#"):
                para.append(s)
            continue
        if not s or s.startswith("#"):
            break
        para.append(s)
    paragraph = " ".join(para)
    cut = paragraph.find(". ")
    directive = paragraph if cut == -1 else paragraph[: cut + 1]
    return title, directive


def _resident_skip_dirs() -> set[str]:
    """Directory names the standards scan never descends into: tooling and
    history, plus the non-canonical staging trees — archive holds superseded
    copies, the inbox holds unreviewed drafts and user drops."""
    names = {".history", ".obsidian", ".redteam", ".claude", "__pycache__"}
    for semantic in ("archive", "inbox"):
        try:
            names.add(_folder_by_semantic(semantic))
        except Exception:
            pass
    return names


def _scan_weighted_standards(
    vault_root: Path,
) -> tuple[list[tuple[float, str, str, str, str]], int]:
    """Every canonical standard note carrying a numeric `weight`, as
    (weight, title, directive, axis, trigger), plus the count of notes skipped
    for not being valid UTF-8. `trigger` is the note's optional one-line
    `trigger:` field and is "" where the note carries none. Skips the
    draft/superseded/tooling trees and any note explicitly `reviewed: false`.

    A non-UTF-8 note is skipped and counted through the strict-decode-or-None
    primitive instead of raising UnicodeDecodeError — which would crash the whole
    sync before any Sync-Log entry while the PostToolUse hook swallows the error
    silently."""
    skip = _resident_skip_dirs()
    found: list[tuple[float, str, str, str, str]] = []
    skipped_nonutf8 = 0
    for path in vault_root.rglob("*.md"):
        if any(part in skip for part in path.parts):
            continue
        try:
            raw_text = _fh.read_text_or_none(path)
        except OSError:
            continue
        if raw_text is None:
            skipped_nonutf8 += 1
            continue
        # read_text_or_none preserves on-disk newlines (byte-exact); the
        # frontmatter/paragraph regexes below expect LF, so normalize the way
        # Path.read_text would have — keeping the resident output byte-identical.
        text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
        fm = _frontmatter(text)
        axis = fm.get("type")
        if axis not in _STANDARD_TYPES:
            continue
        if fm.get("reviewed", "").lower() == "false":
            continue
        raw = fm.get("weight")
        if raw is None:
            continue
        try:
            weight = float(raw)
        except ValueError:
            continue
        title, directive = _title_and_directive(text)
        if title and directive:
            found.append((weight, title, directive, axis, _fm_trigger(fm)))
    return found, skipped_nonutf8


def _resident_inner(standards: list[tuple[float, str, str, str, str]]) -> str:
    """The resident digest grouped by axis (voice/design/format), each axis its
    top-N by weight. Grouping keeps every axis represented — a global ranking
    lets a high-weight axis crowd the others out. Ties break by title so the
    output is stable run to run.

    A standard carrying a `trigger:` gets a second, subordinate line under its
    entry holding that trigger verbatim; a standard without one renders as the
    single headline line it always did."""
    sections: list[str] = []
    for axis_key, axis_label in _AXIS_LABELS:
        ranked = sorted(
            (s for s in standards if s[3] == axis_key),
            key=lambda r: (-r[0], r[1]),
        )[:_RESIDENT_TOP_N_PER_AXIS]
        if not ranked:
            continue
        lines: list[str] = []
        for _w, t, d, _a, trigger in ranked:
            lines.append(f"- **{t}** — {d}")
            if trigger:
                lines.append(_RESIDENT_TRIGGER_PREFIX + trigger)
        bullets = "\n".join(lines)
        sections.append(f"**{axis_label}**\n\n{bullets}")
    if not sections:
        return _RESIDENT_NOTE + "\n\n_No weighted standards found yet._"
    return _RESIDENT_NOTE + "\n\n" + "\n\n".join(sections)


def _build_resident_block(standards: list[tuple[float, str, str, str, str]]) -> str:
    """The marker-bounded `## Resident standards` block for CLAUDE.md/AGENTS.md."""
    return (
        _RESIDENT_SYNC_START + "\n" + _resident_inner(standards) + "\n" + _RESIDENT_SYNC_END
    )


# ---------------------------------------------------------------------------
# Sync-log helpers
# ---------------------------------------------------------------------------

def _config_hash(config_path: Path | None = None) -> str:
    """First 12 hex of the CONFIG.md sha256. Defaults to the loaded ``_CONFIG``;
    an explicit path lets the shape-refusal artifact hash CONFIG before
    config_variables (and thus ``_CONFIG``) is loaded."""
    path = config_path if config_path is not None else _CONFIG
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12]


_SYNC_LOG_HEADER = "# Sync Log\n\nAppend-only sync log. Each H2 is one run.\n\n"


def _ensure_sync_log(log_path: Path) -> None:
    """Create the sync log with its header if absent (safe LF write)."""
    if not log_path.exists():
        log_path.parent.mkdir(parents=True, exist_ok=True)
        _fh.write_text(log_path, _SYNC_LOG_HEADER)


def _append_sync_log(log_path: Path, entry: str) -> None:
    """Append one entry with ``open(..., "a")`` — never read-all/rewrite-all.

    Ends the O(n^2) growth and the concurrent-run clobber race of the old
    read-then-rewrite: existing bytes are untouched, and the append is LF
    (``newline="\\n"``) per the deploy newline canon.
    """
    _ensure_sync_log(log_path)
    with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
        fh.write(entry)


def _last_log_hash() -> str | None:
    """Return the config-hash from the most recent log entry, or None."""
    if _SYNC_LOG is None or not _SYNC_LOG.exists():
        return None
    text = _SYNC_LOG.read_text(encoding="utf-8")
    matches = list(re.finditer(r"config-hash:\s*([0-9a-f]{12})", text))
    return matches[-1].group(1) if matches else None


def _append_log_entry(
    cfg_hash: str,
    counts: dict[str, int],
    recovery: list[str],
    errors: list[str],
    no_changes: bool,
) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if no_changes:
        entry = f"## {ts} — no changes since previous run (config-hash: {cfg_hash})\n\n"
    else:
        recovery_str = ", ".join(recovery) if recovery else "none"
        error_str = ", ".join(errors) if errors else "none"
        lines = [
            f"## {ts} — config-hash: {cfg_hash}",
            "",
            f"- Sources: Types={counts['types']}, Folders={counts['folders']}, "
            f"Subfolders={counts['subfolders']}",
            f"- CLAUDE.md `## Session-start defaults`: {counts['claude_rows']} rows",
            f"- AGENTS.md `## Session-start defaults`: {counts['agents_rows']} rows",
            f"- Recovery: {recovery_str}",
            f"- Errors: {error_str}",
            "",
        ]
        entry = "\n".join(lines) + "\n"
    _append_sync_log(_SYNC_LOG, entry)


def _append_error_log(cfg_hash: str, error: str) -> None:
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = (
        f"## {ts} — config-hash: {cfg_hash}\n\n"
        f"- Errors: {error}\n\n"
    )
    _append_sync_log(_SYNC_LOG, entry)


def _append_shape_refusal(log_path: Path, cfg_hash: str, refusals: list[str]) -> None:
    """Record a shape-gate REFUSE as its own sync-log entry.

    A refused CONFIG otherwise freezes every downstream copy stale and silent —
    the PostToolUse hook swallows all sync output and exits 0. This entry is the
    artifact that makes the refusal visible in the ledger the way a normal run's
    entry is.
    """
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    lines = [
        f"## {ts} — config-shape REFUSED (config-hash: {cfg_hash})",
        "",
        "- Shape check refused; sync aborted — downstream copies left unchanged.",
    ]
    lines += [f"- REFUSE: {r}" for r in refusals]
    lines.append("")
    _append_sync_log(log_path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def _count_table_rows(body: str) -> int:
    """Count data rows in a markdown table body string (exclude header + separator)."""
    rows = 0
    past_header = False
    past_sep = False
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        if not past_header:
            past_header = True
            continue
        if not past_sep:
            past_sep = True
            continue
        rows += 1
    return rows


def _sync_file(md_path: Path, default_header: str, resident_body: str) -> tuple[bool, bool]:
    """Write the synced blocks into md_path. Returns (changed, found).

    Four marker-bounded blocks are written: the session-start Types table under
    ``## Session-start defaults``, the scheduled-agents table under
    ``## Scheduled agents``, the always-on rules under ``## Always-on rules``,
    and the resident-standards digest under ``## Resident standards``. Each is
    replaced independently, so user content around any block is preserved.
    `found` is True when the session-start block was already present (used for
    the recovery report).
    """
    session_body = _build_session_defaults_table()
    agents_body = _build_scheduled_agents_table()
    rules_body = _build_rules_orientation_block()
    if not md_path.exists():
        _fh.write_text(md_path, default_header)
    original = md_path.read_text(encoding="utf-8")

    updated, session_found = _replace_synced_block(
        original, session_body, _SYNC_START, _SYNC_END, _SESSION_ANCHOR
    )
    updated, _agents_found = _replace_synced_block(
        updated, agents_body, _AGENTS_SYNC_START, _AGENTS_SYNC_END, _AGENTS_ANCHOR
    )
    updated, _rules_found = _replace_synced_block(
        updated, rules_body, _RULES_SYNC_START, _RULES_SYNC_END, _RULES_ANCHOR
    )
    updated, _resident_found = _replace_synced_block(
        updated, resident_body, _RESIDENT_SYNC_START, _RESIDENT_SYNC_END, _RESIDENT_ANCHOR
    )
    changed = updated != original
    if changed:
        _fh.write_text(md_path, updated)
    return changed, session_found


def _extract_pipeline_block() -> str:
    """The master pipeline-protocol block from CONFIG, markers excluded."""
    start = _CONFIG_TEXT.find(_PIPE_SYNC_START)
    end = _CONFIG_TEXT.find(_PIPE_SYNC_END)
    if start == -1 or end == -1 or end <= start:
        raise ValueError(
            "CONFIG.md § Pipeline protocol markers not found — the master block is missing."
        )
    return _CONFIG_TEXT[start + len(_PIPE_SYNC_START):end].strip("\n")


def _pipeline_skill_paths(vault_root: Path) -> list[Path]:
    skills_root = vault_root.resolve() / ".claude" / "skills"
    return [skills_root / name / "SKILL.md" for name in _PIPELINE_SKILLS]


def _sync_pipeline_skills(vault_root: Path) -> list[tuple[str, str]]:
    """Stamp the CONFIG master block between each pipeline skill's markers.

    Replaces ONLY between existing markers — a skill without markers is reported,
    never appended to (block placement inside a skill is editorial).
    Returns (skill-name, outcome) pairs: stamped | unchanged | markers-missing | file-missing.
    """
    block = _extract_pipeline_block()
    results: list[tuple[str, str]] = []
    for path in _pipeline_skill_paths(vault_root):
        name = path.parent.name
        if not path.exists():
            results.append((name, "file-missing"))
            continue
        text = path.read_text(encoding="utf-8")
        s, e = text.find(_PIPE_SYNC_START), text.find(_PIPE_SYNC_END)
        if s == -1 or e == -1 or e <= s:
            results.append((name, "markers-missing"))
            continue
        updated = (
            text[: s + len(_PIPE_SYNC_START)] + "\n" + block + "\n" + text[e:]
        )
        if updated != text:
            _fh.write_text(path, updated)
            results.append((name, "stamped"))
        else:
            results.append((name, "unchanged"))
    return results


def _sync_harness_permissions(vault_root: Path) -> str:
    """Mirror CONFIG § Harness permissions into settings.local.json (merge).

    Reads the `rule` column of the § Harness permissions table and unions it
    into permissions.allow — hand-added entries are preserved, never removed.
    A missing section is reported, not an error (older CONFIG). Returns one of:
    written | unchanged | section-missing.
    """
    import json

    try:
        rows = _parse_table(_CONFIG_TEXT, "Harness permissions", ["rule"])
    except Exception:
        return "section-missing"
    rules = [r["rule"].strip().strip("`") for r in rows if r["rule"].strip()]
    if not rules:
        return "section-missing"

    settings_path = vault_root.resolve() / ".claude" / "settings.local.json"
    data: dict = {}
    if settings_path.exists():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
        except Exception:
            return "section-missing"  # never clobber an unparseable file
    perms = data.setdefault("permissions", {})
    allow = perms.setdefault("allow", [])
    before = list(allow)
    for rule in rules:
        if rule not in allow:
            allow.append(rule)
    if allow == before:
        return "unchanged"
    _fh.write_text(settings_path, json.dumps(data, indent=2) + "\n")
    return "written"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Sync the CONFIG.md session-start table into CLAUDE.md and AGENTS.md."
    )
    parser.add_argument(
        "--vault-root",
        type=Path,
        default=None,
        help="Vault root whose `.claude/CLAUDE.md` and `.claude/AGENTS.md` to target. "
             "Falls back to NOTE_KIT_VAULT_ROOT, then to detecting the installed "
             "layout from this script's location; errors out if none resolves.",
    )
    args = parser.parse_args()

    # ------------------------------------------------------------------
    # Shape gate — runs here, not at module import, so `import sync_config`
    # never mutates CONFIG.md. It precedes the config_variables parse so a
    # repair lands before the parsers read CONFIG, and a refusal is caught
    # without a parseable CONFIG.
    # ------------------------------------------------------------------
    config_path = config_shape.default_config_path()
    shape_report = config_shape.run(config_path)
    for _r in shape_report.repairs:
        print(f"[config-shape] REPAIR  {_r}")
    if not shape_report.ok:
        for _r in shape_report.refusals:
            print(f"[config-shape] REFUSE  {_r}", file=sys.stderr)
        # The refusal leaves an artifact: a Sync-Log entry (the log path is
        # resolvable without parsing CONFIG) so a refused shape does not freeze
        # every downstream copy stale and silent — the hook swallows all output.
        log_path = _refusal_sync_log_path(args.vault_root)
        if log_path is not None:
            try:
                _append_shape_refusal(
                    log_path, _config_hash(config_path), shape_report.refusals
                )
            except Exception:
                pass
        print(
            "ERROR: sync aborted — CONFIG.md failed the shape check "
            f"({len(shape_report.refusals)} unsafe drift(s)). Fix CONFIG.md and re-run.",
            file=sys.stderr,
        )
        sys.exit(2)

    # CONFIG is shape-clean; parse it now (the first config_variables import,
    # so the parsers read the shape-repaired file).
    _load_config_variables()

    # CLI overrides env overrides detection; resolve so every downstream helper
    # (which reads these globals) sees the chosen layout.
    vault_root = resolve_vault_root(args.vault_root)
    if vault_root is None:
        print(f"ERROR: {_NO_VAULT_ROOT_ERROR}", file=sys.stderr)
        sys.exit(1)
    global _CLAUDE_MD, _AGENTS_MD, _SYNC_LOG
    _CLAUDE_MD, _AGENTS_MD, _SYNC_LOG = _resolve_targets(Path(vault_root))

    # Resident standards: scan the vault's weighted standard notes once, then
    # build both forms — the marker-bounded block (CLAUDE.md/AGENTS.md) and the
    # bare bullet list (RULES.md, the per-prompt hook injection).
    standards, skipped_standards = _scan_weighted_standards(vault_root)
    if skipped_standards:
        # Surface the skip visibly (stdout) so a dropped standard note is never
        # invisible; the sync still completes and logs its run entry.
        print(f"  resident-standards: skipped {skipped_standards} "
              f"non-UTF-8 standard note(s)")
    resident_block = _build_resident_block(standards)
    resident_inner = _resident_inner(standards)

    cfg_hash = _config_hash()
    recovery: list[str] = []
    errors: list[str] = []

    # ------------------------------------------------------------------
    # Build content block (single shared table)
    # ------------------------------------------------------------------
    try:
        table_body = _build_session_defaults_table()
    except Exception as exc:
        err_msg = f"Config parse/build failure: {exc}"
        print(f"ERROR: {err_msg}", file=sys.stderr)
        try:
            _append_error_log(cfg_hash, err_msg)
        except Exception:
            pass
        sys.exit(1)

    # ------------------------------------------------------------------
    # CLAUDE.md and AGENTS.md — replace ## Session-start defaults
    # ------------------------------------------------------------------
    claude_changed, claude_found = _sync_file(
        _CLAUDE_MD,
        "# Vault\n\nSession-start primer.\n\n",
        resident_block,
    )
    if not claude_found:
        recovery.append(f"CLAUDE.md {_SESSION_ANCHOR}")

    agents_changed, agents_found = _sync_file(
        _AGENTS_MD,
        "# Vault\n\nVocabulary primer for sub-agents and fresh runners.\n\n",
        resident_block,
    )
    if not agents_found:
        recovery.append(f"AGENTS.md {_SESSION_ANCHOR}")

    # ------------------------------------------------------------------
    # Pipeline-protocol block → the staged pipeline skills
    # ------------------------------------------------------------------
    try:
        pipe_results = _sync_pipeline_skills(vault_root)
    except ValueError as exc:
        pipe_results = []
        errors.append(str(exc))
    pipe_changed = any(r == "stamped" for _, r in pipe_results)
    for name, outcome in pipe_results:
        if outcome in ("markers-missing", "file-missing"):
            errors.append(f"pipeline-protocol {name}: {outcome}")

    # ------------------------------------------------------------------
    # § Harness permissions → settings.local.json (merge)
    # ------------------------------------------------------------------
    harness_outcome = _sync_harness_permissions(vault_root)

    # ------------------------------------------------------------------
    # § Rules → generated RULES.md (reminder column, full text where empty)
    # ------------------------------------------------------------------
    try:
        rules_outcome = _sync_rules_md(vault_root, resident_inner)
    except Exception as exc:
        rules_outcome = "error"
        errors.append(f"rules-md: {exc}")

    # ------------------------------------------------------------------
    # Counts
    # ------------------------------------------------------------------
    counts = {
        "types": len(TYPES),
        "folders": len(FOLDER_ROUTING),
        "subfolders": len(SUBFOLDERS),
        "claude_rows": _count_table_rows(table_body),
        "agents_rows": _count_table_rows(table_body),
    }

    # ------------------------------------------------------------------
    # Idempotency check: is this a no-change run?
    # ------------------------------------------------------------------
    no_changes = (
        not claude_changed
        and not agents_changed
        and not pipe_changed
        and harness_outcome != "written"
        and rules_outcome != "written"
        and _last_log_hash() == cfg_hash
    )

    # ------------------------------------------------------------------
    # Log
    # ------------------------------------------------------------------
    _append_log_entry(cfg_hash, counts, recovery, errors, no_changes)

    # ------------------------------------------------------------------
    # Summary to stdout
    # ------------------------------------------------------------------
    if no_changes:
        print(f"No changes since previous run (config-hash: {cfg_hash}).")
    else:
        print(f"sync_config complete (config-hash: {cfg_hash})")
        print(f"  CLAUDE.md {'written' if claude_changed else 'unchanged'}: "
              f"{counts['claude_rows']} rows in `{_SESSION_ANCHOR}`")
        print(f"  AGENTS.md {'written' if agents_changed else 'unchanged'}: "
              f"{counts['agents_rows']} rows in `{_SESSION_ANCHOR}`")
        for name, outcome in pipe_results:
            print(f"  pipeline-protocol -> {name}: {outcome}")
        print(f"  harness-permissions -> settings.local.json: {harness_outcome}")
        print(f"  rules -> RULES.md: {rules_outcome}")
        print(
            f"  resident-standards: {len(standards)} weighted, "
            f"top {_RESIDENT_TOP_N_PER_AXIS}/axis resident"
        )
        if recovery:
            print(f"  Recovered (appended) sections: {recovery}")
        if errors:
            print(f"  Errors: {errors}", file=sys.stderr)
        print(f"  Sync log: {_SYNC_LOG}")


if __name__ == "__main__":
    main()
