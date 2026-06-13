/**
 * User-replaceable icons — the data and helpers behind the "Icons" settings
 * group and the css.ts mask overrides. The solid-icon system (styles.css,
 * body.nkui-solid-icons) masks each control's Lucide <svg> to a Material Symbols
 * Sharp glyph in currentColor; an override here swaps that mask for the user's
 * own SVG, injected after styles.css so it wins by source order.
 *
 * The control set maps a friendly NAME → the Lucide class the glyph rides on,
 * paired with the shipped default mask SVG (the same Material Symbols paths the
 * stylesheet carries) so the settings preview can render the current glyph and
 * the per-row reset is just "drop the override".
 */
export interface IconControl {
	/** stable key stored in settings.iconOverrides */
	key: string;
	/** friendly label shown in settings */
	label: string;
	/** the Lucide class(es) css.ts targets (the first is canonical) */
	lucideClasses: string[];
	/** the shipped default glyph SVG (for the live preview + reset) */
	defaultSvg: string;
}

/** A bare 24-grid svg wrapper for a Material-Symbols path (viewBox 0 -960 960). */
const ms = (d: string): string =>
	`<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 -960 960 960"><path d="${d}"/></svg>`;

/**
 * The user-facing control set. Mirrors the meaningful glyphs the solid-icon
 * block already masks (styles.css ~2519+); each defaultSvg is the same Material
 * Symbols Sharp path that block uses, so a preview with no override shows
 * exactly the shipped look.
 */
export const ICON_CONTROLS: IconControl[] = [
	{
		key: "search",
		label: "Search",
		lucideClasses: ["lucide-search"],
		defaultSvg: ms(
			"M784-120 532-372q-30 24-69 38t-83 14q-109 0-184.5-75.5T120-580q0-109 75.5-184.5T380-840q109 0 184.5 75.5T640-580q0 44-14 83t-38 69l252 252-56 56ZM380-400q75 0 127.5-52.5T560-580q0-75-52.5-127.5T380-760q-75 0-127.5 52.5T200-580q0 75 52.5 127.5T380-400Z"
		),
	},
	{
		key: "bookmark",
		label: "Bookmark",
		lucideClasses: ["lucide-bookmark"],
		defaultSvg: ms("M200-120v-720h560v720L480-240 200-120Z"),
	},
	{
		key: "edit",
		label: "Edit / New note",
		lucideClasses: ["lucide-pencil", "lucide-square-pen"],
		defaultSvg: ms("M120-120v-170l585-583 167 171-582 582H120Zm584-528 56-56-56-56-56 56 56 56Z"),
	},
	{
		key: "folder-plus",
		label: "New folder",
		lucideClasses: ["lucide-folder-plus"],
		defaultSvg: ms(
			"M80-160v-640h320l80 80h400v560H80Zm480-160h80v-80h80v-80h-80v-80h-80v80h-80v80h80v80Z"
		),
	},
	{
		key: "sort",
		label: "Sort",
		lucideClasses: ["lucide-arrow-up-narrow-wide"],
		defaultSvg: ms("M120-240v-80h240v80H120Zm0-200v-80h480v80H120Zm0-200v-80h720v80H120Z"),
	},
	{
		key: "collapse",
		label: "Collapse all",
		lucideClasses: ["lucide-chevrons-down-up"],
		defaultSvg: ms(
			"m356-160-56-56 180-180 180 180-56 56-124-124-124 124Zm124-404L300-744l56-56 124 124 124-124 56 56-180 180Z"
		),
	},
	{
		key: "close",
		label: "Close",
		lucideClasses: ["lucide-x"],
		defaultSvg: ms(
			"m256-200-56-56 224-224-224-224 56-56 224 224 224-224 56 56-224 224 224 224-56 56-224-224-224 224Z"
		),
	},
	{
		key: "settings",
		label: "Settings",
		lucideClasses: ["lucide-settings"],
		defaultSvg: ms(
			"m370-80-16-128q-13-5-24.5-12T307-235l-119 50L78-375l103-78q-1-7-1-13.5v-27q0-6.5 1-13.5L78-585l110-190 119 50q11-8 23-15t24-12l16-128h220l16 128q13 5 24.5 12t22.5 15l119-50 110 190-103 78q1 7 1 13.5v27q0 6.5-2 13.5l103 78-110 190-118-50q-11 8-23 15t-24 12L590-80H370Zm112-260q58 0 99-41t41-99q0-58-41-99t-99-41q-59 0-99.5 41T342-480q0 58 40.5 99t99.5 41Z"
		),
	},
	{
		key: "for-you",
		label: "For You (sun)",
		lucideClasses: ["lucide-sun"],
		defaultSvg: ms("m320-80 40-280H160l360-520h80l-40 320h240L400-80h-80Z"),
	},
];

/** Percent-encode an SVG string for safe use in a CSS `url("data:image/svg+xml,…")`.
 * Encodes the characters that break the data-URI or the surrounding CSS string —
 * `\`, `<`, `>`, `#`, `"`, `%`, `{`, `}` — and collapses whitespace runs to single
 * spaces so multi-line pastes stay compact. Anything else is left readable. */
export function encodeSvgDataUri(svg: string): string {
	const compact = svg.replace(/\s+/g, " ").trim();
	const encoded = compact
		.replace(/%/g, "%25")
		.replace(/\\/g, "%5C")
		.replace(/</g, "%3C")
		.replace(/>/g, "%3E")
		.replace(/#/g, "%23")
		.replace(/"/g, "%22")
		.replace(/\{/g, "%7B")
		.replace(/\}/g, "%7D");
	return `data:image/svg+xml,${encoded}`;
}

/**
 * Normalize a user override into a full <svg>…</svg> string, or null when the
 * input is blank/unusable (the caller then falls through to the shipped
 * default). Accepts either:
 *  · a full `<svg …>…</svg>` paste (used as-is), or
 *  · a bare path-`d` string (wrapped in a 24-grid viewBox svg).
 * Minimal validation only — a malformed paste simply renders nothing, which is
 * the user's own doing and recoverable by clearing the row.
 */
export function normalizeOverrideSvg(raw: string): string | null {
	const v = (raw ?? "").trim();
	if (!v) return null;
	if (/<svg[\s>]/i.test(v)) return v; // a full svg paste
	// A bare path-d (starts with a path command letter). Wrap it in a standard
	// 24-grid svg so it masks at the same scale as the shipped glyphs.
	if (/^[mMlLhHvVcCsSqQtTaAzZ0-9.\-,\s]+$/.test(v)) {
		return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><path d="${v}"/></svg>`;
	}
	// Anything else (e.g. a bare <path …/>) — wrap it as svg children.
	return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24">${v}</svg>`;
}
