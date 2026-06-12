import { NoteKitUiSettings, TypeStyle, typeClass } from "./settings";
import { toneVars } from "./palette";

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
 * accents) lives in styles.css; this only emits the per-prefix / per-type values
 * the user can edit, so the static rules can reference them.
 */
export function buildDynamicCss(s: NoteKitUiSettings, typeStyles: TypeStyle[]): string {
	const out: string[] = [];

	if (s.enablePrefixStyling) {
		for (const p of s.prefixStyles) {
			const sel = `.nav-folder-title[data-nkui-prefix="${attr(p.prefix)}"], .nav-file-title[data-nkui-prefix="${attr(p.prefix)}"]`;
			const size = p.size ?? 1;
			const decls: string[] = [];
			// !important so explorer :hover/active rules can't override the weight,
			// which otherwise un-bolds prefixed rows on hover (a visible "wiggle").
			if (Number.isFinite(p.weight)) decls.push(`font-weight: ${p.weight} !important;`);
			if (Number.isFinite(size) && size !== 1) decls.push(`font-size: ${size}em !important;`);
			if (Number.isFinite(p.opacity) && p.opacity < 1) decls.push(`opacity: ${p.opacity};`);
			if (p.color) decls.push(`color: ${p.color};`);
			if (decls.length) out.push(`${sel} { ${decls.join(" ")} }`);
		}
	}

	if (s.dimSinkContents) {
		// Fade + shrink everything *inside* a sink folder (a faded prefix like 99-),
		// so an expanded sink doesn't read as live files. Opacity rides the children
		// container so the whole subtree fades as one group (no per-row compounding);
		// font-size is inherited once down the tree rather than re-applied per depth.
		for (const p of s.prefixStyles) {
			if (!(Number.isFinite(p.opacity) && p.opacity < 1)) continue;
			const size = p.size ?? 1;
			const decls: string[] = [];
			if (Number.isFinite(size) && size !== 1) decls.push(`font-size: ${size}em;`);
			decls.push(`opacity: ${p.opacity};`);
			out.push(
				`.nav-folder[data-nkui-sink="${attr(p.prefix)}"] > .nav-folder-children { ${decls.join(" ")} }`
			);
		}
	}

	if (s.hideFolderArrows) {
		// Hide the collapse chevron. Folders still toggle on title click.
		out.push(".nav-folder-title .collapse-icon { display: none; }");
	}

	if (s.minimalChrome) {
		out.push(
			// Drop the file-tree indentation guide lines.
			".nav-folder-children { border-left: none !important; }",
			// File-explorer toolbar icons: faint until hovered.
			".nav-buttons-container .clickable-icon { opacity: 0.35; transition: opacity 120ms ease; }",
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
			".mod-root .workspace-tab-header .workspace-tab-header-inner-close-button { opacity: 0; transition: opacity 120ms ease; }",
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
