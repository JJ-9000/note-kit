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
 */
export function pickGateName(names: string[], containerName: string): string | null {
	const one = (xs: string[]): string | null => (xs.length === 1 ? xs[0] : null);
	return (
		one(names) ??
		one(names.filter((n) => /^00[-_ ]/.test(n))) ??
		one(names.filter((n) => n.replace(/\.md$/i, "") === containerName)) ??
		one(names.filter((n) => /^\d{4}-\d{2}-\d{2}/.test(n)))
	);
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
