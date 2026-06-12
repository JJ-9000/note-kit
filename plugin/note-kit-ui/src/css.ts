import { NoteKitUiSettings, TypeStyle, typeClass } from "./settings";
import { toneVars } from "./palette";
import { armDelayMs } from "./holds";

/** Serialize a type colour + its derived sub-tones as CSS declarations. */
function colorDecls(color: string): string {
	const tones = toneVars(color);
	const tv = Object.entries(tones)
		.map(([k, v]) => `${k}: ${v};`)
		.join(" ");
	return `--nkui-type-color: ${color}; ${tv}`;
}

/** Escape a value for safe use inside a CSS attribute-selector string. */
function attr(v: string): string {
	return v.replace(/\\/g, "\\\\").replace(/"/g, '\\"');
}

/**
 * Build the settings-driven stylesheet. Structural presentation (dots, pills,
 * accents) lives in styles.css; this only emits the per-type values the user
 * can edit, so the static rules can reference them. The sink / uncategorized /
 * weight looks are static rules in styles.css keyed on the decorator's
 * data attributes — nothing to emit here.
 */
export function buildDynamicCss(s: NoteKitUiSettings, typeStyles: TypeStyle[]): string {
	const out: string[] = [];

	// The configured hold duration drives the fill animation (paired with
	// configureHolds in main.ts); the close-offer keeps the shipped 5x ratio.
	// The fill only starts after holds.ts's arm delay (so a tap never flashes
	// it), so --nkui-hold is the REMAINDER — the visible sweep, never under
	// the 100ms armDelayMs guards (one source of truth: the delay constant
	// lives in holds.ts).
	const hold = Number.isFinite(s.holdMs) && s.holdMs > 0 ? s.holdMs : 395;
	const fill = Math.max(100, hold - armDelayMs(hold));
	out.push(`:root { --nkui-hold: ${fill}ms; --nkui-count: ${Math.round(hold * 5)}ms; }`);

	if (s.hideFolderArrows) {
		// Hide the collapse chevron. Folders still toggle on title click.
		out.push(".nav-folder-title .collapse-icon { display: none; }");
	}

	if (s.minimalChrome) {
		out.push(
			// Drop the file-tree indentation guide lines.
			".nav-folder-children { border-left: none !important; }",
			// File-explorer toolbar icons: faint until hovered.
			// Durations reference the motion tokens in styles.css (:root), so a
			// timing change stays one edit even for these emitted rules.
			".nav-buttons-container .clickable-icon { opacity: 0.35; transition: opacity var(--nkui-fade, 120ms) ease; }",
			".nav-buttons-container:hover .clickable-icon { opacity: 0.7; }",
			".nav-buttons-container .clickable-icon:hover { opacity: 1; }",
			// Tab bar quieting is scoped to the MAIN editor area (.mod-root) only.
			// Sidebar tab headers are icon-only — hiding their icon or fading them
			// makes the Files/Tags switcher invisible and strands the user in
			// whichever pane is active (the lost-file-view bug). Sidebar keeps
			// stock affordances.
			".mod-root .workspace-tab-header-container { border-bottom: none !important; box-shadow: none !important; }",
			".mod-root .workspace-tab-header, .mod-root .workspace-tab-header-inner { background: transparent !important; border: none !important; box-shadow: none !important; }",
			// Drop the per-tab icon entirely (main tabs have text titles).
			".mod-root .workspace-tab-header .workspace-tab-header-inner-icon { display: none !important; }",
			// Inactive tabs recede; the active tab is marked by weight + colour only.
			".mod-root .workspace-tab-header:not(.is-active) { opacity: 0.4; }",
			".mod-root .workspace-tab-header:hover { opacity: 1; }",
			".mod-root .workspace-tab-header.is-active .workspace-tab-header-inner-title { font-weight: 700; color: var(--text-normal); }",
			// Close button: hidden until the tab is hovered.
			".mod-root .workspace-tab-header .workspace-tab-header-inner-close-button { opacity: 0; transition: opacity var(--nkui-fade, 120ms) ease; }",
			".mod-root .workspace-tab-header:hover .workspace-tab-header-inner-close-button { opacity: 0.65; }"
		);
	}

	if (s.enableTypeStyling) {
		for (const t of typeStyles) {
			if (!t.color) continue;
			const decls = colorDecls(t.color);
			// explorer row dot colour + derived sub-tones
			out.push(`.nav-file-title[data-nkui-type="${attr(t.type)}"] { ${decls} }`);
			// open-note accent colour + derived sub-tones
			out.push(`.${typeClass(t.type)} { ${decls} }`);
		}
	}

	return out.join("\n");
}
