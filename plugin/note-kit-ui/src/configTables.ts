/**
 * Schema-driven CONFIG.md table parse + serialize, for the CONFIG-editor pane
 * (configView.ts). The kit's CONFIG.md is a sequence of `## Heading` sections,
 * many of which OWN a markdown table; `sync_config` and `config_variables` read
 * those tables by heading + the table's own header row, never by a baked-in
 * column set. This module reads them the SAME way: it walks the file, finds each
 * `##` section, and parses every pipe-table inside it into {header, rows} purely
 * from the markdown shape — no column vocabulary, no row vocabulary, is ever
 * hard-coded here. A section gains a column or a renamed heading and the editor
 * reflects it because the shape was parsed, not assumed (the clown-vault rule
 * applied to the editor itself).
 *
 * The serialize side is byte-conservative: everything OUTSIDE the edited table
 * bodies — prose, headings, blank lines, the column alignment of the separator
 * row, even cells the user did not touch — is preserved verbatim. Only the cells
 * a save actually changed are re-rendered, in place, so an unedited round-trip
 * (parse → serialize with no edits) is a no-op.
 *
 * Mirrors the Python config_shape.py table mechanics (cell splitting, separator
 * detection) so the two agree on what a table IS; the skeleton VALIDATION lives
 * in validateShape() below, a minimal TypeScript echo of that script's refusal
 * rules (the plugin can't invoke the Python headlessly).
 */

/** One parsed markdown table inside a section, carrying enough of its ORIGINAL
 * layout to re-emit it touching only changed cells. */
export interface ConfigTable {
	/** 0-based line index (into the file's line array) of the header row. */
	headerLine: number;
	/** 0-based line index of the separator row (`| --- | --- |`), or -1 when the
	 * table has none (malformed — surfaced read-only, never auto-edited). */
	separatorLine: number;
	/** Column names, in order, from the header row (stripped). */
	header: string[];
	/** Body rows: each a list of cells (stripped), one per header column. */
	rows: ConfigRow[];
}

export interface ConfigRow {
	/** 0-based line index of this row in the original file. */
	line: number;
	/** Cell text, stripped, one per header column. A row whose RAW cell count
	 * disagreed with the header is still captured (cells padded/truncated for
	 * display) but flagged `malformed` so the view renders it read-only. */
	cells: string[];
	/** True when the raw row's cell count did not match the header — a lost or
	 * added mid-row pipe the editor must not try to rewrite (config_shape.py
	 * refuses this; here we just refuse to make it editable). */
	malformed: boolean;
}

/** One `## Heading` section and the tables it owns. A section with no table is
 * still listed (so an odd/prose section is visible) with an empty `tables`. */
export interface ConfigSection {
	/** The heading text, without the leading `## `. */
	heading: string;
	/** 0-based line index of the `## Heading` line. */
	headingLine: number;
	/** Tables found between this heading and the next `##` (or EOF). */
	tables: ConfigTable[];
}

/** The whole parsed file: its lines (the source of truth for serialize) plus
 * the section/table structure layered over them. */
export interface ParsedConfig {
	/** The file split into lines (no trailing newline element). Serialize edits
	 * THESE in place and rejoins them, so untouched lines round-trip exactly. */
	lines: string[];
	/** True when the original text ended with a newline — restored on serialize
	 * so the file's trailing newline is preserved. */
	trailingNewline: boolean;
	sections: ConfigSection[];
}

// ── table mechanics (kept in step with config_shape.py) ──────────────────────

/** A separator cell: optional leading/trailing colon around one-or-more dashes. */
const SEP_CELL_RE = /^:?-+:?$/;

/** A line that reads as part of a pipe table: starts or ends with a pipe, or
 * carries at least two pipes (clearly tabular). Mirrors _is_table_line. Shared
 * with kitConfig's CONFIG-facts parse. */
export function isTableLine(line: string): boolean {
	const s = line.trim();
	if (!s.includes("|")) return false;
	return s.startsWith("|") || s.endsWith("|") || (s.match(/\|/g)?.length ?? 0) >= 2;
}

/** Split a pipe row into stripped cells, outer pipes removed. Mirrors
 * _split_cells — the canonical cell reading every kit parser shares. */
export function splitCells(line: string): string[] {
	let inner = line.trim();
	if (inner.startsWith("|")) inner = inner.slice(1);
	if (inner.endsWith("|")) inner = inner.slice(0, -1);
	return inner.split("|").map((c) => c.trim());
}

/** A row that is ALL separator cells (`---`, `:--:`, …). Mirrors
 * _is_separator_row. Shared with kitConfig's CONFIG-facts parse. */
export function isSeparatorRow(line: string): boolean {
	const cells = splitCells(line);
	return cells.length > 0 && cells.every((c) => SEP_CELL_RE.test(c || ""));
}

// ── parse ────────────────────────────────────────────────────────────────────

/**
 * Parse CONFIG.md text into sections and their tables, purely from the markdown
 * shape. Never assumes a column or row vocabulary — an unknown section with a
 * table parses like any other; a section with no table lists with empty tables
 * (the view shows it read-only rather than dropping it).
 */
export function parseConfig(text: string): ParsedConfig {
	const trailingNewline = text.endsWith("\n");
	// Split on \n only; a \r (Windows CONFIG) is left ON the line so serialize
	// rejoining with \n preserves the file's own line endings per-line — we never
	// rewrite a line we did not edit, and an edited line is re-emitted clean.
	const lines = text.split("\n");
	if (trailingNewline && lines.length && lines[lines.length - 1] === "") lines.pop();

	const sections: ConfigSection[] = [];
	let current: ConfigSection | null = null;

	for (let i = 0; i < lines.length; i++) {
		const line = lines[i];
		// An H2 heading opens a new section. (## only — H1 is the doc title, H3+
		// never head a load-bearing table in this file.)
		const m = /^##\s+(.+?)\s*$/.exec(line);
		if (m && !line.startsWith("###")) {
			current = { heading: m[1].trim(), headingLine: i, tables: [] };
			sections.push(current);
			continue;
		}
		if (!current) continue; // prose before the first ## — not editable, skipped
		// A table starts at a pipe line whose NEXT non-empty line is a separator;
		// detect the run and hand it to parseTableAt.
		if (isTableLine(line)) {
			const tbl = parseTableAt(lines, i);
			if (tbl) {
				current.tables.push(tbl);
				// Skip past the table's consumed lines.
				i = tableEndLine(lines, tbl.headerLine) - 1;
			}
		}
	}

	return { lines, trailingNewline, sections };
}

/** The exclusive end line of the table run that began at `headerLine` — the
 * first blank or non-table line after it. */
function tableEndLine(lines: string[], headerLine: number): number {
	let j = headerLine;
	while (j < lines.length && lines[j].trim() && isTableLine(lines[j])) j++;
	return j;
}

/**
 * Parse a single table whose header row is at `start`. Returns null when the run
 * is a single pipe line with no separator (not a real table — e.g. a stray
 * pipe-bearing prose line), so prose is never mistaken for a grid.
 */
function parseTableAt(lines: string[], start: number): ConfigTable | null {
	const end = tableEndLine(lines, start);
	if (end - start < 1) return null;
	const header = splitCells(lines[start]);

	// The separator is the row immediately after the header, when it reads as one.
	let separatorLine = -1;
	let firstBody = start + 1;
	if (start + 1 < end && isSeparatorRow(lines[start + 1])) {
		separatorLine = start + 1;
		firstBody = start + 2;
	}
	// A pipe line with no following separator row is not a table the editor owns.
	if (separatorLine === -1) return null;

	const rows: ConfigRow[] = [];
	for (let k = firstBody; k < end; k++) {
		const raw = splitCells(lines[k]);
		const malformed = raw.length !== header.length;
		// Normalize the displayed cell count to the header so the grid is
		// rectangular; the malformed flag is what gates editability, not the pad.
		const cells = header.map((_, idx) => raw[idx] ?? "");
		rows.push({ line: k, cells, malformed });
	}

	return { headerLine: start, separatorLine, header, rows };
}

// ── serialize ────────────────────────────────────────────────────────────────

/**
 * An edit to apply on save: one cell, addressed by its absolute body-row line
 * and column index, with its new value. The view collects these as the user
 * types; serialize rewrites ONLY the addressed rows, leaving every other line
 * (prose, headings, the separator, untouched rows) byte-identical.
 */
export interface CellEdit {
	/** 0-based line index of the body row (ConfigRow.line). */
	line: number;
	/** 0-based column index within the header. */
	col: number;
	/** The new cell text (will be pipe-escaped on emit). */
	value: string;
}

/** Escape a cell value so it survives as one table cell: a literal pipe becomes
 * `\|` (the only character that would split a cell), newlines collapse to a
 * space (a table cell is one line). Leaves everything else — backticks, angle
 * brackets, markdown — verbatim. */
function escapeCell(value: string): string {
	return value.replace(/\r?\n/g, " ").replace(/\|/g, "\\|");
}

/**
 * Re-render ONE body row from its (possibly edited) cells, reusing the original
 * line's pipe layout where it can so an untouched cell keeps its exact spacing.
 * The strategy: take the original row's segments (split on unescaped pipes),
 * and for each column substitute the edited value, preserving the original
 * cell's surrounding whitespace so column alignment is undisturbed.
 */
function renderRow(originalLine: string, header: string[], newCells: string[]): string {
	// Work on the inner content (between the outer pipes), so we can re-pad each
	// cell to its original width and keep the table's column alignment.
	const hasLead = originalLine.trimStart().startsWith("|");
	const hasTrail = originalLine.trimEnd().endsWith("|");
	// Leading indentation (rare in this file, but preserved if present).
	const indent = originalLine.slice(0, originalLine.length - originalLine.trimStart().length);

	let inner = originalLine.trim();
	if (inner.startsWith("|")) inner = inner.slice(1);
	if (inner.endsWith("|")) inner = inner.slice(0, -1);
	const segments = inner.split("|");

	// Map each header column onto its original segment (1:1 — malformed rows are
	// never sent here). For each, keep the original leading/trailing spaces and
	// drop the old content in the middle, so `| project   |` stays `| <new>     |`
	// with the column width intact where the new value is no longer than the old.
	const rebuilt = header.map((_, idx) => {
		const seg = segments[idx] ?? " ";
		const lead = seg.slice(0, seg.length - seg.trimStart().length) || " ";
		const trail = seg.slice(seg.trimEnd().length) || " ";
		const content = escapeCell(newCells[idx] ?? "");
		// Preserve original column WIDTH when the new content fits, so alignment is
		// undisturbed; a longer value simply grows its own cell (the rest of the
		// row shifts, exactly as a hand-edit would).
		const original = seg;
		const padded = lead + content + trail;
		if (padded.length < original.length) {
			return padded + " ".repeat(original.length - padded.length);
		}
		return padded;
	});

	const body = rebuilt.join("|");
	return indent + (hasLead ? "|" : "") + body + (hasTrail ? "|" : "");
}

/**
 * Apply cell edits to a parsed config and return the new file text. Only the
 * addressed body rows are rewritten; everything else round-trips verbatim. With
 * an empty edit list the output equals the input byte-for-byte (the round-trip
 * no-op the orchestrator can diff-verify).
 */
export function serializeConfig(parsed: ParsedConfig, edits: CellEdit[]): string {
	const lines = parsed.lines.slice();
	if (edits.length) {
		// Group edits by row so each touched row is re-rendered once with all its
		// new cells (a row with two edited cells is emitted correctly).
		const byRow = new Map<number, Map<number, string>>();
		for (const e of edits) {
			let row = byRow.get(e.line);
			if (!row) {
				row = new Map();
				byRow.set(e.line, row);
			}
			row.set(e.col, e.value);
		}
		// Locate each touched row's table to get its header (column count).
		for (const [lineIdx, colMap] of byRow) {
			const tbl = tableForLine(parsed, lineIdx);
			if (!tbl) continue; // edit addressed no known table — ignore defensively
			const rowObj = tbl.rows.find((r) => r.line === lineIdx);
			if (!rowObj || rowObj.malformed) continue; // never rewrite a malformed row
			const newCells = rowObj.cells.slice();
			for (const [col, val] of colMap) {
				if (col >= 0 && col < newCells.length) newCells[col] = val;
			}
			lines[lineIdx] = renderRow(parsed.lines[lineIdx], tbl.header, newCells);
		}
	}
	return lines.join("\n") + (parsed.trailingNewline ? "\n" : "");
}

/** Find the table that owns a given body-row line index. */
function tableForLine(parsed: ParsedConfig, line: number): ConfigTable | null {
	for (const sec of parsed.sections) {
		for (const tbl of sec.tables) {
			if (tbl.rows.some((r) => r.line === line)) return tbl;
		}
	}
	return null;
}

// ── skeleton validation (minimal echo of config_shape.py refusals) ───────────

/**
 * The load-bearing sections every downstream parser depends on, with the
 * columns config_shape.py requires present. Used ONLY to refuse a save that
 * would damage the skeleton — NOT to drive parsing or rendering (those stay
 * fully shape-derived). Kept deliberately small and in step with
 * config_shape.py's _SECTION_COLUMNS; a heading or column added to CONFIG that
 * is not load-bearing is never required here.
 */
const REQUIRED_SECTION_COLUMNS: Record<string, string[]> = {
	Folders: ["wildcard", "literal"],
	Types: ["type"],
	Numbering: ["marker"],
	Rules: ["rule", "reminder"],
};

export interface ShapeVerdict {
	ok: boolean;
	/** Precise, inline-displayable refusal messages (empty when ok). */
	refusals: string[];
}

/**
 * Validate that a parse of the EDITED text still carries the load-bearing
 * skeleton: every required section present with its required columns, no body
 * row whose cell count disagrees with its header, no unclosed backtick in an
 * edited table. This is the conservative gate the brief mandates — a save that
 * would change the table SKELETON is refused with a precise message rather than
 * silently written. It mirrors the SPIRIT of config_shape.py's refusals; the
 * full Python corrector still runs as the real gate at the head of sync_config.
 *
 * `editedText` is the would-be file content (post-serialize). Re-parsing it (vs.
 * trusting the in-memory model) catches a serialize that itself broke a row.
 */
export function validateShape(editedText: string): ShapeVerdict {
	const refusals: string[] = [];
	const parsed = parseConfig(editedText);
	const byHeading = new Map<string, ConfigSection>();
	for (const sec of parsed.sections) {
		// First definition wins (a duplicate heading is itself suspicious, but the
		// first carries the real table — config_variables reads the first match).
		if (!byHeading.has(sec.heading)) byHeading.set(sec.heading, sec);
	}

	for (const [heading, cols] of Object.entries(REQUIRED_SECTION_COLUMNS)) {
		const sec = byHeading.get(heading);
		if (!sec) {
			refusals.push(`§ ${heading}: section is missing — a load-bearing section may not be dropped.`);
			continue;
		}
		const tbl = sec.tables[0];
		if (!tbl) {
			refusals.push(`§ ${heading}: its table is missing — the section must keep a parseable table.`);
			continue;
		}
		const missing = cols.filter((c) => !tbl.header.includes(c));
		if (missing.length) {
			refusals.push(
				`§ ${heading}: header is missing required column(s) ${missing
					.map((c) => `"${c}"`)
					.join(", ")} — changing the table skeleton is refused.`
			);
		}
	}

	// Any malformed body row (cell count ≠ header) anywhere is a skeleton break a
	// save must not commit — config_shape.py refuses these as unrecoverable.
	for (const sec of parsed.sections) {
		for (const tbl of sec.tables) {
			for (const row of tbl.rows) {
				if (row.malformed) {
					refusals.push(
						`§ ${sec.heading} (line ${row.line + 1}): a row's cell count no longer matches its header — a lost or added pipe cannot be saved safely.`
					);
				}
				// Unclosed backtick in any cell — config_shape.py refuses this too.
				for (let c = 0; c < row.cells.length; c++) {
					if ((row.cells[c].match(/`/g)?.length ?? 0) % 2 === 1) {
						refusals.push(
							`§ ${sec.heading} (line ${row.line + 1}): column "${tbl.header[c] ?? c}" has an unclosed backtick.`
						);
					}
				}
			}
		}
	}

	return { ok: refusals.length === 0, refusals };
}
