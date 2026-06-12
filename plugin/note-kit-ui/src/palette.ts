import { TypeStyle } from "./settings";

/**
 * Theme-derived type palette. Reads the active theme's eight extended colors
 * (--color-red … --color-pink) at runtime and maps the kit's type vocabulary
 * onto them, so the UI's type colors always complement whatever theme is on —
 * no hardcoded hexes to clash. Re-derived on every css-change (theme switch,
 * snippet edit); the manual type list applies when the toggle is off.
 */

type Hue = "red" | "orange" | "yellow" | "green" | "cyan" | "blue" | "purple" | "pink";
const HUES: Hue[] = ["red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink"];

type Rgb = [number, number, number];

interface Role {
	hue: Hue;
	/** Principle/support types take a softened variant of their family hue. */
	soft?: boolean;
	/** Operational types stay neutral — they are chrome, not content. */
	neutral?: "muted" | "faint";
}

/** Semantic hue roles for the kit vocabulary. Primary content types get a full
 * hue; their satellite types share the family hue, softened; operational types
 * are gray. A type outside this map (custom vocabulary) gets a full-strength
 * hue by name hash, stable across loads. */
const ROLES: Record<string, Role> = {
	project: { hue: "red" },
	area: { hue: "orange" },
	reference: { hue: "green" },
	research: { hue: "blue" },
	plan: { hue: "purple" },
	journal: { hue: "pink" },
	idea: { hue: "yellow" },
	snippet: { hue: "cyan" },
	index: { hue: "yellow", soft: true },
	voice: { hue: "pink", soft: true },
	design: { hue: "purple", soft: true },
	format: { hue: "cyan", soft: true },
	addendum: { hue: "orange", soft: true },
	revision: { hue: "blue", soft: true },
	session: { hue: "blue", neutral: "muted" },
	source: { hue: "blue", neutral: "muted" },
	note: { hue: "blue", neutral: "muted" },
	log: { hue: "blue", neutral: "faint" },
};

/**
 * Map `types` onto the active theme's palette. Returns null when the theme's
 * color variables can't be resolved (e.g. called before the app's CSS is in) —
 * the caller then keeps whatever palette it had.
 */
export function deriveThemePalette(types: string[]): TypeStyle[] | null {
	const bodyStyle = getComputedStyle(document.body);
	// A probe rendered (not display:none) off-screen — some engines skip resolving
	// custom-property `var()`s in `color` on a display:none element, which silently
	// collapsed every hue to the same fallback and made this return null (the toggle
	// then "did nothing"). Reading the raw value first avoids the probe entirely for
	// the common case (a direct hex/rgb), and the probe only handles var-of-var.
	const probe = document.body.createEl("span");
	probe.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;pointer-events:none;";
	const parse = (raw: string): Rgb | null => {
		raw = raw.trim();
		if (!raw) return null;
		if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw)) {
			const hx = raw.replace("#", "");
			const f = hx.length === 3 ? hx.replace(/./g, "$&$&") : hx;
			const n = parseInt(f, 16);
			return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
		}
		const m = raw.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
		return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
	};
	const resolve = (cssVar: string): Rgb | null => {
		const direct = parse(bodyStyle.getPropertyValue(cssVar));
		if (direct) return direct;
		probe.style.color = `var(${cssVar})`;
		return parse(getComputedStyle(probe).color);
	};
	try {
		const hues = new Map<Hue, Rgb>();
		for (const h of HUES) {
			const c = resolve(`--color-${h}`);
			if (!c) return null;
			hues.set(h, c);
		}
		const bg = resolve("--background-primary");
		const muted = resolve("--text-muted");
		const faint = resolve("--text-faint");
		if (!bg || !muted || !faint) return null;

		// All eight hues identical means the vars aren't really set — bail.
		const first = JSON.stringify(hues.get(HUES[0]));
		if (HUES.every((h) => JSON.stringify(hues.get(h)) === first)) return null;

		return types.map((type) => {
			const role = ROLES[type] ?? { hue: HUES[hashName(type) % HUES.length] };
			let rgb: Rgb;
			if (role.neutral) rgb = role.neutral === "muted" ? muted : faint;
			else {
				rgb = hues.get(role.hue) as Rgb;
				// Soft variant: the family hue eased toward the background, so the
				// satellite type reads as kin to its primary but a register quieter.
				if (role.soft) rgb = mix(rgb, bg, 0.4);
			}
			return { type, color: hex(rgb) };
		});
	} finally {
		probe.remove();
	}
}

function mix(a: Rgb, b: Rgb, t: number): Rgb {
	return [0, 1, 2].map((i) => Math.round(a[i] * (1 - t) + b[i] * t)) as Rgb;
}

function hex(rgb: Rgb): string {
	return "#" + rgb.map((v) => Math.max(0, Math.min(255, v)).toString(16).padStart(2, "0")).join("");
}

function hashName(s: string): number {
	let h = 0;
	for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) >>> 0;
	return h;
}

// ── Sub-tone derivation ──────────────────────────────────────────────────────
// Every tinted variant of a type colour (frontmatter, headings, file names, gate
// text, tab, …) is a hue-preserving LIGHTEN of the canon colour — never a mix
// toward the near-white text colour, which shifts a saturated red toward pink and
// desaturates it. In HSL we hold hue and saturation and only raise lightness, so
// red stays the SAME red, just brighter. Pure functions of the hex: deterministic,
// scriptable, constant per input — so a project's tints can never "lean pink".

function hexToRgb(hex: string): Rgb | null {
	const m = /^#?([0-9a-f]{6})$/i.exec(hex.trim());
	if (!m) return null;
	const n = parseInt(m[1], 16);
	return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

function rgbToHsl([r, g, b]: Rgb): [number, number, number] {
	r /= 255;
	g /= 255;
	b /= 255;
	const max = Math.max(r, g, b);
	const min = Math.min(r, g, b);
	const d = max - min;
	const l = (max + min) / 2;
	let h = 0;
	let s = 0;
	if (d !== 0) {
		s = d / (1 - Math.abs(2 * l - 1));
		switch (max) {
			case r:
				h = ((g - b) / d) % 6;
				break;
			case g:
				h = (b - r) / d + 2;
				break;
			default:
				h = (r - g) / d + 4;
		}
		h *= 60;
		if (h < 0) h += 360;
	}
	return [h, s, l];
}

function hslToRgb([h, s, l]: [number, number, number]): Rgb {
	const c = (1 - Math.abs(2 * l - 1)) * s;
	const x = c * (1 - Math.abs(((h / 60) % 2) - 1));
	const m = l - c / 2;
	let r = 0;
	let g = 0;
	let b = 0;
	if (h < 60) [r, g, b] = [c, x, 0];
	else if (h < 120) [r, g, b] = [x, c, 0];
	else if (h < 180) [r, g, b] = [0, c, x];
	else if (h < 240) [r, g, b] = [0, x, c];
	else if (h < 300) [r, g, b] = [x, 0, c];
	else [r, g, b] = [c, 0, x];
	return [Math.round((r + m) * 255), Math.round((g + m) * 255), Math.round((b + m) * 255)];
}

/** A deterministic colour for a type CONFIG names but no palette covers:
 * golden-angle hue spacing keeps any number of generated colours visually
 * distinct, in the same saturation/lightness band as the curated defaults. */
export function autoColor(index: number): string {
	return hex(hslToRgb([(index * 137.508) % 360, 0.6, 0.55]));
}

/** A hue-preserving tone of `hex`: lightness shifted by `dL` (capped so it never
 * washes to white), saturation scaled by `sMul`. Hue is untouched. */
export function tone(hexStr: string, dL: number, sMul = 1): string {
	const rgb = hexToRgb(hexStr);
	if (!rgb) return hexStr;
	const [h, s, l] = rgbToHsl(rgb);
	const s2 = Math.max(0, Math.min(1, s * sMul));
	const l2 = Math.max(0, Math.min(0.82, l + dL));
	return hex(hslToRgb([h, s2, l2]));
}

/** The standard derived tone set for one canon colour, as CSS custom-property
 * declarations to set on whatever element carries that colour. Consumers read
 * `--nkui-ts` (strong), `--nkui-tm` (mid), `--nkui-tk` (ink) for every tinted use,
 * plus `--nkui-tb` (emphasis ink: bold/italic), so the whole tint family follows
 * the canon hue. `--nkui-tb` sits near canon strength with a saturation boost —
 * emphasis must hold its own against the tinted/washed note background, and the
 * boost raises chroma at the SAME hue (project red reads as a stronger red,
 * never pink — pink is what desaturating toward white produces). */
export function toneVars(hexStr: string): Record<string, string> {
	// Emphasis (`--nkui-tb`) is contrast-aware but hue-true: on a dark theme it
	// pushes LIGHTER, on a light theme DARKER — widening the lightness spectrum
	// away from the washed background — with the same-hue saturation boost. The
	// theme is read off body (theme-dark/theme-light), the one global the
	// palette legitimately depends on.
	const dark = typeof document !== "undefined" && document.body?.classList?.contains("theme-dark");
	return {
		"--nkui-ts": tone(hexStr, 0.06),
		"--nkui-tm": tone(hexStr, 0.13),
		"--nkui-tk": tone(hexStr, 0.09, 0.92),
		"--nkui-tb": tone(hexStr, dark ? 0.18 : -0.18, 1.3),
	};
}
