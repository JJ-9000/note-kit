import { MarkdownView, Plugin, TFile, WorkspaceLeaf } from "obsidian";
import { DEFAULT_SETTINGS, NoteKitUiSettings, NoteKitUiSettingTab, TypeStyle } from "./settings";
import { buildDynamicCss } from "./css";
import { ExplorerDecorator } from "./decorator";
import { NoteClassApplier } from "./noteClass";
import { NowView, NOW_VIEW_TYPE } from "./nowView";
import { KitFacts, readKitFacts } from "./kitConfig";
import { deriveThemePalette } from "./palette";
import { syncGraphColors } from "./graph";

export default class NoteKitUiPlugin extends Plugin {
	settings!: NoteKitUiSettings;
	/** Facts derived from the kit's CONFIG.md at load; null when none exists. */
	kitFacts: KitFacts | null = null;
	/** Theme-derived type palette; null when the toggle is off or underivable. */
	palette: TypeStyle[] | null = null;
	private styleEl!: HTMLStyleElement;
	private decorator!: ExplorerDecorator;
	private noteClass!: NoteClassApplier;
	/** Last-known `reviewed` value per file — lets us catch the false→true flip
	 * (the user checking the box) and offer to close the tab. Seeded on file-open so
	 * the very first flip is caught (an unseen file has no prior `false` to compare). */
	private reviewedState = new Map<string, boolean>();
	/** True while we're opening the For You view — keeps the new-tab→For-You handler
	 * from racing the brief empty leaf `getLeaf("tab")` creates for it. */
	private openingNow = false;

	async onload(): Promise<void> {
		await this.loadSettings();
		await this.applyKitFacts();
		this.refreshPalette();

		this.styleEl = document.head.createEl("style");
		this.styleEl.id = "nkui-dynamic";
		this.applyDynamicCss();
		this.applyBodyClasses();

		this.decorator = new ExplorerDecorator(this);
		this.noteClass = new NoteClassApplier(this);

		this.registerView(NOW_VIEW_TYPE, (leaf) => new NowView(leaf, this));
		this.addRibbonIcon("sun", "Open For You view", () => this.activateNowView());
		this.addCommand({
			id: "open-now-view",
			name: "Open For You view",
			callback: () => this.activateNowView(),
		});

		this.addSettingTab(new NoteKitUiSettingTab(this.app, this));

		// Register the For You view as a hover-link source so a right-click (desktop)
		// or long-press (mobile) on a row can summon the core Page Preview popover
		// (see nowView.attachPreview). No-op if Page Preview is disabled.
		this.registerHoverLinkSource(NOW_VIEW_TYPE, { display: "For You", defaultMod: false });

		// Turn a new empty main-area tab into the For You page (a Home button).
		this.registerEvent(
			this.app.workspace.on("active-leaf-change", (leaf) => {
				this.maybeReplaceEmpty(leaf);
				this.dedupeNowView();
			})
		);

		// One tab per document: opening a file that is already open in another
		// main-area tab focuses the existing tab and closes the duplicate. Also seed
		// the reviewed-state map for the opened file, so a later false→true flip is
		// recognised as a flip (not a first sighting) and the close-tab offer fires.
		this.registerEvent(
			this.app.workspace.on("file-open", (file) => {
				this.dedupeTabsFor(file?.path);
				if (file) this.seedReviewed(file);
			})
		);

		// The palette is read from the app's CSS, so it follows the theme: re-derive
		// whenever the CSS changes (theme switch, snippet edit) and repaint if the
		// colors actually moved.
		this.registerEvent(this.app.workspace.on("css-change", () => this.onCssChange()));

		// When a note's `reviewed` box is checked (false → true), offer to close the
		// tab — the reviewed box becomes a "close tab?" button that fades on a timer.
		this.registerEvent(this.app.metadataCache.on("changed", (file) => this.maybeOfferClose(file)));

		// Fold children with parent: when a folder is collapsed by a click, collapse
		// its descendant folders too, so re-opening it shows them folded.
		this.registerDomEvent(document, "click", (ev) => {
			if (!this.settings.foldChildrenWithParent) return;
			const title = (ev.target as HTMLElement | null)?.closest?.(".nav-folder-title") as HTMLElement | null;
			const path = title?.getAttribute("data-path");
			if (path) window.setTimeout(() => this.foldDescendants(path), 0);
		});

		this.app.workspace.onLayoutReady(() => {
			// Theme CSS is reliably in by now — derive (or re-derive) and paint.
			this.onCssChange();
			// Initial graph sync is unconditional: onCssChange only writes on a
			// palette delta, and a normal load derives the same palette as last time.
			if (this.settings.syncGraphColors) void this.applyGraphColors();
			this.decorator.start();
			this.noteClass.start();
			if (this.settings.nowOpenOnStartup && this.app.workspace.getLeavesOfType(NOW_VIEW_TYPE).length === 0) {
				this.activateNowView();
			}
			// Collapse any duplicate For You tabs a restored workspace brought back.
			this.dedupeNowView();
			// The left dock defaults to the file explorer — never strand a session
			// in the tag pane (the explorer is the kit's primary navigation).
			const explorer = this.app.workspace.getLeavesOfType("file-explorer")[0];
			if (explorer) this.app.workspace.revealLeaf(explorer);
		});
	}

	onunload(): void {
		this.decorator?.stop();
		this.noteClass?.stop();
		this.styleEl?.remove();
		document.body.removeClass("nkui-calm-reading");
		document.body.removeClass("nkui-type-tint");
		document.body.removeClass("nkui-anim");
	}

	private applyBodyClasses(): void {
		document.body.toggleClass("nkui-calm-reading", this.settings.calmReading);
		document.body.toggleClass("nkui-type-tint", this.settings.typeTint);
		document.body.toggleClass("nkui-anim", this.settings.animations);
	}

	/** Close a just-opened duplicate of a file already open in another main tab. */
	private dedupeTabsFor(path: string | undefined): void {
		if (!this.settings.dedupeTabs || !path) return;
		const active = this.app.workspace.activeLeaf;
		if (!active || active.getRoot() !== this.app.workspace.rootSplit) return;
		const dupes = this.app.workspace
			.getLeavesOfType("markdown")
			.filter(
				(l) =>
					l !== active &&
					l.getRoot() === this.app.workspace.rootSplit &&
					(l.view as { file?: { path?: string } }).file?.path === path
			);
		if (!dupes.length) return;
		// Keep the pre-existing tab; close the duplicate that just opened.
		const keep = dupes[0];
		active.detach();
		this.app.workspace.setActiveLeaf(keep, { focus: true });
	}

	private maybeReplaceEmpty(leaf: WorkspaceLeaf | null): void {
		if (!leaf || this.openingNow) return;
		if (leaf.view?.getViewType() !== "empty") return;
		// Main area only — leave empty side-panel leaves alone.
		if (leaf.getRoot() !== this.app.workspace.rootSplit) return;
		if (!this.settings.nowReplaceNewTab) return;
		// One For You at a time: if one is already open, focus it and drop this
		// empty tab instead of opening a second — the Home button reuses its page.
		if (this.settings.dedupeTabs) {
			const existing = this.app.workspace
				.getLeavesOfType(NOW_VIEW_TYPE)
				.find((l) => l.getRoot() === this.app.workspace.rootSplit);
			if (existing) {
				leaf.detach();
				this.app.workspace.revealLeaf(existing);
				this.app.workspace.setActiveLeaf(existing, { focus: true });
				return;
			}
		}
		void leaf.setViewState({ type: NOW_VIEW_TYPE });
	}

	/** Record a file's current `reviewed` value without offering to close — called on
	 * file-open so the next change is compared against a known prior state. Only seeds
	 * an unseen file, so it never clobbers a flip the change handler is about to read. */
	private seedReviewed(file: TFile): void {
		if (this.reviewedState.has(file.path)) return;
		const field = this.settings.reviewedField;
		const fm = this.app.metadataCache.getFileCache(file)?.frontmatter;
		this.reviewedState.set(file.path, !!fm && (fm[field] === true || fm[field] === "true"));
	}

	/** On a `reviewed` false→true flip (the user checking the box), offer to close
	 * the file's tab: its reviewed checkbox is swapped for a "close tab?" button that
	 * fades on a short timer (the hold-fill visual), then the checkbox returns. */
	private maybeOfferClose(file: TFile): void {
		const field = this.settings.reviewedField;
		const fm = this.app.metadataCache.getFileCache(file)?.frontmatter;
		const reviewed = !!fm && (fm[field] === true || fm[field] === "true");
		const prev = this.reviewedState.get(file.path);
		this.reviewedState.set(file.path, reviewed);
		// Only on a real false → true flip. `prev === undefined` (an unseen file) is
		// NOT a flip — seedReviewed on file-open ensures a tracked false precedes it.
		if (!reviewed || prev !== false) return;
		for (const leaf of this.app.workspace.getLeavesOfType("markdown")) {
			const view = leaf.view;
			if (view instanceof MarkdownView && view.file?.path === file.path) {
				// Show after ~1/4s, once Obsidian has re-rendered the property block.
				window.setTimeout(() => this.injectCloseButton(view, leaf), 250);
			}
		}
	}

	private injectCloseButton(view: MarkdownView, leaf: WorkspaceLeaf): void {
		const prop = view.containerEl.querySelector<HTMLElement>(
			'.metadata-property[data-property-key="reviewed"]'
		);
		if (!prop || prop.querySelector(".nkui-close-offer")) return;
		const value = prop.querySelector<HTMLElement>(".metadata-property-value");
		if (!value) return;
		value.style.display = "none";
		const btn = prop.createEl("button", { cls: "nkui-close-offer", text: "close tab?" });
		let timer: number | undefined;
		const restore = (): void => {
			if (timer) window.clearTimeout(timer);
			btn.remove();
			value.style.display = ""; // the checked reviewed box returns
		};
		// The fill depletes over ~1.15s (the hold animation, run in reverse); when it
		// empties the offer expires. Hovering pauses it — the button holds full and the
		// countdown restarts on leave — so it's reachable as long as you're over it.
		const COUNT = 1167;
		const arm = (): void => {
			timer = window.setTimeout(restore, COUNT);
		};
		btn.addEventListener("click", (ev) => {
			ev.preventDefault();
			ev.stopPropagation();
			leaf.detach();
		});
		btn.addEventListener("pointerenter", () => {
			if (timer) window.clearTimeout(timer);
			btn.removeClass("is-counting");
		});
		btn.addEventListener("pointerleave", () => {
			btn.addClass("is-counting");
			arm();
		});
		window.requestAnimationFrame(() => btn.addClass("is-counting"));
		arm();
	}

	/** Collapse every descendant folder of a folder that was just collapsed (using
	 * the file-explorer's fileItems map). No-op when the click expanded it. */
	private foldDescendants(path: string): void {
		const view = this.app.workspace.getLeavesOfType("file-explorer")[0]?.view as unknown as {
			fileItems?: Record<
				string,
				{ collapsed?: boolean; collapsible?: boolean; setCollapsed?: (c: boolean) => void }
			>;
		};
		const items = view?.fileItems;
		if (!items) return;
		const self = items[path];
		if (!self || !self.collapsed) return; // only when the click just collapsed it
		for (const p of Object.keys(items)) {
			if (p === path || !p.startsWith(path + "/")) continue;
			const it = items[p];
			if (it?.collapsible && !it.collapsed && typeof it.setCollapsed === "function") {
				it.setCollapsed(true);
			}
		}
	}

	/** Keep a single For You tab in the main area — detach any extras (e.g. two
	 * restored from a saved workspace), keeping the active one. Mirrors dedupeTabs
	 * for the custom view, which is keyed on view type, not a file path. */
	private dedupeNowView(): void {
		if (!this.settings.dedupeTabs) return;
		const leaves = this.app.workspace
			.getLeavesOfType(NOW_VIEW_TYPE)
			.filter((l) => l.getRoot() === this.app.workspace.rootSplit);
		if (leaves.length <= 1) return;
		const active = this.app.workspace.activeLeaf;
		const keep = active && leaves.includes(active) ? active : leaves[0];
		for (const l of leaves) if (l !== keep) l.detach();
		this.app.workspace.revealLeaf(keep);
	}

	async activateNowView(): Promise<void> {
		const { workspace } = this.app;
		let leaf = workspace.getLeavesOfType(NOW_VIEW_TYPE)[0];
		if (!leaf) {
			this.openingNow = true;
			try {
				leaf = workspace.getLeaf("tab");
				await leaf.setViewState({ type: NOW_VIEW_TYPE, active: true });
			} finally {
				this.openingNow = false;
			}
		}
		workspace.revealLeaf(leaf);
	}

	applyDynamicCss(): void {
		this.styleEl.textContent = buildDynamicCss(this.settings, this.typeStyles());
	}

	/** The type colours in force: the theme-derived palette when on, else the
	 * manual list. Every consumer (css, explorer, For You, graph) reads this. */
	typeStyles(): TypeStyle[] {
		return this.settings.themePalette && this.palette ? this.palette : this.settings.typeStyles;
	}

	/** (Re)derive the theme palette for the kit's type vocabulary. */
	refreshPalette(): void {
		if (!this.settings.themePalette) {
			this.palette = null;
			return;
		}
		const types = this.kitFacts?.types?.length
			? this.kitFacts.types
			: this.settings.typeStyles.map((t) => t.type);
		this.palette = deriveThemePalette(types) ?? this.palette;
	}

	private onCssChange(): void {
		const before = JSON.stringify(this.palette);
		this.refreshPalette();
		if (JSON.stringify(this.palette) === before) return;
		this.applyDynamicCss();
		this.refreshNowViews();
		if (this.settings.syncGraphColors) void syncGraphColors(this.app, this.typeStyles());
	}

	/** Write the current type colours into the graph view's colour groups. */
	async applyGraphColors(): Promise<void> {
		await syncGraphColors(this.app, this.typeStyles());
	}

	private refreshNowViews(): void {
		for (const leaf of this.app.workspace.getLeavesOfType(NOW_VIEW_TYPE)) {
			const view = leaf.view;
			if (view instanceof NowView) view.refresh();
		}
	}

	async loadSettings(): Promise<void> {
		this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
		// Migrate rules saved before `size` existed so the field is always present.
		for (const p of this.settings.prefixStyles) {
			if (typeof p.size !== "number") p.size = 1;
		}
	}

	/**
	 * Kit facts override their stored counterparts in memory on every load — the
	 * kit's CONFIG.md stays the single home for vault structure and the plugin
	 * can't drift from it. Presentation (colors, weights, toggles) stays the
	 * plugin's own. With no CONFIG in the vault (standalone use) or the sync
	 * toggle off, the manual settings apply untouched.
	 */
	async applyKitFacts(): Promise<void> {
		if (!this.settings.useKitConfig) {
			this.kitFacts = null;
			return;
		}
		this.kitFacts = await readKitFacts(this.app);
		if (!this.kitFacts) return;
		const f = this.kitFacts;
		const s = this.settings;
		s.typeField = "type";
		s.reviewedField = "reviewed";
		s.inboxFolders = [f.inboxLiteral];
		s.nowQueueFolders = [f.outboxLiteral];
		s.userQueuePath = f.userQueuePath;
		s.machineQueuePath = f.machineQueuePath;
		// Prefix tokens come from CONFIG § Numbering; their styling stays user-set.
		// A kit prefix with no styling row gets a neutral one; extra rows are kept.
		for (const p of f.prefixes) {
			if (!s.prefixStyles.some((r) => r.prefix === p)) {
				s.prefixStyles.push({ prefix: p, weight: 400, size: 1, opacity: 1, color: "" });
			}
		}
	}

	async saveSettings(): Promise<void> {
		await this.saveData(this.settings);
		this.refreshPalette();
		this.applyDynamicCss();
		this.applyBodyClasses();
		this.decorator?.refresh();
		this.noteClass?.refresh();
		this.refreshNowViews();
		if (this.settings.syncGraphColors) await this.applyGraphColors();
	}
}
