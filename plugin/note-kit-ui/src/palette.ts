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
	const probe = document.body.createEl("span");
	probe.style.display = "none";
	const resolve = (cssVar: string): Rgb | null => {
		probe.style.color = `var(${cssVar})`;
		const m = getComputedStyle(probe).color.match(/rgba?\(\s*([\d.]+)[,\s]+([\d.]+)[,\s]+([\d.]+)/);
		if (!m) return null;
		return [Number(m[1]), Number(m[2]), Number(m[3])];
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
