#!/usr/bin/env python3
"""Generate `vocabulary.json` for the vault-search daemon from CONFIG.md.

The daemon runs in its own venv, isolated from the kit's `config_variables.py`,
so this parses CONFIG.md's § Folders and § Types tables directly (standard
library only) rather than importing that module — the "generate rather than
import" call the daemon's isolation forces. It is the ONE source for the
daemon's folder vocabulary, inbox name, and canonical type set: `vocabulary.py`
reads the result at runtime for indexer.py / tools.py / topology.py, and
`install_daemon.py` fills config.yaml's inbox-relative values from it.

CONFIG's § Folders rows map a `<wildcard>` (e.g. `<inbox>`) to a `literal`
on-disk folder name (e.g. `Inbox`). CONFIG's § Types rows list the canonical
note-type keys in their first column. This writes:

    {
      "inbox_folder": "Inbox",
      "roots": { "<semantic>": "<literal>", ... },   # keyed by bare wildcard
      "types": ["project", "area", ...]
    }

Usage:
    python generate_vocabulary.py [--config <CONFIG.md>] [--out <vocabulary.json>]

Defaults: CONFIG.md is the one two parents up at <vault>/.claude/CONFIG.md;
the output is vocabulary.json beside this script.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_DAEMON_DIR = Path(__file__).resolve().parent          # .claude/vault-search
_CLAUDE_DIR = _DAEMON_DIR.parent                        # .claude
_DEFAULT_CONFIG = _CLAUDE_DIR / "CONFIG.md"
_DEFAULT_OUT = _DAEMON_DIR / "vocabulary.json"

# Minimum row counts below which a parse is treated as shrunken (a silent
# vocabulary shrink is the classify-by-folder bug). Keep these >= the sizes of
# vocabulary.py's _FALLBACK_ROOTS (7) and _FALLBACK_TYPES (18); a legitimate
# CONFIG change that lowers a canonical count must bump both together.
_MIN_ROOTS = 7
_MIN_TYPES = 18


def _section(text: str, heading: str) -> str:
    """Return the body of a `## <heading>` section up to the next `## `."""
    m = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    if m is None:
        raise ValueError(f"CONFIG.md: section '## {heading}' not found")
    after = text[m.end():]
    nxt = re.search(r"^## ", after, re.MULTILINE)
    return after[: nxt.start()] if nxt else after


def _table_rows(section: str) -> list[list[str]]:
    """Return the data rows (cells) of the first markdown table in `section`,
    skipping the header row and the `|---|` separator."""
    rows: list[list[str]] = []
    seen_header = False
    for line in section.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            if seen_header and rows:
                break  # table ended
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if set("".join(cells)) <= {"-", ":", " "}:
            continue  # separator row
        if not seen_header:
            seen_header = True  # first pipe row is the header
            header = cells
            continue
        rows.append(cells)
    return rows if rows else []


def _strip_backticks(cell: str) -> str:
    return cell.strip().strip("`").strip()


def parse_folder_vocabulary(config_text: str) -> tuple[dict[str, str], str]:
    """Parse § Folders → ({semantic: literal}, inbox_folder).

    `semantic` is the wildcard with its angle brackets stripped (`<inbox>` ->
    `inbox`); `literal` is the on-disk folder name.
    """
    section = _section(config_text, "Folders")
    header = None
    for line in section.splitlines():
        s = line.strip()
        if s.startswith("|"):
            header = [c.strip().lower() for c in s.strip("|").split("|")]
            break
    if not header:
        raise ValueError("§ Folders: no table header found")
    if "wildcard" not in header:
        raise ValueError(f"§ Folders: header {header!r} lacks a 'wildcard' column")
    literal_col = "literal" if "literal" in header else (
        "folder" if "folder" in header else None
    )
    if literal_col is None:
        raise ValueError(f"§ Folders: header {header!r} lacks a 'literal'/'folder' column")
    wc_idx = header.index("wildcard")
    lit_idx = header.index(literal_col)

    roots: dict[str, str] = {}
    skipped: list[list[str]] = []
    for cells in _table_rows(section):
        if len(cells) <= max(wc_idx, lit_idx):
            skipped.append(cells)
            continue
        wildcard = _strip_backticks(cells[wc_idx])
        literal = _strip_backticks(cells[lit_idx])
        semantic = wildcard.strip("<>").strip()
        if not semantic or not literal:
            skipped.append(cells)
            continue
        roots[semantic] = literal
    # A skipped row is a silent vocabulary shrink — report it loudly and refuse.
    for row in skipped:
        print(f"[vocab] § Folders: SKIPPED malformed row: {row!r}", file=sys.stderr)
    if skipped:
        raise ValueError(
            f"§ Folders: {len(skipped)} malformed row(s) skipped; refusing to ship "
            "a shrunken folder vocabulary"
        )
    if len(roots) < _MIN_ROOTS:
        raise ValueError(
            f"§ Folders: parsed {len(roots)} roots, need >= {_MIN_ROOTS}"
        )
    inbox = roots.get("inbox")
    if not inbox:
        raise ValueError("§ Folders: no '<inbox>' wildcard row found")
    return roots, inbox


def parse_type_vocabulary(config_text: str) -> list[str]:
    """Parse § Types → the canonical type keys (first column, in table order)."""
    section = _section(config_text, "Types")
    types: list[str] = []
    skipped: list[list[str]] = []
    for cells in _table_rows(section):
        key = _strip_backticks(cells[0]).lower() if cells else ""
        if re.fullmatch(r"[a-z][a-z0-9]*", key):
            types.append(key)
        else:
            skipped.append(cells)
    # A dropped type row silently reintroduces classify-by-folder — report and refuse.
    for row in skipped:
        print(f"[vocab] § Types: SKIPPED malformed row: {row!r}", file=sys.stderr)
    if skipped:
        raise ValueError(
            f"§ Types: {len(skipped)} malformed row(s) skipped; refusing to ship "
            "a shrunken type vocabulary"
        )
    if len(types) < _MIN_TYPES:
        raise ValueError(
            f"§ Types: parsed {len(types)} types, need >= {_MIN_TYPES}"
        )
    return types


def build_vocabulary(config_text: str) -> dict:
    roots, inbox = parse_folder_vocabulary(config_text)
    types = parse_type_vocabulary(config_text)
    return {"inbox_folder": inbox, "roots": roots, "types": types}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate vocabulary.json from CONFIG § Folders and § Types."
    )
    parser.add_argument("--config", type=Path, default=None,
                        help="Path to CONFIG.md (default: <vault>/.claude/CONFIG.md).")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output path (default: vocabulary.json beside this script).")
    args = parser.parse_args()

    config_path = (args.config or _DEFAULT_CONFIG).resolve()
    out_path = (args.out or _DEFAULT_OUT).resolve()

    if not config_path.is_file():
        print(f"[vocab] ERROR: CONFIG.md not found at {config_path}", file=sys.stderr)
        return 1
    try:
        vocab = build_vocabulary(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[vocab] ERROR parsing {config_path}: {exc}", file=sys.stderr)
        return 1

    out_path.write_text(
        json.dumps(vocab, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8", newline="\n",
    )
    print(f"[vocab] Wrote {out_path}")
    print(f"          inbox_folder -> {vocab['inbox_folder']}")
    print(f"          roots        -> {vocab['roots']}")
    print(f"          types        -> {len(vocab['types'])} keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
