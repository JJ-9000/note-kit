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

# Import after sys.path is updated
from config_variables import (  # noqa: E402
    FILE_HANDLING,
    TYPES,
    FOLDER_ROUTING,
    SUBFOLDERS,
    _folder_by_semantic,
    _CONFIG_TEXT,
    _parse_table,
)

# Use the SAME CONFIG.md config_variables resolved (honors the NOTE_KIT_CONFIG
# override), not a second hardcoded kit-root path, so the hash and the parsed
# tables come from one source.
from config_variables import _CONFIG as _CONFIG  # noqa: E402
_ARCHIVE_FOLDER = _folder_by_semantic("archive")


def _detect_installed_vault_root() -> Path | None:
    """Detect the installed layout from this script's own location.

    Installed, the kit root IS a vault's `.claude/` directory, so the vault
    root is its parent — recognized by the vault archive sitting there (the
    sync log's destination root). The kit-source checkout shares the `.claude`
    name but its parent is a repo with no archive, so it is deliberately not
    detected; bare runs there must name a target via --vault-root or
    NOTE_KIT_VAULT_ROOT.
    """
    if _KIT_ROOT.name == ".claude":
        candidate = _KIT_ROOT.parent
        if (candidate / _ARCHIVE_FOLDER).is_dir():
            return candidate
    return None


def _vault_root_from_env() -> Path | None:
    raw = os.environ.get("NOTE_KIT_VAULT_ROOT")
    return Path(raw) if raw else None


def _resolve_vault_root(cli_root: Path | None) -> Path | None:
    """Vault root, in priority order: --vault-root, env var, installed layout."""
    if cli_root is not None:
        return cli_root
    env_root = _vault_root_from_env()
    if env_root is not None:
        return env_root
    return _detect_installed_vault_root()


def _resolve_targets(vault_root: Path) -> tuple[Path, Path, Path]:
    """Resolve (CLAUDE.md, AGENTS.md, Sync-Log.md) under the vault root.

    The orientation pair lives at ``<vault-root>/.claude/`` and the sync log
    under the vault's archive. There is no pathless fallback: callers must
    resolve a vault root first (see ``_resolve_vault_root``) so the log can
    never land outside a vault.
    """
    vault_root = vault_root.resolve()
    claude_md = vault_root / ".claude" / "CLAUDE.md"
    agents_md = vault_root / ".claude" / "AGENTS.md"
    sync_log = vault_root / _ARCHIVE_FOLDER / "99-Logs" / "Sync-Log.md"
    return claude_md, agents_md, sync_log


_NO_VAULT_ROOT_ERROR = (
    "No vault root resolved. Pass --vault-root, set NOTE_KIT_VAULT_ROOT, or run "
    "the copy installed at <vault>/.claude/scripts/ (detected by the vault "
    f"archive folder beside the kit root). Kit root: {_KIT_ROOT}. "
    "Refusing to write a sync log outside a vault."
)

# Default (import-time) targets honor the env var and the installed-layout
# detection so importers and the session-end hook pick up an installed layout
# without extra wiring. Left as None when unresolvable — main() re-resolves
# with the CLI arg and errors out cleanly if a vault root still can't be found.
_IMPORT_VAULT_ROOT = _resolve_vault_root(None)
if _IMPORT_VAULT_ROOT is not None:
    _CLAUDE_MD, _AGENTS_MD, _SYNC_LOG = _resolve_targets(_IMPORT_VAULT_ROOT)
else:
    _CLAUDE_MD = _AGENTS_MD = _SYNC_LOG = None

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
    canonical, display-ready home (`<projects>`, `parent's 00-Voice`, `00-Ideas`,
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


def _build_rules_md() -> str:
    """The generated RULES.md the cadence hook injects: each rule's reminder
    cell, falling back to the full rule text where the cell is empty."""
    bullets = "\n".join(
        f"- {(r['reminder'].strip() or r['rule'].strip())}" for r in _rules_rows()
    )
    return _RULES_MD_NOTICE + "\n# Always-on rules\n\n" + bullets + "\n"


def _sync_rules_md(vault_root: Path) -> str:
    """Write the generated RULES.md. Returns written | unchanged."""
    rules_path = vault_root.resolve() / ".claude" / "RULES.md"
    body = _build_rules_md()
    if rules_path.exists() and rules_path.read_text(encoding="utf-8") == body:
        return "unchanged"
    rules_path.write_text(body, encoding="utf-8")
    return "written"


# ---------------------------------------------------------------------------
# Sync-log helpers
# ---------------------------------------------------------------------------

def _config_hash() -> str:
    raw = _CONFIG.read_bytes()
    return hashlib.sha256(raw).hexdigest()[:12]


def _ensure_sync_log() -> None:
    """Create the sync log with header if it does not exist."""
    if not _SYNC_LOG.exists():
        _SYNC_LOG.parent.mkdir(parents=True, exist_ok=True)
        _SYNC_LOG.write_text(
            "# Sync Log\n\nAppend-only sync log. Each H2 is one run.\n\n",
            encoding="utf-8",
        )


def _last_log_hash() -> str | None:
    """Return the config-hash from the most recent log entry, or None."""
    if not _SYNC_LOG.exists():
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
    _ensure_sync_log()
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

    existing = _SYNC_LOG.read_text(encoding="utf-8")
    _SYNC_LOG.write_text(existing + entry, encoding="utf-8")


def _append_error_log(cfg_hash: str, error: str) -> None:
    _ensure_sync_log()
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entry = (
        f"## {ts} — config-hash: {cfg_hash}\n\n"
        f"- Errors: {error}\n\n"
    )
    existing = _SYNC_LOG.read_text(encoding="utf-8")
    _SYNC_LOG.write_text(existing + entry, encoding="utf-8")


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


def _sync_file(md_path: Path, default_header: str) -> tuple[bool, bool]:
    """Write both synced blocks into md_path. Returns (changed, found).

    Two marker-bounded blocks are written: the session-start Types table under
    ``## Session-start defaults`` and the scheduled-agents table under
    ``## Scheduled agents``. Each is replaced independently, so user content
    around either block is preserved. `found` is True when the session-start
    block was already present (used for the recovery report).
    """
    session_body = _build_session_defaults_table()
    agents_body = _build_scheduled_agents_table()
    rules_body = _build_rules_orientation_block()
    if not md_path.exists():
        md_path.write_text(default_header, encoding="utf-8")
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
    changed = updated != original
    if changed:
        md_path.write_text(updated, encoding="utf-8")
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
            path.write_text(updated, encoding="utf-8")
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
    settings_path.write_text(
        json.dumps(data, indent=2) + "\n", encoding="utf-8"
    )
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

    # CLI overrides env overrides detection; re-resolve so every downstream
    # helper (which reads these globals) sees the chosen layout.
    vault_root = _resolve_vault_root(args.vault_root)
    if vault_root is None:
        print(f"ERROR: {_NO_VAULT_ROOT_ERROR}", file=sys.stderr)
        sys.exit(1)
    global _CLAUDE_MD, _AGENTS_MD, _SYNC_LOG
    _CLAUDE_MD, _AGENTS_MD, _SYNC_LOG = _resolve_targets(vault_root)

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
    )
    if not claude_found:
        recovery.append(f"CLAUDE.md {_SESSION_ANCHOR}")

    agents_changed, agents_found = _sync_file(
        _AGENTS_MD,
        "# Vault\n\nVocabulary primer for sub-agents and fresh runners.\n\n",
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
        rules_outcome = _sync_rules_md(vault_root)
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
        if recovery:
            print(f"  Recovered (appended) sections: {recovery}")
        if errors:
            print(f"  Errors: {errors}", file=sys.stderr)
        print(f"  Sync log: {_SYNC_LOG}")


if __name__ == "__main__":
    main()
