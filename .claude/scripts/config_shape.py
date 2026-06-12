"""
config_shape.py
===============

Shape corrector for CONFIG.md's load-bearing tables. Runs at the head of
`sync_config.py` (sync aborts on a refusal) and stands alone:

    python .claude/scripts/config_shape.py [path-to-CONFIG.md] [--check-only]

Validates the four sections every parser depends on against an expected
skeleton:

  - § Folders   — the roots table (`wildcard` + `literal` columns present) and
                  the token table beneath it; every required token defined
                  exactly once: inbox, outbox, projects, areas, reference,
                  snippets, archive, user-queue, machine-queue.
  - § Types     — `type` column present; every type key non-empty and unique.
  - § Numbering — the legacy-marker table (`marker` column) parseable.
  - § Rules     — `rule` + `reminder` columns present; every rule non-empty.

Per row it checks the cell count against the header, the separator row, and
that literals parse (no unclosed backticks, no empty load-bearing cells).

Two outcomes, strictly separated:

  REPAIRS (safe drift, fixed in place and reported) — damage a parser would
  silently misread but whose intent is unambiguous: a row missing its leading
  or trailing pipe, tabs inside a table, trailing whitespace, and a missing or
  malformed separator row (wrong dashes, wrong cell count). A repaired table is
  re-emitted with aligned columns, which also normalizes stray spacing.
  Cosmetic intra-cell padding on an otherwise structurally sound table is not
  load-bearing (every parser strips it) and is deliberately left untouched, so
  a clean CONFIG round-trips byte-identical.

  REFUSALS (unsafe drift, reported with file:line and a nonzero exit) — damage
  whose repair would require guessing intent: a missing section, table, or
  required column; a required token missing; a duplicate token, type, or
  marker; a data row whose cell count disagrees with its header (a pipe lost
  mid-row merges two cells — unrecoverable); an unclosed backtick; an empty
  load-bearing cell.

Stdlib only. Exit codes: 0 = clean (repairs allowed), 2 = refused.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_KIT_ROOT = _SCRIPTS_DIR.parent

# Tokens that must each be defined exactly once across the § Folders roots
# table (`wildcard` column) and the token table beneath it (`token` column).
REQUIRED_TOKENS = (
    "inbox", "outbox", "projects", "areas", "reference", "snippets",
    "archive", "user-queue", "machine-queue",
)

# Required columns per checked section's first table. § Folders' token table is
# located separately by its own header (`token` | `resolves to`).
_SECTION_COLUMNS = {
    "Folders": ("wildcard", "literal"),
    "Types": ("type",),
    "Numbering": ("marker",),
    "Rules": ("rule", "reminder"),
}

# Columns whose cells must never be empty (load-bearing literals).
_NONEMPTY_COLUMNS = {
    "Folders": ("wildcard", "literal"),
    "Types": ("type",),
    "Numbering": ("marker",),
    "Rules": ("rule",),
}


@dataclass
class ShapeReport:
    repairs: list[str] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    changed: bool = False

    @property
    def ok(self) -> bool:
        return not self.refusals


def default_config_path() -> Path:
    """The CONFIG.md this kit reads — the NOTE_KIT_CONFIG override when set,
    else the file beside this script's kit root (mirrors config_variables)."""
    override = os.environ.get("NOTE_KIT_CONFIG")
    return Path(override).resolve() if override else (_KIT_ROOT / "CONFIG.md")


# ---------------------------------------------------------------------------
# Table mechanics
# ---------------------------------------------------------------------------

_SEP_CELL_RE = re.compile(r"^:?-+:?$")


def _is_table_line(line: str) -> bool:
    s = line.strip()
    return s.startswith("|") or s.endswith("|") or s.count("|") >= 2


def _split_cells(line: str) -> list[str]:
    """Cells of a pipe row, outer pipes stripped, content stripped."""
    inner = line.strip()
    if inner.startswith("|"):
        inner = inner[1:]
    if inner.endswith("|"):
        inner = inner[:-1]
    return [c.strip() for c in inner.split("|")]


def _is_separator_row(line: str) -> bool:
    cells = _split_cells(line)
    return bool(cells) and all(_SEP_CELL_RE.match(c or "") for c in cells)


def _looks_separator_ish(line: str) -> bool:
    """A damaged separator row: dashes present, nothing alphanumeric."""
    inner = line.strip().strip("|")
    return "-" in inner and not re.search(r"[A-Za-z0-9`<>]", inner)


def _render_table(header: list[str], rows: list[list[str]]) -> list[str]:
    """Canonical re-emit: pipe-bounded, single canonical separator, columns
    padded to the widest cell (min 3)."""
    widths = [max(3, len(h)) for h in header]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def emit(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(w) for c, w in zip(cells, widths)) + " |"

    out = [emit(header), "| " + " | ".join("-" * w for w in widths) + " |"]
    out.extend(emit(r) for r in rows)
    return out


class _Table:
    """One markdown table with its original line span [start, end)."""

    def __init__(self, lines: list[str], start: int, end: int):
        self.start = start
        self.end = end
        self.raw = lines[start:end]
        self.header = _split_cells(self.raw[0])
        self.sep_idx: int | None = None
        self.data: list[tuple[int, list[str]]] = []  # (orig line idx, cells)
        for off, line in enumerate(self.raw[1:], start=1):
            if off == 1 and (_is_separator_row(line) or _looks_separator_ish(line)):
                self.sep_idx = start + off
                continue
            self.data.append((start + off, _split_cells(line)))


def _find_tables(lines: list[str], lo: int, hi: int) -> list[_Table]:
    """All pipe tables between line indexes [lo, hi)."""
    tables: list[_Table] = []
    i = lo
    while i < hi:
        if _is_table_line(lines[i]) and "|" in lines[i]:
            j = i
            while j < hi and lines[j].strip() and _is_table_line(lines[j]):
                j += 1
            tables.append(_Table(lines, i, j))
            i = j
        else:
            i += 1
    return tables


def _section_span(lines: list[str], heading: str) -> tuple[int, int] | None:
    """[start, end) line indexes of an H2 section's body."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() == f"## {heading}":
            start = i + 1
            break
    if start is None:
        return None
    end = len(lines)
    for i in range(start, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return start, end


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _pre_repair_lines(lines: list[str], spans: list[tuple[int, int]],
                      report: ShapeReport, where: str) -> None:
    """Line-level safe repairs inside the checked sections only: missing outer
    pipes on a table row, tabs, trailing whitespace."""
    for lo, hi in spans:
        for i in range(lo, min(hi, len(lines))):
            line = lines[i]
            if "|" not in line:
                continue
            fixed = line.rstrip()
            if fixed != line:
                report.repairs.append(
                    f"{where}:{i + 1}: trimmed trailing whitespace")
            if "\t" in fixed:
                fixed = fixed.replace("\t", " ")
                report.repairs.append(f"{where}:{i + 1}: replaced tab with space")
            s = fixed.strip()
            # A table row missing an outer pipe (≥2 pipes = clearly tabular).
            if s.count("|") >= 2 and (not s.startswith("|") or not s.endswith("|")):
                if not s.startswith("|"):
                    s = "| " + s
                    report.repairs.append(f"{where}:{i + 1}: restored leading pipe")
                if not s.endswith("|"):
                    s = s + " |"
                    report.repairs.append(f"{where}:{i + 1}: restored trailing pipe")
                fixed = s
            if fixed != line:
                lines[i] = fixed
                report.changed = True


def _check_table(tbl: _Table, section: str, lines: list[str],
                 report: ShapeReport, where: str,
                 required_cols: tuple[str, ...],
                 nonempty_cols: tuple[str, ...]) -> bool:
    """Structural checks + separator/alignment repair for one table.
    Returns True when the table is usable after repair."""
    usable = True
    header = tbl.header
    missing = [c for c in required_cols if c not in header]
    if missing:
        report.refusals.append(
            f"{where}:{tbl.start + 1}: § {section} table header {header!r} is "
            f"missing required column(s) {missing!r}")
        return False

    needs_reemit = False

    # Separator row: missing entirely, or present but malformed.
    if tbl.sep_idx is None:
        report.repairs.append(
            f"{where}:{tbl.start + 2}: § {section} separator row missing — inserted")
        needs_reemit = True
    else:
        sep_line = lines[tbl.sep_idx]
        sep_cells = _split_cells(sep_line)
        if len(sep_cells) != len(header) or not _is_separator_row(sep_line):
            report.repairs.append(
                f"{where}:{tbl.sep_idx + 1}: § {section} separator row malformed — rebuilt")
            needs_reemit = True

    # Data rows: cell count must match the header exactly.
    clean_rows: list[list[str]] = []
    for line_idx, cells in tbl.data:
        if len(cells) != len(header):
            report.refusals.append(
                f"{where}:{line_idx + 1}: § {section} row has {len(cells)} cell(s), "
                f"header has {len(header)} — a lost or added mid-row pipe cannot "
                f"be repaired safely")
            usable = False
            continue
        # Unclosed backtick in any cell.
        for col, cell in zip(header, cells):
            if cell.count("`") % 2 == 1:
                report.refusals.append(
                    f"{where}:{line_idx + 1}: § {section} column {col!r} cell "
                    f"{cell!r} has an unclosed backtick")
                usable = False
        # Empty load-bearing cell.
        for col in nonempty_cols:
            if col in header and not cells[header.index(col)]:
                report.refusals.append(
                    f"{where}:{line_idx + 1}: § {section} load-bearing column "
                    f"{col!r} is empty")
                usable = False
        clean_rows.append(cells)

    if needs_reemit and usable:
        new_block = _render_table(header, clean_rows)
        lines[tbl.start:tbl.end] = new_block
        report.changed = True

    return usable


def _strip_token(cell: str) -> str:
    return cell.strip().strip("`").strip().strip("<>").strip()


def run(config_path: Path, write: bool = True) -> ShapeReport:
    """Validate (and safely repair) one CONFIG.md. Repairs are written back
    only when `write` is True and no refusal was raised."""
    report = ShapeReport()
    where = str(config_path)
    if not config_path.is_file():
        report.refusals.append(f"{where}: not found")
        return report

    text = config_path.read_text(encoding="utf-8")
    had_trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if had_trailing_newline and lines and lines[-1] == "":
        lines.pop()

    spans: list[tuple[int, int]] = []
    section_spans: dict[str, tuple[int, int]] = {}
    for section in _SECTION_COLUMNS:
        span = _section_span(lines, section)
        if span is None:
            report.refusals.append(f"{where}: section '## {section}' not found")
            continue
        section_spans[section] = span
        spans.append(span)

    _pre_repair_lines(lines, spans, report, where)

    token_definitions: dict[str, list[int]] = {}

    for section, (lo, hi) in section_spans.items():
        tables = _find_tables(lines, lo, hi)
        if not tables:
            report.refusals.append(
                f"{where}: section '## {section}' has no markdown table")
            continue

        usable = _check_table(
            tables[0], section, lines, report, where,
            _SECTION_COLUMNS[section], _NONEMPTY_COLUMNS[section],
        )
        # Re-scan: a re-emitted table can shift line numbers for later tables.
        tables = _find_tables(lines, lo, hi)

        if section == "Folders":
            # Token collection runs even when the table carried a refusal:
            # every row that individually parses still defines its token, so
            # the missing/duplicate report below names the REAL gaps instead
            # of declaring every token missing because one row broke.
            if "wildcard" in tables[0].header:
                roots = tables[0]
                wc_i = roots.header.index("wildcard")
                for line_idx, cells in roots.data:
                    if len(cells) != len(roots.header):
                        continue
                    tok = _strip_token(cells[wc_i])
                    if tok:
                        token_definitions.setdefault(tok, []).append(line_idx + 1)
            # The token table: the section's next table whose header starts
            # with `token`.
            token_tbl = next(
                (t for t in tables[1:]
                 if t.header and _strip_token(t.header[0]).lower() == "token"),
                None,
            )
            if token_tbl is None:
                report.refusals.append(
                    f"{where}: § Folders token table (header `token | resolves "
                    f"to`) not found")
            else:
                _check_table(token_tbl, "Folders (token table)", lines,
                             report, where, ("token",), ("token",))
                for line_idx, cells in token_tbl.data:
                    if len(cells) != len(token_tbl.header):
                        continue
                    tok = _strip_token(cells[0])
                    if tok:
                        token_definitions.setdefault(tok, []).append(line_idx + 1)
                    if tok in ("user-queue", "machine-queue"):
                        value = re.search(r"`([^`]+)`", cells[1] if len(cells) > 1 else "")
                        if not value or not value.group(1).strip():
                            report.refusals.append(
                                f"{where}:{line_idx + 1}: `<{tok}>` resolves "
                                f"to no parseable backticked literal")

        if section == "Types" and usable:
            seen: dict[str, int] = {}
            t_i = tables[0].header.index("type")
            for line_idx, cells in tables[0].data:
                if len(cells) != len(tables[0].header):
                    continue
                key = cells[t_i].strip().lower()
                if key in seen:
                    report.refusals.append(
                        f"{where}:{line_idx + 1}: § Types duplicate type {key!r} "
                        f"(first defined line {seen[key]})")
                else:
                    seen[key] = line_idx + 1

        if section == "Numbering" and usable:
            seen_m: dict[str, int] = {}
            m_i = tables[0].header.index("marker")
            for line_idx, cells in tables[0].data:
                if len(cells) != len(tables[0].header):
                    continue
                marker = _strip_token(cells[m_i])
                if marker in seen_m:
                    report.refusals.append(
                        f"{where}:{line_idx + 1}: § Numbering duplicate marker "
                        f"{marker!r} (first defined line {seen_m[marker]})")
                else:
                    seen_m[marker] = line_idx + 1

    # Required tokens: each defined exactly once across § Folders' two tables.
    if "Folders" in section_spans:
        for tok in REQUIRED_TOKENS:
            defs = token_definitions.get(tok, [])
            if not defs:
                report.refusals.append(
                    f"{where}: required token `<{tok}>` is not defined in "
                    f"§ Folders")
            elif len(defs) > 1:
                report.refusals.append(
                    f"{where}: required token `<{tok}>` defined "
                    f"{len(defs)} times (lines {', '.join(map(str, defs))}) — "
                    f"must be exactly once")

    if report.changed and write and report.ok:
        out = "\n".join(lines) + ("\n" if had_trailing_newline else "")
        config_path.write_text(out, encoding="utf-8")

    return report


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate (and safely repair) CONFIG.md's load-bearing tables."
    )
    parser.add_argument(
        "config", nargs="?", type=Path, default=None,
        help="Path to CONFIG.md. Defaults to NOTE_KIT_CONFIG, else the kit's own.",
    )
    parser.add_argument(
        "--check-only", action="store_true",
        help="Report repairs without writing them back.",
    )
    args = parser.parse_args()
    path = (args.config or default_config_path()).resolve()

    report = run(path, write=not args.check_only)

    for line in report.repairs:
        print(f"REPAIR  {line}")
    for line in report.refusals:
        print(f"REFUSE  {line}", file=sys.stderr)

    if not report.ok:
        print(f"config_shape: REFUSED — {len(report.refusals)} unsafe "
              f"drift(s); nothing written.", file=sys.stderr)
        return 2
    if report.repairs:
        verb = "repaired in place" if not args.check_only else "repairable (--check-only, not written)"
        print(f"config_shape: OK — {len(report.repairs)} safe repair(s) {verb}.")
    else:
        print("config_shape: OK — shape clean, nothing to do.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
