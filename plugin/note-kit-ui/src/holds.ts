/** Shared press-and-hold commit affordance. Every hold in the kit (row approve,
 * section approve-all, undo) runs through attachHold so the arming rules — the
 * fill, the swallowed tap, the keyboard shortcut — stay identical everywhere. */

/** The ONE shipped press-and-hold duration (ms) — the single source of truth for
 * hold timing. The settings default (settings.ts holdMs) IS this constant; the
 * close-offer (closeOfferFor / closeOfferMs) derives from it; and the CSS static fallbacks
 * (--nkui-hold / --nkui-count in styles.css :root) mirror it by hand. The LIVE
 * value is the "Hold duration" setting — main.ts calls configureHolds(settings.holdMs)
 * on load and every save, and css.ts emits the matching --nkui-hold / --nkui-count
 * from it, overriding the static fallbacks. (Was three disagreeing literals — 395
 * here, 610 in settings, 303 in the CSS fallback; unified to the tuned 610.) */
export const HOLD_MS = 610;

/** Arm delay (ms) between the press and the VISIBLE hold (`is-holding`, the
 * fill): a quick tap releases inside this window and never flashes the fill.
 * The COMMIT still lands at the full configured hold — the fill just sweeps
 * the remainder, so css.ts emits --nkui-hold as holdMs minus this delay
 * (clamped; one source of truth, imported from here). */
export const HOLD_ARM_DELAY_MS = 120;

/** The arm delay in force for a given total hold: never so long it eats the
 * fill below the 100ms css.ts clamps it to — a short configured hold shrinks
 * the delay, not the visible sweep. */
export function armDelayMs(holdMs: number): number {
	return Math.max(0, Math.min(HOLD_ARM_DELAY_MS, holdMs - 100));
}

/** The close-offer countdown is this many times the hold — the ONE place the
 * ratio lives, shared by closeOfferMs() (the live value) and css.ts's --nkui-count
 * emission, so the "close tab?" window always tracks the commit fill. */
export const CLOSE_OFFER_RATIO = 5;

/** The close-offer countdown (ms) for a given hold duration — the single close-
 * offer timing authority (see main.ts injectCloseButton, the .nkui-close-offer
 * transitions, and css.ts's --nkui-count). */
export function closeOfferFor(holdMs: number): number {
	return Math.round(holdMs * CLOSE_OFFER_RATIO);
}

let liveHoldMs = HOLD_MS;

/** Set the live hold duration from settings (clamped to the settings range). */
export function configureHolds(ms: number): void {
	liveHoldMs = Number.isFinite(ms) && ms > 0 ? ms : HOLD_MS;
}

/** The live "close tab?" countdown — the configured hold run through the shared
 * ratio (closeOfferFor), matching css.ts's --nkui-count emission. */
export function closeOfferMs(): number {
	return closeOfferFor(liveHoldMs);
}

/** When any hold last committed (module-level — shared across every hold).
 * A commit re-renders the page and the layout shift can put the still-pressed
 * cursor over a DIFFERENT hold; arming that one from the same physical press
 * reads as "the hold persists". Any pointerdown inside this window is ignored. */
let lastCommitAt = 0;
const COMMIT_SUPPRESS_MS = 350;

/** Movement allowance while a hold arms (px). At row scale the press target is
 * also the scroll surface — the row must NOT carry touch-action: none, so the
 * list keeps scrolling. A drift past this slop reads as a scroll (or a drag),
 * not a hold, and disarms without committing. Touch scrolling also fires
 * pointercancel once the browser claims the gesture; the slop catches mice,
 * pens, and the slow pre-scroll drift before that claim lands. */
const HOLD_SLOP_PX = 8;

/** Eat the release that follows a pointer-held commit at the document level
 * (capture phase, one-shot): after the commit re-renders, the pointer may sit
 * over a row / fold header / another hold — the swallowed pointerup and click
 * must not interact with whatever landed under the cursor. The listeners
 * self-remove after the suppress window if no release arrives. */
function swallowNextRelease(): void {
	const swallow = (ev: Event): void => {
		ev.preventDefault();
		ev.stopPropagation();
	};
	document.addEventListener("pointerup", swallow, { capture: true, once: true });
	document.addEventListener("click", swallow, { capture: true, once: true });
	window.setTimeout(() => {
		document.removeEventListener("pointerup", swallow, true);
		document.removeEventListener("click", swallow, true);
	}, COMMIT_SUPPRESS_MS);
}

export interface HoldOpts {
	/** Runs when the press is held the full duration (or on keyboard Enter/Space). */
	onCommit: () => void | Promise<void>;
	/** Runs on a released-early press (a tap). Optional — without it a tap does
	 * nothing, matching the original holds. Receives the releasing pointer event
	 * when one exists (a keyboard tap passes none), so a row-scale tap can branch
	 * on its target or its modifier keys. */
	onTap?: (ev?: PointerEvent) => void;
	holdMs?: number;
	/** While held, this class is added to `armTarget` (e.g. the section's
	 * `is-arming`, which lights up the candidate rows). */
	armClass?: string;
	armTarget?: HTMLElement | null;
	/** Keyboard mirrors the pointer: Enter/Space held the full duration commits,
	 * released early taps (firing `onTap`). Without it (the default) Enter/Space
	 * commits at once — the original button behaviour, kept for the dedicated
	 * holds (e.g. the queue unlock) where the element has no tap action. */
	keyHold?: boolean;
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
	/** Delays the VISIBLE arm (`is-holding`) so a tap never flashes the fill;
	 * the commit timer above still runs from the press itself. */
	let armTimer: number | undefined;
	let holding = false;
	let committed = false;
	/** True while a keyboard hold (keyHold mode) is arming — pointer end events
	 * (leave/cancel/move) must not disarm a press the pointer never made. */
	let keyHeld = false;
	let startX = 0;
	let startY = 0;
	const end = (): void => {
		holding = false;
		keyHeld = false;
		el.removeClass("is-holding");
		if (opts.armClass) opts.armTarget?.removeClass(opts.armClass);
		if (timer) window.clearTimeout(timer);
		timer = undefined;
		if (armTimer) window.clearTimeout(armTimer);
		armTimer = undefined;
	};
	const commit = (): void => {
		committed = true;
		lastCommitAt = Date.now();
		end();
		void opts.onCommit();
	};
	// The visible arm waits out the delay — a tap released inside it never
	// flashes the fill. Releasing during the delay is still a tap (pointerup
	// reads `holding`, set immediately), and the commit timer runs from the
	// press, so the total hold time is unchanged.
	const armSoon = (): void => {
		armTimer = window.setTimeout(() => {
			el.addClass("is-holding");
			if (opts.armClass) opts.armTarget?.addClass(opts.armClass);
		}, armDelayMs(hold));
	};
	el.addEventListener("pointerdown", (ev) => {
		// A press landing on an interactive CHILD (a checkbox, a text field, a
		// nested button) belongs to that control — never armed, never swallowed.
		// The hold element itself may be a button (the queue unlock), so only a
		// strict descendant bails.
		const t = ev.target;
		if (t instanceof Element) {
			const it = t.closest("input, textarea, select, button, a");
			if (it && it !== el) return;
		}
		ev.preventDefault();
		ev.stopPropagation();
		// A press right after ANY commit is the same physical press handed to a
		// new element by the commit's layout shift — never a fresh intent. Ignore
		// it; the user lifts and presses again to arm.
		if (Date.now() - lastCommitAt < COMMIT_SUPPRESS_MS) return;
		if (holding) return;
		holding = true;
		committed = false;
		startX = ev.clientX;
		startY = ev.clientY;
		armSoon();
		timer = window.setTimeout(() => {
			swallowNextRelease();
			commit();
		}, hold);
	});
	// The tap is decided on pointerup (not click — preventDefault on pointerdown
	// suppresses the click on some mobile platforms): released while still armed
	// and before the commit landed.
	el.addEventListener("pointerup", (ev) => {
		if (keyHeld) return;
		const tapped = holding && !committed;
		end();
		if (tapped) opts.onTap?.(ev);
	});
	// Slop-cancel: a hold that drifts past the slop is a scroll or a drag, not a
	// commit intent — disarm. Essential at row scale, where the hold target IS
	// the scroll surface (the row keeps native touch-action so the list scrolls).
	el.addEventListener("pointermove", (ev) => {
		if (!holding || keyHeld) return;
		if (Math.hypot(ev.clientX - startX, ev.clientY - startY) > HOLD_SLOP_PX) end();
	});
	el.addEventListener("pointerleave", () => {
		if (!keyHeld) end();
	});
	el.addEventListener("pointercancel", () => {
		if (!keyHeld) end();
	});
	// The release click still bubbles — swallow it so a press never folds the
	// section or opens the row's file underneath.
	el.addEventListener("click", (ev) => ev.stopPropagation());
	el.addEventListener("keydown", (ev) => {
		if (ev.key !== "Enter" && ev.key !== " ") return;
		ev.preventDefault();
		ev.stopPropagation();
		// A held key auto-repeats — only the first press arms/commits.
		if (ev.repeat) return;
		if (!opts.keyHold) {
			// Keyboard commits shift layout too — suppress pointer arming for the
			// same window (no release to swallow: nothing is pressed).
			lastCommitAt = Date.now();
			void opts.onCommit();
			return;
		}
		// keyHold mode: the key mirrors the pointer — arm on keydown (the fill
		// sweeps), commit when held the full duration, tap on an early release.
		if (holding) return;
		holding = true;
		committed = false;
		keyHeld = true;
		armSoon();
		timer = window.setTimeout(commit, hold);
	});
	el.addEventListener("keyup", (ev) => {
		if (!keyHeld) return;
		if (ev.key !== "Enter" && ev.key !== " ") return;
		const tapped = holding && !committed;
		end();
		if (tapped) opts.onTap?.();
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
