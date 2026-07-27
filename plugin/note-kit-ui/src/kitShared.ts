import { ItemView, Platform, debounce } from "obsidian";
import type { NoteKitUiSettings } from "./settings";

/**
 * Resolve a working set's gate from the NAMES of its root-level members — the one
 * rule both the For You view (nowView) and the explorer decorator apply. It works
 * over name strings so a caller holding Entry objects maps to names and back. A
 * lone root file IS the gate ("a folder with one human-facing file", CONFIG
 * § Group approval); among several, naming decides: a single 00-prefixed
 * cover/manifest, else a single folder-note (basename equal to the container, the
 * plain scheme's cover convention), else a single date-named file (a handoff
 * set's gate is its session log, not a 00- cover). Two candidates of the same
 * rank resolve to none — no guessing. Names carry their extension; only the
 * folder-note rank strips a trailing `.md` before the basename compare.
 *
 * OD2 (decided 2026-07-25): a same-rank tie among the distinguished-cover ranks
 * (two `00-` covers, two date-named files) resolves DETERMINISTICALLY to the
 * lexicographically-first basename — a stable, explainable key, so the gate never
 * flickers between renders and the same file is always the gate. The tie stays
 * VISIBLE through `gateTieCount` (the UI's "N gates" note), which flags the naming
 * to fix. A lone root is still the gate outright; only the sub-ranks tie-break.
 */
export function pickGateName(names: string[], containerName: string): string | null {
	const one = (xs: string[]): string | null => (xs.length === 1 ? xs[0] : null);
	/** The lexicographically-first name by UTF-16 CODE UNIT (`<`), not localeCompare
	 * — a byte-stable order identical on every host, since locale-aware collation
	 * varies by platform ICU (desktop vs mobile). Returns the sole candidate when
	 * there is exactly one. */
	const first = (xs: string[]): string | null =>
		xs.length ? xs.reduce((a, b) => (b < a ? b : a)) : null;
	return (
		one(names) ??
		first(names.filter((n) => /^00[-_ ]/.test(n))) ??
		first(names.filter((n) => n.replace(/\.md$/i, "") === containerName)) ??
		first(names.filter((n) => /^\d{4}-\d{2}-\d{2}/.test(n)))
	);
}

/** How many same-rank gate candidates tie when pickGateName resolves to none —
 * two `00-` covers, two folder-notes, two date-named files. Walks the same rank
 * ladder pickGateName uses: the first sub-rank (00-, folder-note, date) with a
 * lone candidate resolves the gate (returns 0, no tie); the first with two or more
 * is the tie and its count is returned. A set with several plain peers and no
 * distinguished cover at any rank is not a "gates" tie (returns 0) — that is the
 * ordinary no-gate case. Used only to LABEL the tie; gate resolution stays with
 * pickGateName ("no guessing"). */
export function gateTieCount(names: string[], containerName: string): number {
	if (names.length <= 1) return 0;
	const subRanks = [
		names.filter((n) => /^00[-_ ]/.test(n)),
		names.filter((n) => n.replace(/\.md$/i, "") === containerName),
		names.filter((n) => /^\d{4}-\d{2}-\d{2}/.test(n)),
	];
	for (const r of subRanks) {
		if (r.length === 1) return 0; // a distinguished cover resolves — no tie
		if (r.length >= 2) return r.length; // same-rank tie
	}
	return 0; // no distinguished cover at any rank — the ordinary no-gate fold
}

/** A draft awaiting review: the tri-state `reviewed` reads false (the boolean or
 * the string "false"). Keyed on the configured field name. */
export function isUnreviewed(fm: Record<string, unknown>, reviewedField: string): boolean {
	const v = fm[reviewedField];
	return v === false || v === "false";
}

/** Explicitly stamped reviewed: true (the boolean or the string "true") —
 * distinct from merely not-a-draft (a file with no reviewed field is neither).
 * Keyed on the configured field name. */
export function isApproved(fm: Record<string, unknown>, reviewedField: string): boolean {
	const v = fm[reviewedField];
	return v === true || v === "true";
}

/**
 * Publish the vertical-centering shift on a kit pane's contentEl, shared by every
 * kit view (For You, queue, CONFIG). The user's vertical-placement bias
 * (settings.nowVerticalBias, a percent-ish share of screen height; negative =
 * higher) emits everywhere as --nkui-screen-shift, which the stylesheet turns
 * into spacer min-heights.
 *
 * Desktop has no navbar to correct, so it emits the BIAS alone. Mobile also
 * corrects for the pane sitting below the screen centre: 2 × (screen centre −
 * pane centre) px, positive when the pane's centre is above the screen's and the
 * content must move DOWN. Pass `rect` to reuse an already-measured box (nowView
 * measures once per settle); omit it to measure here. A hidden pane (height 0)
 * keeps its last value and the next onResize re-measures.
 */
export function applyScreenShift(
	contentEl: HTMLElement,
	settings: NoteKitUiSettings,
	rect?: DOMRect
): void {
	const bias = ((settings.nowVerticalBias ?? 0) / 100) * window.innerHeight;
	if (!Platform.isMobile) {
		contentEl.style.setProperty("--nkui-screen-shift", `${bias}px`);
		return;
	}
	const pane = rect ?? contentEl.getBoundingClientRect();
	if (pane.height <= 0) return;
	const shift = window.innerHeight / 2 - (pane.top + pane.height / 2);
	contentEl.style.setProperty("--nkui-screen-shift", `${2 * shift + bias}px`);
}

/**
 * The debounced re-render every kit view arms in its constructor: defer the
 * rebuild while a TEXTAREA/INPUT inside the view's contentEl is focused (adding
 * or editing a task, picking an option, editing a CONFIG cell), so a background
 * vault change never blows away in-progress text — re-arm until the field blurs,
 * then catch up via `reload`. 250ms trailing, matching the per-view copies it
 * replaces.
 */
export function makeDeferredRender(view: ItemView, reload: () => void): () => void {
	const scheduled = debounce(
		() => {
			const ae = document.activeElement;
			if (
				ae instanceof HTMLElement &&
				(ae.tagName === "TEXTAREA" || ae.tagName === "INPUT") &&
				view.contentEl.contains(ae)
			) {
				scheduled();
				return;
			}
			reload();
		},
		250,
		false
	);
	return scheduled;
}
