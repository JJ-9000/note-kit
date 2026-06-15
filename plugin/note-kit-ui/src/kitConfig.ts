import { App } from "obsidian";
import { isSeparatorRow, isTableLine, splitCells } from "./configTables";

/**
 * Kit facts derived from the vault's own `.claude/CONFIG.md` — the kit's single
 * source of truth. The plugin reads these at load instead of keeping its own
 * copies, so a CONFIG change (a renamed inbox, a moved queue) never drifts from
 * what the plugin displays. One-way: the plugin only ever reads CONFIG.
 */
export interface KitFacts {
	/** literal folder names, e.g. "Inbox" (legacy installs: "00-Inbox") */
	inboxLiteral: string;
	outboxLiteral: string;
	/** the archive root from § Folders, e.g. "Archive" — "" when CONFIG
	 * carries no `<archive>` row (consumers fall back to their own heuristic) */
	archiveLiteral: string;
	/** the session subfolder literal (§ Subfolders `<sessions>`), e.g. "Sessions"
	 * — "" when CONFIG defines no such token; consumers then fall back to the
	 * conventional bare name. Lets the session-unification look track a renamed
	 * sessions folder instead of a hard-coded literal. */
	sessionsLiteral: string;
	/** resolved queue file paths, e.g. "Inbox/User-Queue.md" */
	userQueuePath: string;
	machineQueuePath: string;
	/** the type vocabulary from § Types, in table-row order */
	types: string[];
	/** § Folders literals in table-row order — CONFIG row order IS the explorer
	 * root order under the plugin's semantic ordering */
	rootOrder: string[];
	/** § Subfolders literals in table-row order — subfolder display order */
	subfolderOrder: string[];
}

const CONFIG_PATH = ".claude/CONFIG.md";

/**
 * Parse the CONFIG tables. Returns null when no CONFIG exists (standalone use —
 * the stored manual settings then apply) or when the load-bearing rows are
 * missing (a malformed CONFIG should fall back, not half-apply).
 */
export async function readKitFacts(app: App): Promise<KitFacts | null> {
	let text: string;
	try {
		text = await app.vault.adapter.read(CONFIG_PATH);
	} catch {
		return null; // no kit in this vault — manual settings apply
	}

	// Wildcard → literal rows: | `<inbox>` | `Inbox` | …  (also captures the
	// token table, whose second cell opens with a backticked value).
	const tokens = new Map<string, string>();
	for (const m of text.matchAll(/^\|\s*`<([\w-]+)>`\s*\|\s*`([^`]+)`/gm)) {
		// First definition wins: the § Folders and token tables define each token
		// once; later tables (e.g. § Operational documents) merely mention tokens
		// in their cells and must not overwrite the real value.
		if (!tokens.has(m[1])) tokens.set(m[1], m[2].trim().replace(/\/+$/, ""));
	}
	const inbox = tokens.get("inbox");
	const outbox = tokens.get("outbox");
	const uq = tokens.get("user-queue");
	const mq = tokens.get("machine-queue");
	if (!inbox || !outbox || !uq || !mq) return null;

	const resolve = (v: string) => v.replace(/<([\w-]+)>/g, (_s, k: string) => tokens.get(k) ?? `<${k}>`);

	// The kit's tables are found by their own COLUMN SIGNATURE, never by a baked
	// heading literal — the clown-vault rule: a vault that renames `## Types` to
	// `## Note Types` (or § Folders to § Roots) still derives, because the table's
	// shape is what identifies it, not the words above it. findTable walks the
	// pipe-tables in document order and returns the header + body rows of the FIRST
	// table whose header satisfies the predicate; cells are read by COLUMN NAME, so
	// neither a reordered column nor an added one shifts the value read. Row order
	// (the display order) is preserved exactly as the table is written.
	// Cell splitting and table/separator detection are the canonical primitives
	// from configTables (which mirrors the Python config_shape.py) — shared so the
	// two TS parsers can never drift. findTable stays here (it has no configTables
	// twin), built on those primitives.
	const tableLines = text.split("\n");
	const findTable = (
		headerMatches: (header: string[]) => boolean
	): { header: string[]; rows: string[][] } | null => {
		for (let i = 0; i < tableLines.length - 1; i++) {
			if (!isTableLine(tableLines[i]) || !isSeparatorRow(tableLines[i + 1])) continue;
			const header = splitCells(tableLines[i]).map((c) => c.replace(/`/g, "").trim());
			if (!headerMatches(header)) continue;
			const rows: string[][] = [];
			for (let j = i + 2; j < tableLines.length && tableLines[j].trim() && isTableLine(tableLines[j]); j++) {
				rows.push(splitCells(tableLines[j]));
			}
			return { header, rows };
		}
		return null;
	};
	/** Read one named column of the first table matching the signature, in row
	 * order: the cell's literal with backticks/trailing slash stripped, skipping
	 * blanks and wildcard-only (`<token>`) cells. Column found by NAME (index), so
	 * a reordered or added column never shifts the read. */
	const column = (headerMatches: (h: string[]) => boolean, colName: string): string[] => {
		const tbl = findTable((h) => headerMatches(h) && h.includes(colName));
		if (!tbl) return [];
		const idx = tbl.header.indexOf(colName);
		const out: string[] = [];
		for (const row of tbl.rows) {
			const lit = (row[idx] ?? "").replace(/`/g, "").trim().replace(/\/+$/, "");
			if (lit && !lit.startsWith("<") && !out.includes(lit)) out.push(lit);
		}
		return out;
	};

	// Type vocabulary: the table that HAS a `type` column (the § Types table by
	// shape). Read the `type` column by name — heading-rename-proof AND
	// column-order-proof.
	const types = column((h) => h.includes("type"), "type").filter((t) => /^[a-z][\w-]*$/.test(t));

	// Section literals, by column signature (not heading literal): § Folders is the
	// table with `wildcard` + `literal`; § Subfolders has `type-role` + `subfolder`.
	const rootOrder = column(
		(h) => h.includes("wildcard") && h.includes("literal"),
		"literal"
	).filter((l) => !l.includes("/"));
	const subfolderOrder = column(
		(h) => h.includes("type-role") && h.includes("subfolder"),
		"subfolder"
	);

	return {
		inboxLiteral: inbox,
		outboxLiteral: outbox,
		archiveLiteral: tokens.get("archive") ?? "",
		sessionsLiteral: tokens.get("sessions") ?? "",
		userQueuePath: resolve(uq),
		machineQueuePath: resolve(mq),
		types,
		rootOrder,
		subfolderOrder,
	};
}
