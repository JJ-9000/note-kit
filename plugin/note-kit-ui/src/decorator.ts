import { TFile, TFolder, Vault, debounce } from "obsidian";
import type NoteKitUiPlugin from "./main";

/**
 * Decorates file-explorer rows with data attributes the stylesheet keys off:
 *   data-nkui-prefix   (a) numeric structural prefix
 *   data-nkui-type     (c) note type (only for types with a configured colour)
 *   data-nkui-reviewed (d) "false" on unreviewed drafts
 * Also rewrites displayed names to hide the prefix (b) and injects the live
 * "N unreviewed" pill on inbox folder rows (d).
 *
 * All DOM writes are idempotent: re-running on an already-decorated row produces
 * no mutation, so the MutationObserver that drives re-decoration cannot loop.
 */
export class ExplorerDecorator {
	private plugin: NoteKitUiPlugin;
	private observers: MutationObserver[] = [];
	private redraw: () => void;

	private prefixSet = new Set<string>();
	private typeColors = new Set<string>();
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
		this.typeColors = new Set(this.plugin.typeStyles().filter((t) => t.color).map((t) => t.type));
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
		let prefixHit: string | null = null;
		if (s.enablePrefixStyling) {
			const m = name.match(/^(\d{2,})[ _-]/);
			if (m && this.prefixSet.has(m[1])) prefixHit = m[1];
		}
		this.setAttr(el, "data-nkui-prefix", prefixHit);

		// (c) type + (d) reviewed — files only
		if (!isFolder) {
			const fm = this.plugin.app.metadataCache.getCache(path)?.frontmatter;
			let typeHit: string | null = null;
			if (s.enableTypeStyling && fm) {
				const t = fm[s.typeField];
				if (t != null && this.typeColors.has(String(t))) typeHit = String(t);
			}
			this.setAttr(el, "data-nkui-type", typeHit);

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
				const n = this.countUnreviewed(folderPath, s);
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

	private countUnreviewed(folderPath: string, s = this.plugin.settings): number {
		const folder = this.plugin.app.vault.getAbstractFileByPath(folderPath);
		if (!(folder instanceof TFolder)) return 0;
		let n = 0;
		Vault.recurseChildren(folder, (af) => {
			if (af instanceof TFile && af.extension === "md") {
				const fm = this.plugin.app.metadataCache.getFileCache(af)?.frontmatter;
				if (fm && this.isUnreviewed(fm, s)) n++;
			}
		});
		return n;
	}

	private isUnreviewed(fm: Record<string, unknown>, s = this.plugin.settings): boolean {
		const v = fm[s.reviewedField];
		return v === false || v === "false";
	}

	// ── teardown ─────────────────────────────────────────────────────────────

	private clearAll(): void {
		for (const c of this.containers()) {
			c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title").forEach((el) => {
				el.removeAttribute("data-nkui-prefix");
				el.removeAttribute("data-nkui-type");
				el.removeAttribute("data-nkui-reviewed");
				const content = el.querySelector<HTMLElement>(
					".nav-folder-title-content, .nav-file-title-content"
				);
				if (content && content.dataset.nkuiOrig !== undefined) {
					content.textContent = content.dataset.nkuiOrig;
					delete content.dataset.nkuiOrig;
				}
			});
			c.querySelectorAll<HTMLElement>(".nkui-inbox-count").forEach((b) => b.remove());
		}
	}

	private setAttr(el: HTMLElement, name: string, value: string | null): void {
		if (value === null) el.removeAttribute(name);
		else if (el.getAttribute(name) !== value) el.setAttribute(name, value);
	}
}

/** CSS.escape is unavailable in some mobile webviews; fall back to a minimal escape. */
function cssEscape(v: string): string {
	const g = globalThis as unknown as { CSS?: { escape?: (s: string) => string } };
	if (g.CSS?.escape) return g.CSS.escape(v);
	return v.replace(/["\\]/g, "\\$&");
}
