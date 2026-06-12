import { App } from "obsidian";

/**
 * Kit facts derived from the vault's own `.claude/CONFIG.md` — the kit's single
 * source of truth. The plugin reads these at load instead of keeping its own
 * copies, so a CONFIG change (a renamed inbox, a moved queue) never drifts from
 * what the plugin displays. One-way: the plugin only ever reads CONFIG.
 */
export interface KitFacts {
	/** literal folder names, e.g. "00-Inbox" */
	inboxLiteral: string;
	outboxLiteral: string;
	/** the archive root from § Folders, e.g. "99-Archive" — "" when CONFIG
	 * carries no `<archive>` row (consumers fall back to their own heuristic) */
	archiveLiteral: string;
	/** resolved queue file paths, e.g. "00-Inbox/00-User-Queue.md" */
	userQueuePath: string;
	machineQueuePath: string;
	/** numeric structural prefixes from § Numbering, e.g. ["00","01","02","99"] */
	prefixes: string[];
	/** the type vocabulary from § Types */
	types: string[];
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

	// Wildcard → literal rows: | `<inbox>` | `00-Inbox` | …  (also captures the
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

	// § Numbering markers: | `00-` | top | …
	const prefixes: string[] = [];
	for (const m of text.matchAll(/^\|\s*`(\d{2,})-`\s*\|/gm)) {
		if (!prefixes.includes(m[1])) prefixes.push(m[1]);
	}

	// § Types rows: | project | Title-Case-Hyphens | …
	const types: string[] = [];
	const typesSec = text.split(/^## Types\b/m)[1]?.split(/\n## /m)[0] ?? "";
	for (const m of typesSec.matchAll(/^\|\s*([a-z][\w-]*)\s*\|/gm)) {
		if (m[1] !== "type" && !types.includes(m[1])) types.push(m[1]);
	}

	return {
		inboxLiteral: inbox,
		outboxLiteral: outbox,
		archiveLiteral: tokens.get("archive") ?? "",
		userQueuePath: resolve(uq),
		machineQueuePath: resolve(mq),
		prefixes,
		types,
	};
}
