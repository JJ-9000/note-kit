import { App } from "obsidian";
import { TypeStyle } from "./settings";

/**
 * Mirror the type colors into the graph view's color groups, so graph/bubble
 * nodes match the explorer dots and For You bubbles — one palette everywhere.
 * Writes `.obsidian/graph.json` (each group queries the `type` property) and
 * nudges any open graph view to repaint. Only `colorGroups` is touched; the
 * user's other graph options (filters, forces, toggles) are preserved.
 */
export async function syncGraphColors(app: App, styles: TypeStyle[]): Promise<void> {
	const path = `${app.vault.configDir}/graph.json`;
	let data: Record<string, unknown> = {};
	try {
		const parsed = JSON.parse(await app.vault.adapter.read(path)) as unknown;
		// Guard against a graph.json that parses but is not an object (a corrupted
		// `[]` or bare value): assigning colorGroups onto a non-record would persist
		// a structurally broken file. Keep the fresh {} in that case.
		if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
			data = parsed as Record<string, unknown>;
		}
	} catch {
		// no graph.json yet (graph never opened) — write a fresh one; the graph
		// view merges its own defaults around it.
	}
	const colorGroups = styles
		.filter((t) => /^#[0-9a-fA-F]{6}$/.test(t.color))
		.map((t) => ({
			query: `["type":"${t.type}"]`,
			color: { a: 1, rgb: parseInt(t.color.slice(1), 16) },
		}));
	data.colorGroups = colorGroups;
	await app.vault.adapter.write(path, JSON.stringify(data, null, 2));

	// Repaint open graphs in place. dataEngine is the graph view's options
	// surface (undocumented but long-stable); failure just means the colors
	// appear the next time the graph opens.
	for (const leaf of app.workspace.getLeavesOfType("graph")) {
		const view = leaf.view as unknown as {
			dataEngine?: { setOptions?: (o: unknown) => void };
		};
		try {
			view.dataEngine?.setOptions?.({ colorGroups });
		} catch {
			/* best effort */
		}
	}
}

/**
 * Open (or reveal) the global graph view with its search filter set to a tag, so
 * the graph shows and highlights that tag's notes. Best-effort, on the same
 * undocumented dataEngine surface syncGraphColors uses:
 *  · reveal an existing graph leaf, else open one (core "graph" internal plugin),
 *  · then set the data engine's search query to `tag:#<tag>` so the matching
 *    nodes stay lit and the rest dim — the graph's own search behaviour.
 * Every step is wrapped so a failure (a renamed internal API) just leaves the
 * graph open without the filter rather than throwing into the click handler.
 *
 * @param tag the tag WITHOUT its leading `#` (the caller strips it).
 */
export async function openGraphForTag(app: App, tag: string): Promise<void> {
	const clean = tag.replace(/^#/, "").trim();
	if (!clean) return;
	const query = `tag:#${clean}`;

	// Find an open graph leaf, or open one via the core graph plugin.
	let leaf = app.workspace.getLeavesOfType("graph")[0] ?? null;
	if (!leaf) {
		try {
			const graphPlugin = (
				app as unknown as {
					internalPlugins?: { getPluginById?: (id: string) => { enable?: () => void } | null };
				}
			).internalPlugins?.getPluginById?.("graph");
			graphPlugin?.enable?.();
		} catch {
			/* the global graph may already be enabled; ignore */
		}
		// Open the graph in a new main-area tab.
		leaf = app.workspace.getLeaf("tab");
		try {
			await leaf.setViewState({ type: "graph", active: true });
		} catch {
			return; // graph view unavailable — nothing more to do
		}
	}
	try {
		await app.workspace.revealLeaf(leaf);
	} catch {
		/* best effort */
	}

	// Set the search query on the data engine. setOptions with search is the
	// surface the graph's own search box drives; filterOptions.search is the
	// nested shape some builds expose — set both, ignoring whichever is absent.
	const apply = (): void => {
		const view = leaf?.view as unknown as {
			dataEngine?: {
				setOptions?: (o: unknown) => void;
				searchQueries?: unknown;
			};
		} | undefined;
		const engine = view?.dataEngine;
		if (!engine?.setOptions) return;
		try {
			engine.setOptions({ search: query, filterOptions: { search: query } });
		} catch {
			/* best effort */
		}
	};
	// The view may need a tick to build its data engine after setViewState; apply
	// now and once more after layout settles.
	apply();
	window.setTimeout(apply, 50);
}
