/** Shared press-and-hold commit affordance. Every hold in the kit (gate approve,
 * approve all, undo) runs through attachHold so the arming rules — the fill, the
 * swallowed tap, the keyboard shortcut — stay identical everywhere. */

/** Shipped press-and-hold duration (ms). The LIVE value is the "Hold duration"
 * setting — main.ts calls configureHolds(settings.holdMs) on load and on every
 * save, and css.ts emits the matching --nkui-hold so the fill animation always
 * tracks the configured commit time. */
export const HOLD_MS = 303;

/** "close tab?" offer countdown (ms) — the hold fill run in reverse (see
 * main.ts injectCloseButton and the .nkui-close-offer transitions). Scales
 * with the configured hold (5x) so the pair keeps its shipped ratio. */
export const CLOSE_OFFER_MS = 1517;

let liveHoldMs = HOLD_MS;

/** Set the live hold duration from settings (clamped to the settings range). */
export function configureHolds(ms: number): void {
	liveHoldMs = Number.isFinite(ms) && ms > 0 ? ms : HOLD_MS;
}

/** The live "close tab?" countdown — 5x the configured hold, matching the
 * shipped 303/1517 ratio. */
export function closeOfferMs(): number {
	return Math.round(liveHoldMs * 5);
}

export interface HoldOpts {
	/** Runs when the press is held the full duration (or on keyboard Enter/Space). */
	onCommit: () => void | Promise<void>;
	/** Runs on a released-early press (a tap). Optional — without it a tap does
	 * nothing, matching the original holds. */
	onTap?: () => void;
	holdMs?: number;
	/** While held, this class is added to `armTarget` (e.g. the section's
	 * `is-arming`, which lights up the candidate rows). */
	armClass?: string;
	armTarget?: HTMLElement | null;
}

/**
 * Press-and-hold to commit; a fill sweeps while held (`is-holding` on the element).
 * Releasing early is a tap — it fires `onTap` when given, otherwise nothing: a
 * tap never folds a section or opens the row's file (the release click is
 * swallowed). Keyboard Enter/Space commits directly — a keyboard can't "hold".
 */
export function attachHold(el: HTMLElement, opts: HoldOpts): void {
	const hold = opts.holdMs ?? liveHoldMs;
	let timer: number | undefined;
	let holding = false;
	let committed = false;
	const end = (): void => {
		holding = false;
		el.removeClass("is-holding");
		if (opts.armClass) opts.armTarget?.removeClass(opts.armClass);
		if (timer) window.clearTimeout(timer);
		timer = undefined;
	};
	el.addEventListener("pointerdown", (ev) => {
		ev.preventDefault();
		ev.stopPropagation();
		if (holding) return;
		holding = true;
		committed = false;
		el.addClass("is-holding");
		if (opts.armClass) opts.armTarget?.addClass(opts.armClass);
		timer = window.setTimeout(() => {
			committed = true;
			end();
			void opts.onCommit();
		}, hold);
	});
	// The tap is decided on pointerup (not click — preventDefault on pointerdown
	// suppresses the click on some mobile platforms): released while still armed
	// and before the commit landed.
	el.addEventListener("pointerup", () => {
		const tapped = holding && !committed;
		end();
		if (tapped) opts.onTap?.();
	});
	el.addEventListener("pointerleave", end);
	el.addEventListener("pointercancel", end);
	// The release click still bubbles — swallow it so a press never folds the
	// section or opens the row's file underneath.
	el.addEventListener("click", (ev) => ev.stopPropagation());
	el.addEventListener("keydown", (ev) => {
		if (ev.key === "Enter" || ev.key === " ") {
			ev.preventDefault();
			ev.stopPropagation();
			// A held key auto-repeats — only the first press commits.
			if (ev.repeat) return;
			void opts.onCommit();
		}
	});
}

/** Keyboard activation for a role="button" element: Enter AND Space both fire
 * (Space preventDefaulted so it never scrolls the page), matching the native
 * button contract. */
export function attachKeyActivate(el: HTMLElement, activate: () => void): void {
	el.addEventListener("keydown", (ev) => {
		if (ev.key !== "Enter" && ev.key !== " ") return;
		ev.preventDefault();
		activate();
	});
}
