import { TFile, TFolder, Vault, debounce } from "obsidian";
import type NoteKitUiPlugin from "./main";

/**
 * Decorates file-explorer rows with data attributes the stylesheet keys off:
 *   data-nkui-prefix   (a) numeric structural prefix
 *   data-nkui-type     (c) note type (only for types with a configured colour)
 *   data-nkui-reviewed (d) "false" on unreviewed drafts
 * Also rewrites displayed names to hide the prefix (b) and injects the live
 * "needs you" pill on inbox folder rows (d) — drafts awaiting a decision, not
 * sets already approved and waiting on the filing-agent.
 *
 * All DOM writes are idempotent: re-running on an already-decorated row produces
 * no mutation, so the MutationObserver that drives re-decoration cannot loop.
 */
export class ExplorerDecorator {
	private plugin: NoteKitUiPlugin;
	private observers: MutationObserver[] = [];
	private redraw: () => void;

	private prefixSet = new Set<string>();
	private sinkSet = new Set<string>();
	private typeColors = new Set<string>();
	private floatSet = new Set<string>();
	private hideRe: RegExp | null = null;

	constructor(plugin: NoteKitUiPlugin) {
		this.plugin = plugin;
		this.redraw = debounce(() => this.decorateAll(), 150, false);
	}

	start(): void {
		this.compile();
		const app = this.plugin.app;
		// Decorate the affected row synchronously on cache/rename events, then let the
		// debounced full pass do the bookkeeping — routing these through the debounce
		// alone leaves the row raw for ~150ms and it visibly snaps into shape (see the
		// vault note on decorating in the MutationObserver callback).
		this.plugin.registerEvent(
			app.metadataCache.on("changed", (file) => {
				this.decorateByPath(file.path);
				this.redraw();
			})
		);
		this.plugin.registerEvent(app.metadataCache.on("resolved", this.redraw));
		this.plugin.registerEvent(
			app.vault.on("rename", (file) => {
				this.decorateByPath(file.path);
				this.redraw();
			})
		);
		this.plugin.registerEvent(app.vault.on("create", this.redraw));
		this.plugin.registerEvent(app.vault.on("delete", this.redraw));
		this.plugin.registerEvent(
			app.workspace.on("layout-change", () => {
				this.attachObservers();
				this.redraw();
			})
		);
		this.attachObservers();
		this.decorateAll();
	}

	stop(): void {
		for (const o of this.observers) o.disconnect();
		this.observers = [];
		this.clearAll();
	}

	refresh(): void {
		this.compile();
		this.attachObservers();
		this.clearAll();
		this.decorateAll();
	}

	// ── setup ────────────────────────────────────────────────────────────────

	private compile(): void {
		const s = this.plugin.settings;
		this.prefixSet = new Set(s.prefixStyles.map((p) => p.prefix));
		// A "sink" is a folder whose prefix rule fades it (opacity < 1, e.g. 99-).
		// Tagging the wrapper lets the stylesheet dim its contents to match.
		this.sinkSet = new Set(
			s.dimSinkContents
				? s.prefixStyles
						.filter((p) => Number.isFinite(p.opacity) && p.opacity < 1)
						.map((p) => p.prefix)
				: []
		);
		this.typeColors = new Set(this.plugin.typeStyles().filter((t) => t.color).map((t) => t.type));
		this.floatSet = new Set(s.floatTopTypes ?? []);
		if (s.enableHidePrefix && this.prefixSet.size) {
			const alt = [...this.prefixSet].map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
			this.hideRe = new RegExp(`^(?:${alt})[ _-]+`);
		} else {
			this.hideRe = null;
		}
	}

	private containers(): HTMLElement[] {
		const out: HTMLElement[] = [];
		for (const leaf of this.plugin.app.workspace.getLeavesOfType("file-explorer")) {
			const c = leaf.view.containerEl.querySelector(".nav-files-container");
			if (c instanceof HTMLElement) out.push(c);
		}
		return out;
	}

	private attachObservers(): void {
		const live = this.containers();
		// Drop observers whose container is gone; keep ones still attached.
		this.observers = this.observers.filter((o) => {
			// no public way to read target; simplest is to rebuild — disconnect all.
			o.disconnect();
			return false;
		});
		for (const c of live) {
			const obs = new MutationObserver((muts) => this.onMutations(muts));
			// attributeFilter: a rename updates data-path on the existing element —
			// no childList mutation fires, so without this the row waits for the
			// debounced pass and snaps.
			obs.observe(c, {
				childList: true,
				subtree: true,
				attributes: true,
				attributeFilter: ["data-path"],
			});
			this.observers.push(obs);
		}
	}

	/**
	 * Decorate newly-inserted rows synchronously, then schedule the debounced full
	 * pass. The observer callback runs as a microtask — before the browser paints —
	 * so a freshly-expanded folder's rows appear already-styled instead of rendering
	 * raw (prefix showing, no type dot) and snapping into place ~150ms later when the
	 * debounced redraw catches up. The full pass still runs for inbox counts and any
	 * row the targeted walk missed; decoration is idempotent, so the overlap is free.
	 */
	private onMutations(muts: MutationRecord[]): void {
		const s = this.plugin.settings;
		for (const m of muts) {
			if (m.type === "attributes" && m.target instanceof HTMLElement) {
				if (m.target.matches(".nav-file-title, .nav-folder-title")) this.decorate(m.target, s);
				continue;
			}
			m.addedNodes.forEach((node) => {
				if (!(node instanceof HTMLElement)) return;
				if (node.matches(".nav-file-title, .nav-folder-title")) this.decorate(node, s);
				node
					.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title")
					.forEach((el) => this.decorate(el, s));
			});
		}
		this.redraw();
	}

	/** Synchronous, targeted decoration of one row by its data-path. */
	private decorateByPath(path: string): void {
		const s = this.plugin.settings;
		const sel = `.nav-file-title[data-path="${cssEscape(path)}"], .nav-folder-title[data-path="${cssEscape(path)}"]`;
		for (const c of this.containers()) {
			const el = c.querySelector<HTMLElement>(sel);
			if (el) this.decorate(el, s);
		}
	}

	// ── decoration ─────────────────────────────────────────────────────────────

	private decorateAll(): void {
		const s = this.plugin.settings;
		for (const c of this.containers()) {
			const titles = c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title");
			titles.forEach((el) => this.decorate(el, s));
		}
		this.updateInboxCounts();
	}

	private decorate(el: HTMLElement, s = this.plugin.settings): void {
		const path = el.getAttribute("data-path");
		if (!path) return;
		const isFolder = el.classList.contains("nav-folder-title");
		const leaf = path.split("/").pop() ?? path;
		const name = isFolder ? leaf : leaf.replace(/\.md$/i, "");

		// (a) prefix — only configured prefixes; never matches YYYY dates
		const m = name.match(/^(\d{2,})[ _-]/);
		const matched = m && this.prefixSet.has(m[1]) ? m[1] : null;
		this.setAttr(el, "data-nkui-prefix", s.enablePrefixStyling ? matched : null);

		// (a2) sink folder — tag the wrapping .nav-folder so the stylesheet can
		// fade everything *inside* an expanded sink (e.g. 99-Archive). The folder's
		// own row is still styled by (a); this only reaches its descendants.
		if (isFolder) {
			const wrapper = el.closest<HTMLElement>(".nav-folder");
			const sinkHit = matched && this.sinkSet.has(matched) ? matched : null;
			if (wrapper) this.setAttr(wrapper, "data-nkui-sink", sinkHit);
		}

		// (c) type + (d) reviewed + (e) queue surface — files only
		if (!isFolder) {
			// The configured queues (user/machine) are interaction surfaces, not
			// content: tag them so the stylesheet floats them to the top of their
			// folder and gives them the same accent as their For You buckets.
			const isQueue = path === s.userQueuePath || path === s.machineQueuePath;
			this.setAttr(el, "data-nkui-queue", isQueue ? "true" : null);

			const fm = this.plugin.app.metadataCache.getCache(path)?.frontmatter;
			let typeHit: string | null = null;
			if (s.enableTypeStyling && fm) {
				const t = fm[s.typeField];
				if (t != null && this.typeColors.has(String(t))) typeHit = String(t);
			}
			this.setAttr(el, "data-nkui-type", typeHit);

			// (e2) float-to-top — a file whose type is in the configured set sorts to
			// the top of its folder (CSS order). Uses the raw type, colour or not.
			const rawType = fm?.[s.typeField] != null ? String(fm[s.typeField]) : null;
			this.setAttr(el, "data-nkui-float", rawType && this.floatSet.has(rawType) ? "true" : null);

			const draft =
				s.enableReviewFlags && s.showRowBadge && fm ? this.isUnreviewed(fm, s) : false;
			this.setAttr(el, "data-nkui-reviewed", draft ? "false" : null);
		}

		// (b) hide prefix — rewrite display text, keep filename on disk
		const content = el.querySelector<HTMLElement>(
			isFolder ? ".nav-folder-title-content" : ".nav-file-title-content"
		);
		if (content) {
			if (s.enableHidePrefix && this.hideRe) {
				const current = content.textContent ?? "";
				const stripped = current.replace(this.hideRe, "");
				if (stripped !== current) {
					if (content.dataset.nkuiOrig === undefined) content.dataset.nkuiOrig = current;
					content.textContent = stripped;
				}
			} else if (content.dataset.nkuiOrig !== undefined) {
				content.textContent = content.dataset.nkuiOrig;
				delete content.dataset.nkuiOrig;
			}
		}
	}

	private updateInboxCounts(): void {
		const s = this.plugin.settings;
		const show = s.enableReviewFlags && s.showInboxCount;
		for (const c of this.containers()) {
			for (const folderPath of s.inboxFolders) {
				const titleEl = c.querySelector<HTMLElement>(
					`.nav-folder-title[data-path="${cssEscape(folderPath)}"]`
				);
				if (!titleEl) continue;
				let badge = titleEl.querySelector<HTMLElement>(".nkui-inbox-count");
				if (!show) {
					badge?.remove();
					continue;
				}
				const n = this.countNeedsAttention(folderPath, s);
				if (n <= 0) {
					badge?.remove();
					continue;
				}
				if (!badge) badge = titleEl.createSpan({ cls: "nkui-inbox-count" });
				const text = String(n);
				if (badge.textContent !== text) badge.setText(text);
			}
		}
	}

	/**
	 * Count the inbox drafts that actually need the user — not every unreviewed
	 * file lingering in the folder. A draft in a working set whose gate is already
	 * approved is awaiting the filing-agent, not a decision (CONFIG § Group
	 * approval): it sits in the inbox but needs nothing from the user, so it must
	 * not inflate the attention pill. This mirrors the For You view's
	 * "awaiting-filing" exclusion. Loose drafts at the inbox root, and drafts in
	 * sets whose gate is still a draft, count.
	 */
	private countNeedsAttention(folderPath: string, s = this.plugin.settings): number {
		const folder = this.plugin.app.vault.getAbstractFileByPath(folderPath);
		if (!(folder instanceof TFolder)) return 0;

		// Per container (the first path segment under the inbox): its root-level
		// members by name + approval, used to resolve the set's gate; and the count
		// of unreviewed drafts anywhere inside it. A draft directly at the inbox
		// root has no container and always counts.
		const roots = new Map<string, { name: string; approved: boolean }[]>();
		const draftsByContainer = new Map<string, number>();
		let looseDrafts = 0;

		Vault.recurseChildren(folder, (af) => {
			if (!(af instanceof TFile) || af.extension !== "md") return;
			const segs = af.path.slice(folderPath.length + 1).split("/");
			const fm = this.plugin.app.metadataCache.getFileCache(af)?.frontmatter;
			const draft = fm ? this.isUnreviewed(fm, s) : false;
			if (segs.length === 1) {
				if (draft) looseDrafts++;
				return;
			}
			const container = segs[0];
			if (segs.length === 2) {
				const arr = roots.get(container) ?? [];
				arr.push({ name: segs[1], approved: fm ? this.isApproved(fm, s) : false });
				roots.set(container, arr);
			}
			if (draft) draftsByContainer.set(container, (draftsByContainer.get(container) ?? 0) + 1);
		});

		let n = looseDrafts;
		for (const [container, count] of draftsByContainer) {
			const members = roots.get(container) ?? [];
			const gate = pickGateName(members.map((m) => m.name));
			const awaitingFiling = !!gate && (members.find((m) => m.name === gate)?.approved ?? false);
			if (!awaitingFiling) n += count;
		}
		return n;
	}

	private isUnreviewed(fm: Record<string, unknown>, s = this.plugin.settings): boolean {
		const v = fm[s.reviewedField];
		return v === false || v === "false";
	}

	private isApproved(fm: Record<string, unknown>, s = this.plugin.settings): boolean {
		const v = fm[s.reviewedField];
		return v === true || v === "true";
	}

	// ── teardown ─────────────────────────────────────────────────────────────

	private clearAll(): void {
		for (const c of this.containers()) {
			c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title").forEach((el) => {
				el.removeAttribute("data-nkui-prefix");
				el.removeAttribute("data-nkui-type");
				el.removeAttribute("data-nkui-reviewed");
				el.removeAttribute("data-nkui-queue");
				el.removeAttribute("data-nkui-float");
				const content = el.querySelector<HTMLElement>(
					".nav-folder-title-content, .nav-file-title-content"
				);
				if (content && content.dataset.nkuiOrig !== undefined) {
					content.textContent = content.dataset.nkuiOrig;
					delete content.dataset.nkuiOrig;
				}
			});
			c.querySelectorAll<HTMLElement>(".nav-folder[data-nkui-sink]").forEach((el) =>
				el.removeAttribute("data-nkui-sink")
			);
			c.querySelectorAll<HTMLElement>(".nkui-inbox-count").forEach((b) => b.remove());
		}
	}

	private setAttr(el: HTMLElement, name: string, value: string | null): void {
		if (value === null) el.removeAttribute(name);
		else if (el.getAttribute(name) !== value) el.setAttribute(name, value);
	}
}

/**
 * Resolve a working set's gate by the names of its root-level members — the
 * name-only twin of nowView's pickGate (the decorator only has names + approval
 * to work with). A lone root file IS the gate; among several, a single
 * 00-prefixed cover wins, else a single date-named file. Ambiguity resolves to
 * none — no guessing.
 */
function pickGateName(names: string[]): string | null {
	const one = (xs: string[]): string | null => (xs.length === 1 ? xs[0] : null);
	return (
		one(names) ??
		one(names.filter((n) => /^00[-_ ]/.test(n))) ??
		one(names.filter((n) => /^\d{4}-\d{2}-\d{2}/.test(n)))
	);
}

/** CSS.escape is unavailable in some mobile webviews; fall back to a minimal escape. */
function cssEscape(v: string): string {
	const g = globalThis as unknown as { CSS?: { escape?: (s: string) => string } };
	if (g.CSS?.escape) return g.CSS.escape(v);
	return v.replace(/["\\]/g, "\\$&");
}
