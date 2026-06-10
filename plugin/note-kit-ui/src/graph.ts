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
		data = JSON.parse(await app.vault.adapter.read(path)) as Record<string, unknown>;
	} catch {
		// no graph.json yet (graph never opened) — write a fresh one; the graph
		// view merges its own defaults around it.
	}
	const colorGroups = styles
		.filter((t) => /^#[0-9a-fA-F]{6}$/.test(t.color))
		.map((t) => ({
			query: `["type":${t.type}]`,
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
