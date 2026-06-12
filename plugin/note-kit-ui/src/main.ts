import { MarkdownView, Notice, Platform, Plugin, TFile, WorkspaceLeaf } from "obsidian";
import { DEFAULT_SETTINGS, NoteKitUiSettings, NoteKitUiSettingTab, TypeStyle } from "./settings";
import { buildDynamicCss } from "./css";
import { ExplorerDecorator } from "./decorator";
import { NoteClassApplier } from "./noteClass";
import { NowView, NOW_VIEW_TYPE, NOW_SIDE_VIEW_TYPE } from "./nowView";
import { QueueView, QUEUE_VIEW_TYPE, rawEscapes } from "./queueView";
import { closeOfferMs, configureHolds } from "./holds";
import { KitFacts, readKitFacts } from "./kitConfig";
import { autoColor, deriveThemePalette } from "./palette";
import { syncGraphColors } from "./graph";

/** The default palette shipped BEFORE 0.4.49 (recovered from git, 0.4.46) —
 * compared once against the saved palette: an exact match means the user never
 * customised it, so the one-time migration may advance it to the current
 * defaults (see migratePalette). */
const PRE_049_TYPE_STYLES: TypeStyle[] = [
	{ type: "project", color: "#e42148" },
	{ type: "area", color: "#ff9933" },
	{ type: "reference", color: "#00b347" },
	{ type: "research", color: "#3b82f6" },
	{ type: "plan", color: "#a855f7" },
	{ type: "session", color: "#4d61ec" },
	{ type: "journal", color: "#f03da2" },
	{ type: "idea", color: "#f0dc00" },
	{ type: "snippet", color: "#00b9d1" },
	{ type: "source", color: "#5c6bc0" },
	{ type: "index", color: "#ffffff" },
	{ type: "note", color: "#9aa1c1" },
	{ type: "voice", color: "#e8893a" },
	{ type: "design", color: "#ffbb55" },
	{ type: "format", color: "#ffd685" },
	{ type: "addendum", color: "#ffc894" },
	{ type: "log", color: "#454545" },
	{ type: "revision", color: "#ab6969" },
];

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
	/** Session memory of each file's last view mode (source = live preview,
	 * preview = reading) — recorded when a markdown view is left, restored when
	 * its file reopens, so the mode persists across close/reopen. */
	private viewModes = new Map<string, string>();
	private lastActiveMd: MarkdownView | null = null;
	/** Markdown views already carrying the clean-queue-view header action. */
	private cleanViewActioned = new WeakSet<MarkdownView>();
	/** Sidebar open-state trackers for the mobile swipe discipline — only the
	 * collapsed→open TRANSITION enforces a reveal, so the user can still switch
	 * tabs inside an already-open drawer (see enforceSidebars). */
	private leftWasOpen = false;
	private rightWasOpen = false;
	/** True once onLayoutReady seeded the trackers from the real drawer state —
	 * before that, a startup layout-change with a restored-open drawer would
	 * read as a fresh swipe and force-switch its tab. */
	private sidebarsSeeded = false;

	async onload(): Promise<void> {
		await this.loadSettings();
		await this.migratePalette();
		await this.applyKitFacts();
		this.refreshPalette();
		configureHolds(this.settings.holdMs);

		this.styleEl = document.head.createEl("style");
		this.styleEl.id = "nkui-dynamic";
		this.applyDynamicCss();
		this.applyBodyClasses();

		this.decorator = new ExplorerDecorator(this);
		this.noteClass = new NoteClassApplier(this);

		this.registerView(NOW_VIEW_TYPE, (leaf) => new NowView(leaf, this));
		this.registerView(NOW_SIDE_VIEW_TYPE, (leaf) => new NowView(leaf, this, true));
		this.registerView(QUEUE_VIEW_TYPE, (leaf) => new QueueView(leaf, this));
		this.addRibbonIcon("sun", "Open For You view", () => this.activateNowView());
		// `icon` so the command carries the For You sun when added to the mobile
		// toolbar — the quick way into the view on a phone.
		this.addCommand({
			id: "open-now-view",
			name: "Open For You view",
			icon: "sun",
			callback: () => this.activateNowView(),
		});
		// The way BACK into the clean queue view after "edit raw": clears the
		// session's raw-escape for the active queue file and swaps the leaf.
		this.addCommand({
			id: "open-queue-clean-view",
			name: "Reopen queue in clean view",
			icon: "list-checks",
			checkCallback: (checking) => {
				const p = this.app.workspace.getActiveFile()?.path;
				const isQueue =
					!!p && (p === this.settings.userQueuePath || p === this.settings.machineQueuePath);
				if (checking) return isQueue;
				if (!isQueue || !p) return false;
				rawEscapes.delete(p);
				const leaf = this.app.workspace.getActiveViewOfType(MarkdownView)?.leaf;
				if (leaf) void leaf.setViewState({ type: QUEUE_VIEW_TYPE, state: { file: p }, active: true });
				return true;
			},
		});

		this.addSettingTab(new NoteKitUiSettingTab(this.app, this));

		// Register the For You view as a hover-link source so a right-click (desktop)
		// or long-press (mobile) on a row can summon the core Page Preview popover
		// (see nowView.attachPreview). No-op if Page Preview is disabled.
		this.registerHoverLinkSource(NOW_VIEW_TYPE, { display: "For You", defaultMod: false });

		// Turn a new empty main-area tab into the For You page (a Home button).
		this.registerEvent(
			this.app.workspace.on("active-leaf-change", (leaf) => {
				// Leaving a markdown view records its mode, so reopening its file
				// restores live-preview/reading the way the user left it.
				if (this.lastActiveMd?.file) {
					this.viewModes.set(this.lastActiveMd.file.path, this.lastActiveMd.getMode());
				}
				this.lastActiveMd = this.app.workspace.getActiveViewOfType(MarkdownView);
				// A single new-tab press is handled by exactly one pass: when
				// maybeReplaceEmpty acts on the leaf (refocus or convert), the
				// dedupe sweep is skipped this tick so the same leaf can't be
				// replaced AND deduped at once. Genuine duplicates (a restored
				// workspace) still get swept on the next ordinary event.
				if (!this.maybeReplaceEmpty(leaf)) this.dedupeNowView();
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
				// Queue files open in the clean queue view (unless the user escaped
				// to the raw editor this session) — after the dedupe settles.
				this.maybeRouteQueue(file);
				// A raw-escaped queue editor gets a header action back to the
				// clean view (the command alone wasn't discoverable).
				this.maybeAddCleanViewAction(file);
				this.restoreViewMode(file);
			})
		);

		// Queue routing's reliable trigger: layout-change fires after a new
		// leaf's view state is committed (file-open fires too early for it).
		// The same event carries the mobile sidebar discipline: Obsidian exposes
		// no swipe API, so the drawers' collapsed→open flips are watched here.
		this.registerEvent(
			this.app.workspace.on("layout-change", () => {
				this.routeQueueLeaves();
				this.enforceSidebars();
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
			// The RIGHT sidebar's content — For You by default, or a queue (mobile:
			// swipe left opens it). Installed quietly — never focused or revealed
			// on startup.
			this.applySidebarNow();
			// Seed the drawer trackers from the real state, so a sidebar already
			// open at startup doesn't read as a fresh swipe.
			this.leftWasOpen = !(this.app.workspace.leftSplit?.collapsed ?? true);
			this.rightWasOpen = !(this.app.workspace.rightSplit?.collapsed ?? true);
			this.sidebarsSeeded = true;
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
		document.body.removeClass("nkui-solid-icons");
	}

	private applyBodyClasses(): void {
		document.body.toggleClass("nkui-calm-reading", this.settings.calmReading);
		document.body.toggleClass("nkui-type-tint", this.settings.typeTint);
		document.body.toggleClass("nkui-anim", this.settings.animations);
		document.body.toggleClass("nkui-solid-icons", this.settings.solidIcons);
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

	/** Open a queue FILE as the clean queue view: when the clean-view setting is
	 * on and the opened path is one of the two configured queues, swap the leaf's
	 * markdown view for the queue view in place. A path the user escaped to the
	 * raw editor this session (rawEscapes — the queue view's "edit raw" adds it
	 * BEFORE switching) is left alone, which also breaks the re-route loop. */
	private maybeRouteQueue(file: TFile | null): void {
		if (!file) return;
		const p = file.path;
		if (p !== this.settings.userQueuePath && p !== this.settings.machineQueuePath) return;
		// file-open fires before the markdown view has loaded its file or even
		// committed its leaf state, so don't inspect leaves here — defer to the
		// scan, which also runs on layout-change (that event fires once the
		// leaf's state is real).
		window.setTimeout(() => this.routeQueueLeaves(), 0);
	}

	/** Convert every main-area markdown leaf sitting on a queue file into the
	 * clean queue view (unless the user escaped that path to the raw editor —
	 * the queue view's "edit raw" adds it to rawEscapes BEFORE switching, which
	 * also breaks the re-route loop). Matches on the leaf's view STATE, not
	 * view.file — the state carries the path before the file loads. */
	private routeQueueLeaves(): void {
		if (!this.settings.queueCleanView) return;
		const queues = [this.settings.userQueuePath, this.settings.machineQueuePath];
		for (const leaf of this.app.workspace.getLeavesOfType("markdown")) {
			if (leaf.getRoot() !== this.app.workspace.rootSplit) continue;
			const state = leaf.getViewState().state as { file?: string } | undefined;
			const p = state?.file;
			if (!p || !queues.includes(p) || rawEscapes.has(p)) continue;
			void leaf.setViewState({ type: QUEUE_VIEW_TYPE, state: { file: p }, active: true });
		}
	}

	/** Put a "Reopen clean queue view" action in the header of a queue file's
	 * raw markdown editor — the visible way back after "edit raw" (the command
	 * palette route alone wasn't discoverable). */
	private maybeAddCleanViewAction(file: TFile | null): void {
		if (!file) return;
		if (file.path !== this.settings.userQueuePath && file.path !== this.settings.machineQueuePath) {
			return;
		}
		const v = this.app.workspace.getActiveViewOfType(MarkdownView);
		if (!v || v.file?.path !== file.path || this.cleanViewActioned.has(v)) return;
		this.cleanViewActioned.add(v);
		v.addAction("list-checks", "Reopen clean queue view", () => {
			rawEscapes.delete(file.path);
			void v.leaf.setViewState({ type: QUEUE_VIEW_TYPE, state: { file: file.path }, active: true });
		});
	}

	/** Restore the file's remembered view mode once the markdown view settles —
	 * only when it differs, so an untouched open stays on the global default. */
	private restoreViewMode(file: TFile | null): void {
		if (!file) return;
		const want = this.viewModes.get(file.path);
		if (!want) return;
		window.setTimeout(() => {
			const v = this.app.workspace.getActiveViewOfType(MarkdownView);
			if (!v || v.file?.path !== file.path || v.getMode() === want) return;
			const st = v.leaf.getViewState();
			void v.leaf.setViewState({
				...st,
				state: { ...(st.state ?? {}), mode: want },
			});
		}, 0);
	}

	/** Turn a new empty main-area tab into the For You page. Returns true when it
	 * acted on the leaf (so the caller skips the dedupe sweep this tick). */
	private maybeReplaceEmpty(leaf: WorkspaceLeaf | null): boolean {
		if (!leaf || this.openingNow) return false;
		if (leaf.view?.getViewType() !== "empty") return false;
		// Main area only — leave empty side-panel leaves alone.
		if (leaf.getRoot() !== this.app.workspace.rootSplit) return false;
		if (!this.settings.nowReplaceNewTab) return false;
		// One For You at a time: if one is already open, reuse its page instead of
		// opening a second. Order matters for a flash-free hand-off: focus the
		// existing leaf FIRST, then detach the no-longer-active empty leaf — all
		// in this same synchronous tick. Detaching the ACTIVE leaf first (the old
		// order) made Obsidian activate a neighbouring tab before our refocus
		// landed, which painted an intermediate state (the stutter). Detaching an
		// inactive leaf triggers no focus juggling, so the only paint after this
		// handler shows the final state: existing For You focused, empty tab gone.
		// This also keeps the existing view instance (scroll/fold state) and its
		// tab position, which converting the empty leaf in place would discard.
		if (this.settings.dedupeTabs) {
			const existing = this.app.workspace
				.getLeavesOfType(NOW_VIEW_TYPE)
				.find((l) => l.getRoot() === this.app.workspace.rootSplit);
			if (existing) {
				this.app.workspace.setActiveLeaf(existing, { focus: true });
				leaf.detach();
				return true;
			}
		}
		void leaf.setViewState({ type: NOW_VIEW_TYPE });
		return true;
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
		// The fill depletes over the close-offer window (the hold animation in
		// reverse, 5x the configured hold); when it empties the offer expires.
		// Hovering pauses it — the button holds full and the countdown restarts
		// on leave — so it's reachable while you're over it.
		const arm = (): void => {
			timer = window.setTimeout(restore, closeOfferMs());
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
		for (const type of [NOW_VIEW_TYPE, NOW_SIDE_VIEW_TYPE]) {
			for (const leaf of this.app.workspace.getLeavesOfType(type)) {
				const view = leaf.view;
				if (view instanceof NowView) view.refresh();
			}
		}
	}

	async loadSettings(): Promise<void> {
		this.settings = Object.assign({}, DEFAULT_SETTINGS, await this.loadData());
		// Strip retired keys a stale data.json may still carry, so the legacy
		// numeric-prefix machinery (and the removed Recent section's knob) can't
		// resurrect in memory (or be re-saved).
		const DEAD_KEYS = ["prefixStyles", "enablePrefixStyling", "enableHidePrefix", "dimSinkContents", "nowRecentCount"];
		for (const k of DEAD_KEYS) {
			delete (this.settings as unknown as Record<string, unknown>)[k];
		}
	}

	/** One-time palette advance: a saved palette that exactly matches the
	 * pre-0.4.49 shipped defaults was never customised, so it moves to the
	 * current defaults (with a Notice). A customised palette is left alone.
	 * Either way the flag is set so this never runs again. */
	private async migratePalette(): Promise<void> {
		if (this.settings.paletteMigrated) return;
		const cur = this.settings.typeStyles;
		const untouched =
			cur.length === PRE_049_TYPE_STYLES.length &&
			PRE_049_TYPE_STYLES.every(
				(o, i) => cur[i]?.type === o.type && (cur[i]?.color ?? "").toLowerCase() === o.color
			);
		if (untouched) {
			this.settings.typeStyles = DEFAULT_SETTINGS.typeStyles.map((r) => ({ ...r }));
			new Notice("Note Kit UI: default colours updated — Reset in settings restores them anytime");
		}
		this.settings.paletteMigrated = true;
		await this.saveData(this.settings);
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
		// CONFIG § Numbering prefixes are still parsed (kitConfig) but no longer
		// consumed — the legacy numeric-prefix styling was retired.
		// A type CONFIG names but the colour list doesn't gets a row on every
		// sync (load and the Re-sync button), so the kit vocabulary is always
		// fully colourable: the shipped default where one exists, else a
		// deterministic spaced hue. Existing rows are never overwritten.
		for (const [i, t] of f.types.entries()) {
			if (s.typeStyles.some((r) => r.type === t)) continue;
			const shipped = DEFAULT_SETTINGS.typeStyles.find((r) => r.type === t);
			s.typeStyles.push(shipped ? { ...shipped } : { type: t, color: autoColor(i) });
		}
	}

	/** Reconcile the right-sidebar leaf with the sidebar settings: `sidebarNow`
	 * turns it on/off, `sidebarContent` picks what it hosts — the For You page
	 * (default), the user queue, or the machine queue. The chosen view installs
	 * quietly (never focused or revealed); the others detach. Idempotent — safe
	 * on every settings save. */
	private applySidebarNow(): void {
		const ws = this.app.workspace;
		const s = this.settings;
		// The side For You exists ONLY as a sidebar view, so it is managed
		// unfiltered — a strict `getRoot() === rightSplit` test misses sidebar
		// leaves on mobile (the drawer reports a different root), reads the
		// sidebar as empty, and installs a duplicate on every settings save.
		const sideNow = ws.getLeavesOfType(NOW_SIDE_VIEW_TYPE);
		// Queues DO legitimately live in the main area (the routed queue view) —
		// only sidebar-resident ones are managed. inSidebar() is root-tolerant.
		const sideQueues = ws.getLeavesOfType(QUEUE_VIEW_TYPE).filter((l) => this.inSidebar(l));
		if (!s.sidebarNow) {
			for (const leaf of [...sideNow, ...sideQueues]) leaf.detach();
			return;
		}
		if (s.sidebarContent === "for-you") {
			for (const leaf of sideQueues) leaf.detach();
			// Keep exactly one — and sweep any duplicates an earlier
			// root-mismatch bug piled up.
			for (const leaf of sideNow.slice(1)) leaf.detach();
			if (sideNow.length > 0) return;
			const side = ws.getRightLeaf(false);
			if (side) void side.setViewState({ type: NOW_SIDE_VIEW_TYPE, active: false });
			return;
		}
		const path = this.sidebarQueuePath();
		for (const leaf of sideNow) leaf.detach();
		let installed = false;
		for (const leaf of sideQueues) {
			const file = (leaf.getViewState().state as { file?: string } | undefined)?.file;
			// Keep one leaf already on the right queue; detach the other queue
			// (the setting changed) and any duplicate.
			if (!installed && file === path) installed = true;
			else leaf.detach();
		}
		if (!installed) {
			const side = ws.getRightLeaf(false);
			if (side) void side.setViewState({ type: QUEUE_VIEW_TYPE, state: { file: path }, active: false });
		}
	}

	/** The queue file the sidebar hosts when `sidebarContent` names a queue. */
	private sidebarQueuePath(): string {
		return this.settings.sidebarContent === "user-queue"
			? this.settings.userQueuePath
			: this.settings.machineQueuePath;
	}

	/** A leaf living in a side dock. Desktop popout windows are excluded by the
	 * split test; on mobile the drawers can report a root that is neither
	 * rootSplit nor the split objects, so anything outside the main area counts
	 * (mobile has no popouts). */
	inSidebar(leaf: WorkspaceLeaf): boolean {
		const ws = this.app.workspace;
		const root = leaf.getRoot();
		if (Platform.isMobile) return root !== ws.rootSplit;
		return root === ws.leftSplit || root === ws.rightSplit;
	}

	/** Mobile sidebar discipline. A swipe-right opens the LEFT drawer: it must
	 * always land on the file pane, never a plugin view. A swipe-left opens the
	 * RIGHT drawer: it lands on the configured sidebar content, and a queue
	 * there re-arms its hold-to-unlock gate. Obsidian gives no swipe event, so
	 * the collapsed→open flip of each split (observed via layout-change) stands
	 * in for the gesture; only the TRANSITION acts, so switching tabs inside an
	 * already-open drawer stays free. */
	private enforceSidebars(): void {
		if (!Platform.isMobile || !this.sidebarsSeeded) return;
		const ws = this.app.workspace;
		const leftOpen = !(ws.leftSplit?.collapsed ?? true);
		const rightOpen = !(ws.rightSplit?.collapsed ?? true);
		// Update the trackers BEFORE acting: revealLeaf fires layout-change
		// synchronously, and a re-entry reading the stale "was closed" state
		// would act twice on one swipe.
		const leftFlipped = leftOpen && !this.leftWasOpen;
		const rightFlipped = rightOpen && !this.rightWasOpen;
		this.leftWasOpen = leftOpen;
		this.rightWasOpen = rightOpen;
		if (leftFlipped) {
			const explorer = ws.getLeavesOfType("file-explorer")[0];
			if (explorer) void ws.revealLeaf(explorer);
		}
		if (rightFlipped) this.revealSidebarContent();
	}

	/** Land the just-opened right drawer on the configured content. A queue is
	 * a write surface for the agents, so its view re-locks behind the
	 * "are you sure?" hold-to-unlock before it's revealed — a stray swipe can
	 * never land a tap on a live checkbox. */
	private revealSidebarContent(): void {
		if (!this.settings.sidebarNow) return;
		const ws = this.app.workspace;
		if (this.settings.sidebarContent === "for-you") {
			const leaf = ws.getLeavesOfType(NOW_SIDE_VIEW_TYPE)[0];
			if (leaf) void ws.revealLeaf(leaf);
			return;
		}
		const path = this.sidebarQueuePath();
		for (const leaf of ws.getLeavesOfType(QUEUE_VIEW_TYPE)) {
			if (!this.inSidebar(leaf)) continue;
			const file = (leaf.getViewState().state as { file?: string } | undefined)?.file;
			if (file !== path) continue;
			if (leaf.view instanceof QueueView) leaf.view.lockForReveal();
			void ws.revealLeaf(leaf);
			return;
		}
	}

	async saveSettings(): Promise<void> {
		await this.saveData(this.settings);
		configureHolds(this.settings.holdMs);
		this.refreshPalette();
		this.applyDynamicCss();
		this.applyBodyClasses();
		this.decorator?.refresh();
		this.noteClass?.refresh();
		this.refreshNowViews();
		this.applySidebarNow();
		if (this.settings.syncGraphColors) await this.applyGraphColors();
	}
}
