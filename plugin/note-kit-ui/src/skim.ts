import { MarkdownPostProcessorContext } from "obsidian";
import type NoteKitUiPlugin from "./main";

/**
 * Skim mode — a reading-view declutter, implemented as a markdown post-processor
 * (the same composition pattern as injectReviewedHeader: run alongside
 * Obsidian's own rendering, never touch the file). READING VIEW ONLY — live
 * preview is a CM6 editor surface that a post-processor never sees, so the live
 * minimize/fold there would need a separate CM6 ViewPlugin (out of scope; the
 * setting copy says so).
 *
 * Three shapes, one `skimMode` setting:
 *  · minimize-completed — purely the body class nkui-skim-min-done (the
 *    stylesheet dims `- [x]` items the way crossed-off queue lines read); no
 *    per-section work, so this branch is a no-op here.
 *  · fold-keywords — a heading whose text matches a user keyword starts folded:
 *    its following siblings (up to the next heading of equal-or-higher level)
 *    are hidden, and the heading gets a click-to-expand affordance.
 *  · first-last — every heading starts folded EXCEPT the file's first and last,
 *    for a fast skim of just the opening and closing.
 *
 * The post-processor fires per SECTION render, and sibling sections may not all
 * be in the DOM when an early one renders. So the fold work is deferred to a
 * microtask-ish timeout keyed on the stable sizer and run once per full render:
 * by then every section is attached and the heading→content sibling walk is
 * reliable (the same "defer to a settled layout" tactic the queue routing uses).
 */
export class SkimProcessor {
	constructor(private plugin: NoteKitUiPlugin) {}

	/** Heading levels we fold on. */
	private static readonly HEADING_SEL = "h1, h2, h3, h4, h5, h6";

	/** Headings this processor decorated, each with the exact click listener it
	 * was given, so stop() can remove the listeners (and clean the chevron/fold
	 * classes) on unload — otherwise an open reading view pins the old instance. */
	private decorated = new Map<HTMLElement, (ev: MouseEvent) => void>();

	/** Remove every decoration this processor added: detach each heading's click
	 * listener, drop the chevron span, and clear the skim classes/attrs so a
	 * reload starts clean. Called from main.ts onunload, mirroring decorator.stop(). */
	stop(): void {
		for (const [heading, handler] of this.decorated) {
			heading.removeEventListener("click", handler);
			heading.querySelector(".nkui-skim-chevron")?.remove();
			heading.classList.remove("nkui-skim-heading");
			const section = heading.closest<HTMLElement>(".nkui-skim-head");
			if (section) {
				section.classList.remove("nkui-skim-head", "is-folded");
				delete section.dataset.nkuiSkim;
				delete section.dataset.nkuiSkimState;
			}
		}
		this.decorated.clear();
	}

	process(el: HTMLElement, ctx: MarkdownPostProcessorContext): void {
		const mode = this.plugin.settings.skimMode;
		// minimize-completed is a body class only (the stylesheet does the dimming);
		// off means nothing folds. Either way no per-section folding runs.
		if (mode !== "fold-keywords" && mode !== "first-last") return;

		// Walk up to the preview sizer — the stable container that persists across
		// section re-renders. Mirrors injectReviewedHeader's anchor choice.
		const sizer = el.closest(".markdown-preview-sizer");
		if (!sizer) return;
		// Reading view only — never an embed (![[note]]) or a hover-preview; their
		// render has a sizer too, and folding them would hide content mid-document.
		if (el.closest(".markdown-embed, .popover, .hover-popover")) return;

		void ctx; // ctx.sourcePath is available if a future shape needs the file

		// Coalesce: schedule one fold pass per render. A flag on the sizer keeps
		// repeated section calls from each queuing a pass.
		const host = sizer as HTMLElement;
		if (host.dataset.nkuiSkimPending === "1") return;
		host.dataset.nkuiSkimPending = "1";
		window.setTimeout(() => {
			delete host.dataset.nkuiSkimPending;
			this.applyFolds(host, mode);
		}, 0);
	}

	/** Pair each heading element under a preview sizer with its section wrapper
	 * and level, in document order. Reading-view sections are the sizer's direct
	 * children (.el-h2, .el-p, …); each heading lives inside its own wrapper. The
	 * reviewed-header injection is skipped — it is chrome, not document content. */
	private collectHeadings(
		sizer: HTMLElement
	): { section: HTMLElement; heading: HTMLElement; level: number }[] {
		const sections = Array.from(sizer.children) as HTMLElement[];
		const headings: { section: HTMLElement; heading: HTMLElement; level: number }[] = [];
		for (const section of sections) {
			if (section.classList.contains("nkui-reviewed-header")) continue;
			const heading = section.matches(SkimProcessor.HEADING_SEL)
				? section
				: section.querySelector<HTMLElement>(SkimProcessor.HEADING_SEL);
			if (!heading) continue;
			const level = Number(heading.tagName.slice(1)) || 6;
			headings.push({ section, heading, level });
		}
		return headings;
	}

	/** Fold the sections of one fully-rendered preview sizer. Idempotent: a
	 * heading already wearing the skim toggle is re-evaluated, not re-wrapped, so
	 * a re-render (theme change, scroll virtualization) settles to the same shape. */
	private applyFolds(sizer: HTMLElement, mode: string): void {
		const headings = this.collectHeadings(sizer);
		if (!headings.length) return;

		const keywords =
			mode === "fold-keywords"
				? this.plugin.settings.skimFoldKeywords
						.split(",")
						.map((k) => k.trim().toLowerCase())
						.filter(Boolean)
				: [];

		headings.forEach((h, i) => {
			// The default fold decision for this heading under the active mode.
			let shouldFold = false;
			if (mode === "fold-keywords") {
				const text = (h.heading.textContent ?? "").toLowerCase();
				shouldFold = keywords.some((k) => text.includes(k));
			} else if (mode === "first-last") {
				// Fold everything except the first and last heading's section.
				shouldFold = i !== 0 && i !== headings.length - 1;
			}
			const firstTime = h.section.dataset.nkuiSkimState === undefined;
			this.decorate(h.section, h.heading);
			// First sighting → apply the mode's default. A re-render of an already
			// seen heading re-applies its REMEMBERED state instead, so a user who
			// expanded a folded section keeps it open across re-renders (the toggle
			// owns the state thereafter).
			const fold = firstTime ? shouldFold : h.section.dataset.nkuiSkimState === "folded";
			this.setFolded(sizer, headings, i, fold);
		});
	}

	/** Add the click-to-expand affordance to a heading section once. The whole
	 * heading row is the hit target (whole-row interaction idiom); a leading
	 * chevron marker shows fold state via CSS rotation. */
	private decorate(section: HTMLElement, heading: HTMLElement): void {
		if (section.dataset.nkuiSkim === "1") return;
		section.dataset.nkuiSkim = "1";
		section.classList.add("nkui-skim-head");
		heading.classList.add("nkui-skim-heading");
		// A marker span, styled by CSS (a small triangle that rotates on fold).
		if (!heading.querySelector(".nkui-skim-chevron")) {
			const chevron = createSpan({ cls: "nkui-skim-chevron" });
			heading.prepend(chevron);
		}
		// Track the exact handler so stop() (onunload) can detach it; without this
		// an open reading view keeps the listener and pins the old plugin instance.
		const handler = (ev: MouseEvent): void => {
			ev.preventDefault();
			ev.stopPropagation();
			const sizer = section.closest(".markdown-preview-sizer");
			if (!(sizer instanceof HTMLElement)) return;
			// Recompute the heading list at click time so it tracks any re-render.
			const folded = section.dataset.nkuiSkimState === "folded";
			this.toggle(sizer, section, !folded);
		};
		heading.addEventListener("click", handler);
		this.decorated.set(heading, handler);
	}

	/** Toggle one heading by section reference: re-derive the list, find it,
	 * fold/unfold its block. */
	private toggle(sizer: HTMLElement, section: HTMLElement, fold: boolean): void {
		const headings = this.collectHeadings(sizer);
		const idx = headings.findIndex((h) => h.section === section);
		if (idx < 0) return;
		this.setFolded(sizer, headings, idx, fold);
	}

	/** Hide (or reveal) the sections that belong to heading `idx` — every sibling
	 * after it up to the next heading of equal-or-higher level. Records the state
	 * on the heading section so a re-render restores it. */
	private setFolded(
		_sizer: HTMLElement,
		headings: { section: HTMLElement; heading: HTMLElement; level: number }[],
		idx: number,
		fold: boolean
	): void {
		const self = headings[idx];
		self.section.dataset.nkuiSkimState = fold ? "folded" : "open";
		self.section.classList.toggle("is-folded", fold);
		// Walk forward in DOM order from the heading's section until a heading of
		// equal-or-higher level (a smaller-or-equal level number) is reached — that
		// marks the end of this heading's owned block.
		//
		// Folding hides every owned section. Unfolding reveals them, EXCEPT the
		// content of a deeper child heading that is itself still folded: a nested
		// fold survives its parent re-opening. `skipUntilLevel` holds the level of
		// the still-folded child whose content we're keeping hidden — cleared once
		// a sibling at or above that level ends the child's block.
		let node = self.section.nextElementSibling as HTMLElement | null;
		let skipUntilLevel: number | null = null;
		while (node) {
			const owns = headings.find((h) => h.section === node);
			if (owns && owns.level <= self.level) break; // next section of same/higher rank
			if (fold) {
				node.classList.add("nkui-skim-hidden");
			} else if (owns) {
				// A sub-heading: its end-of-block clears any active skip if this
				// heading is at or above the skipped level. The heading row itself
				// always re-shows (part of this block's outline); its own content
				// stays hidden when it is still folded.
				if (skipUntilLevel !== null && owns.level <= skipUntilLevel) skipUntilLevel = null;
				node.classList.remove("nkui-skim-hidden");
				if (node.classList.contains("is-folded")) skipUntilLevel = owns.level;
			} else if (skipUntilLevel === null) {
				node.classList.remove("nkui-skim-hidden");
			}
			// else: content owned by a still-folded child — left hidden.
			node = node.nextElementSibling as HTMLElement | null;
		}
	}
}
