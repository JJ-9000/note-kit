import { MarkdownPostProcessorContext, MarkdownView, Notice, Platform, Plugin, TFile, WorkspaceLeaf, normalizePath, setIcon } from "obsidian";
import { DEFAULT_SETTINGS, NoteKitUiSettings, NoteKitUiSettingTab, TypeStyle } from "./settings";
import { buildDynamicCss } from "./css";
import { ExplorerDecorator } from "./decorator";
import { NoteClassApplier } from "./noteClass";
import { NowView, NOW_VIEW_TYPE, NOW_SIDE_VIEW_TYPE } from "./nowView";
import { QueueView, QUEUE_VIEW_TYPE, rawEscapes } from "./queueView";
import { ConfigView, CONFIG_VIEW_TYPE } from "./configView";
import { closeOfferMs, configureHolds } from "./holds";
import { KitFacts, readKitFacts } from "./kitConfig";
import { autoColor, deriveThemePalette } from "./palette";
import { syncGraphColors, openGraphForTag } from "./graph";
import { SkimProcessor } from "./skim";

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
	/** Reading-view skim post-processor (fold-by-keyword, first/last). */
	private skim!: SkimProcessor;
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
	/** The minimalist-mode exit button mounted in the file-explorer pane —
	 * minimalist hides the app chrome (stylesheet), so this is the one way
	 * back out. Null while minimalist is off or the explorer isn't there yet. */
	private minimalExitEl: HTMLElement | null = null;

	async onload(): Promise<void> {
		await this.loadSettings();
		await this.migratePalette();
		await this.applyKitFacts();
		this.refreshPalette();
		configureHolds(this.settings.holdMs);

		this.styleEl = document.head.createEl("style");
		this.styleEl.id = "nkui-dynamic";
		this.applyDynamicCss();
		this.injectFont();
		this.applyBodyClasses();

		this.decorator = new ExplorerDecorator(this);
		this.noteClass = new NoteClassApplier(this);
		this.skim = new SkimProcessor(this);

		this.registerView(NOW_VIEW_TYPE, (leaf) => new NowView(leaf, this));
		this.registerView(NOW_SIDE_VIEW_TYPE, (leaf) => new NowView(leaf, this, true));
		this.registerView(QUEUE_VIEW_TYPE, (leaf) => new QueueView(leaf, this));
		this.registerView(CONFIG_VIEW_TYPE, (leaf) => new ConfigView(leaf, this));
		this.addRibbonIcon("sun", "Open For You view", () => this.activateNowView());
		// CONFIG-editor ribbon — only when the feature is enabled, so an install
		// that doesn't want it carries no extra ribbon icon.
		if (this.settings.configEditor) {
			this.addRibbonIcon("sliders-horizontal", "Open CONFIG editor", () =>
				void this.openConfigView()
			);
		}
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
		// Place-anywhere queue views (the sidebar philosophy: the plugin never
		// forces a pane — the user opens any of the three views wherever they
		// like and Obsidian's own workspace persistence keeps it). Icons, so the
		// commands carry their glyphs on the mobile toolbar like the For You one.
		this.addCommand({
			id: "open-user-queue-view",
			name: "Open user queue (clean view)",
			icon: "list-todo",
			callback: () => void this.openQueueView(this.settings.userQueuePath),
		});
		this.addCommand({
			id: "open-machine-queue-view",
			name: "Open machine queue (clean view)",
			icon: "list-checks",
			callback: () => void this.openQueueView(this.settings.machineQueuePath),
		});
		// CONFIG editor — the schema-driven editable view of the kit's CONFIG.md
		// tables (read-only until held-to-unlock; saves archive-first + shape-check).
		// Gated on the setting so an install that doesn't want it stays clean. Icon
		// so it carries its glyph on the mobile toolbar like the other view commands.
		this.addCommand({
			id: "open-config-editor-view",
			name: "Open CONFIG editor",
			icon: "sliders-horizontal",
			checkCallback: (checking) => {
				if (checking) return this.settings.configEditor;
				if (!this.settings.configEditor) return false;
				void this.openConfigView();
				return true;
			},
		});

		// Reviewed header — a fixed header row surfacing the `reviewed` frontmatter
		// property at the top of every note in reading view (§ Y item 9). Runs in the
		// post-processor so it composes with Obsidian's own rendering without touching
		// the file. Fires on every section render; the first section within the sizer
		// injects the header once, subsequent sections no-op.
		this.registerMarkdownPostProcessor((el, ctx) => this.injectReviewedHeader(el, ctx));

		// Skim mode — a second reading-view post-processor (same per-section
		// composition pattern). Cheap and idempotent: it coalesces to one fold
		// pass per render and no-ops when skimMode is off or minimize-completed
		// (that mode is pure CSS via the body class).
		this.registerMarkdownPostProcessor((el, ctx) => this.skim.process(el, ctx));

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
				if (!this.maybeReplaceEmpty(leaf)) {
					this.dedupeNowView();
					this.dedupeQueueViews();
				}
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
				// Note-open flash — a type-coloured overlay wipes the black-tab gap
				// that appears before a file renders (§ V contract, § Y item 5).
				this.injectFlash(file);
			})
		);

		// Queue routing's reliable trigger: layout-change fires after a new
		// leaf's view state is committed (file-open fires too early for it).
		this.registerEvent(
			this.app.workspace.on("layout-change", () => {
				this.routeQueueLeaves();
				// Re-mount the minimalist exit if the explorer leaf was rebuilt
				// (idempotent — a still-connected button is left alone).
				this.applyMinimalExit();
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

		// Tag click → graph view filtered to that tag. Capture phase so we run
		// before Obsidian's own tag-search handler, intercepting the click only
		// when the feature is on (otherwise Obsidian's default behaviour stands).
		this.registerDomEvent(
			document,
			"click",
			(ev) => this.maybeOpenGraphForTag(ev),
			{ capture: true }
		);

		this.app.workspace.onLayoutReady(() => {
			// Theme CSS is reliably in by now — derive (or re-derive) and paint.
			this.onCssChange();
			// Initial graph sync is unconditional: onCssChange only writes on a
			// palette delta, and a normal load derives the same palette as last time.
			if (this.settings.syncGraphColors) void this.applyGraphColors();
			this.decorator.start();
			this.noteClass.start();
			// One-time layout seed; after it the panes behave like normal Obsidian
			// panes — nothing is forced or reinstalled (seedLayout).
			this.seedLayout();
			// Honour "show in sidebar" OFF (detach the plugin's side leaves);
			// otherwise leave the user's arrangement alone.
			this.applySidebarNow();
			// Collapse any duplicate For You / clean-queue tabs a restored
			// workspace brought back (queue views are keyed per file).
			this.dedupeNowView();
			this.dedupeQueueViews();
			// The right dock opens to the For You page ONLY (the user's default):
			// one For You there, the restore's re-piled extras detached. Replaces
			// the old "open For You in the main area" startup behaviour.
			if (this.settings.nowOpenOnStartup) this.ensureRightForYouOnly();
		});
	}

	onunload(): void {
		this.decorator?.stop();
		this.noteClass?.stop();
		this.skim?.stop();
		this.styleEl?.remove();
		this.minimalExitEl?.remove();
		this.minimalExitEl = null;
		document.body.removeClass("nkui-calm-reading");
		document.body.removeClass("nkui-type-tint");
		document.body.removeClass("nkui-anim");
		document.body.removeClass("nkui-solid-icons");
		document.body.removeClass("nkui-large-mouths");
		document.body.removeClass("nkui-rounded");
		document.body.removeClass("nkui-minimal");
		document.body.removeClass("nkui-skim-min-done");
		document.body.removeClass("nkui-skim-condense");
		document.body.removeClass("nkui-skim-tier-readable");
		document.body.removeClass("nkui-skim-tier-faint");
		document.body.removeClass("nkui-skim-tier-outline");
		document.body.removeClass("nkui-skim");
	}

	/** Declare the bundled Inter face (the public San-Francisco alternative) as
	 * font-family 'Inter', pointing at the woff2 shipped in the plugin's fonts/
	 * folder via a vault resource URL — no base64 bloat. The variable font covers
	 * the whole 100–900 weight axis, so the iOS weight scale (400/500/600/700)
	 * all resolves to real faces. Injected unconditionally; the nkui-font body
	 * class is what actually routes the interface/text fonts to it. */
	private injectFont(): void {
		if (!this.manifest.dir) return;
		const path = normalizePath(`${this.manifest.dir}/fonts/InterVariable.woff2`);
		const url = this.app.vault.adapter.getResourcePath(path);
		const el = document.head.createEl("style", { attr: { id: "nkui-font-face" } });
		el.textContent =
			`@font-face{font-family:'Inter';font-weight:100 900;font-style:normal;` +
			`font-display:swap;src:url("${url}") format("woff2");}`;
		this.register(() => el.remove());
	}

	private applyBodyClasses(): void {
		document.body.toggleClass("nkui-calm-reading", this.settings.calmReading);
		document.body.toggleClass("nkui-type-tint", this.settings.typeTint);
		document.body.toggleClass("nkui-anim", this.settings.animations);
		document.body.toggleClass("nkui-solid-icons", this.settings.solidIcons);
		document.body.toggleClass("nkui-large-mouths", this.settings.largeMouths);
		document.body.toggleClass("nkui-rounded", this.settings.roundedCorners);
		document.body.toggleClass("nkui-minimal", this.settings.minimalistMode);
		// iPhone-style interface font (bundled Inter) — the @font-face is always
		// injected; this class is what routes --font-interface/--font-text to it.
		document.body.toggleClass("nkui-font", this.settings.interfaceFont);
		// Skim: minimize-completed is a pure-CSS dim of `- [x]` items (body class);
		// condense + the folding modes also wear nkui-skim so the chevron/heading
		// affordance shows. condense additionally wears nkui-skim-condense, which
		// drives the CSS that shrinks every non-heading block to an outline row.
		document.body.toggleClass(
			"nkui-skim-min-done",
			this.settings.skimMode === "minimize-completed"
		);
		document.body.toggleClass("nkui-skim-condense", this.settings.skimMode === "condense");
		// Condense intensity tier — one of nkui-skim-tier-readable / -faint /
		// -outline, applied only in condense mode. Always strip all three first so a
		// tier change (or a mode change away from condense) swaps cleanly with no
		// stale tier class lingering. The extra body class raises specificity over
		// the base condense rules, so each tier's opacity/margin overrides win.
		const condensing = this.settings.skimMode === "condense";
		document.body.removeClass(
			"nkui-skim-tier-readable",
			"nkui-skim-tier-faint",
			"nkui-skim-tier-outline"
		);
		if (condensing) {
			document.body.addClass("nkui-skim-tier-" + this.settings.condenseTier);
		}
		document.body.toggleClass(
			"nkui-skim",
			this.settings.skimMode === "condense" ||
				this.settings.skimMode === "fold-keywords" ||
				this.settings.skimMode === "first-last"
		);
		// Re-apply the persisted animation-speed multiplier on load (§ Z slider
		// writes it live; without this it stays at the stylesheet default until
		// the slider is next moved).
		document.body.style.setProperty("--nkui-speed-user", String(this.settings.animSpeed));
		this.applyMinimalExit();
	}

	/** Mount (or remove) the minimalist-mode exit. Minimalist hides the mobile
	 * navbar and view headers (stylesheet, body.nkui-minimal), so the way OUT
	 * is this one quiet ghost button pinned to the bottom-left of the
	 * file-explorer pane — the left drawer on mobile. Idempotent: re-run on
	 * every settings save and layout-change, re-mounting only when the
	 * explorer leaf was rebuilt. */
	private applyMinimalExit(): void {
		if (!this.settings.minimalistMode) {
			this.minimalExitEl?.remove();
			this.minimalExitEl = null;
			return;
		}
		if (this.minimalExitEl?.isConnected) return;
		this.minimalExitEl?.remove();
		this.minimalExitEl = null;
		const host = this.app.workspace.getLeavesOfType("file-explorer")[0]?.view.containerEl;
		if (!host) return; // no explorer yet — the next layout-change mounts it
		const btn = host.createEl("button", { cls: "clickable-icon nkui-minimal-exit" });
		setIcon(btn, "eye");
		btn.setAttr("aria-label", "Exit minimalist mode");
		btn.addEventListener("click", () => {
			this.settings.minimalistMode = false;
			void this.saveSettings(); // saveSettings → applyBodyClasses unmounts this
		});
		this.minimalExitEl = btn;
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

	/** Tag click → open the graph view filtered to that tag. Reads the tag text
	 * from a clicked tag element — Obsidian renders frontmatter tags as
	 * `a.tag`/`.tag` pills and inline tags as `a.tag` — strips the leading `#`,
	 * and hands off to graph.openGraphForTag. Gated behind tagClickOpensGraph;
	 * when on, the click is consumed (preventDefault + stop) so the default
	 * tag-search pane doesn't also open. Plain modifier-clicks are left alone so
	 * the user can still middle/ctrl-click a tag the normal way. */
	private maybeOpenGraphForTag(ev: MouseEvent): void {
		if (!this.settings.tagClickOpensGraph) return;
		if (ev.button !== 0 || ev.ctrlKey || ev.metaKey || ev.shiftKey || ev.altKey) return;
		const target = ev.target as HTMLElement | null;
		// Inline reading-view tags (a.tag / .tag) and live-preview tag tokens
		// (.cm-hashtag) are tag-specific. A frontmatter `.multi-select-pill` is
		// generic (any list property renders as pills), so it only counts inside
		// the tags property — a `parent`/list pill must not open the graph.
		let el = target?.closest?.("a.tag, .tag, .cm-hashtag") as HTMLElement | null;
		if (!el) {
			const pill = target?.closest?.(".multi-select-pill") as HTMLElement | null;
			const prop = pill?.closest?.(".metadata-property") as HTMLElement | null;
			const key = prop?.getAttribute("data-property-key");
			if (pill && (key === "tags" || key === "tag")) el = pill;
		}
		if (!el) return;
		// Pull the tag text: the href (#tag) is most reliable for a.tag; otherwise
		// the element's own text. Strip a leading # and any stray whitespace.
		const href = el.getAttribute("href") ?? "";
		let tag = href.startsWith("#") ? href : el.textContent ?? "";
		tag = tag.trim().replace(/^#/, "");
		// A live-preview hashtag splits into a # token and a text token; if we
		// landed on the bare # marker, the readable name is the next sibling.
		if (!tag && el.classList.contains("cm-hashtag")) {
			tag = (el.nextElementSibling?.textContent ?? "").trim().replace(/^#/, "");
		}
		if (!tag) return;
		ev.preventDefault();
		ev.stopPropagation();
		void openGraphForTag(this.app, tag);
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

	/** Keep ONE clean queue view per queue file in the main area — detach extras
	 * a re-open or a restored workspace piled up. QUEUE_VIEW_TYPE leaves are not
	 * "markdown" leaves, so dedupeTabsFor never sees them; this is their twin,
	 * keyed per queue-file path (each queue file is its own document). */
	private dedupeQueueViews(): void {
		if (!this.settings.dedupeTabs) return;
		const root = this.app.workspace.rootSplit;
		const active = this.app.workspace.activeLeaf;
		const byPath = new Map<string, WorkspaceLeaf[]>();
		for (const leaf of this.app.workspace.getLeavesOfType(QUEUE_VIEW_TYPE)) {
			if (leaf.getRoot() !== root) continue;
			const p = (leaf.getViewState().state as { file?: string } | undefined)?.file;
			if (!p) continue;
			const arr = byPath.get(p);
			if (arr) arr.push(leaf);
			else byPath.set(p, [leaf]);
		}
		for (const leaves of byPath.values()) {
			if (leaves.length <= 1) continue;
			const keep = active && leaves.includes(active) ? active : leaves[0];
			for (const l of leaves) if (l !== keep) l.detach();
		}
	}

	async activateNowView(): Promise<void> {
		const { workspace } = this.app;
		const existing = workspace.getLeavesOfType(NOW_VIEW_TYPE)[0];
		if (existing) {
			// Already open AND active → full no-op: any view-state write (or even
			// a redundant reveal) rebuilds the live page — the quick flash when
			// the For You button is pressed while the page is showing.
			if (workspace.activeLeaf === existing) return;
			// Open but not active → just focus and reveal it; never setViewState.
			workspace.setActiveLeaf(existing, { focus: true });
			workspace.revealLeaf(existing);
			return;
		}
		this.openingNow = true;
		try {
			const leaf = workspace.getLeaf("tab");
			await leaf.setViewState({ type: NOW_VIEW_TYPE, active: true });
			workspace.revealLeaf(leaf);
		} finally {
			this.openingNow = false;
		}
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
		// numeric-prefix machinery (and the removed Recent section's knob, and
		// the removed sidebar-content dropdown) can't resurrect in memory (or be
		// re-saved).
		const DEAD_KEYS = ["prefixStyles", "enablePrefixStyling", "enableHidePrefix", "dimSinkContents", "nowRecentCount", "sidebarContent"];
		for (const k of DEAD_KEYS) {
			delete (this.settings as unknown as Record<string, unknown>)[k];
		}
		// 0.4.65: the shipped `log` colour (#3b3b3b) was illegible on a dark
		// theme. Like the note-colour precedent, a saved value still equal to
		// the old default was never customised — advance it; a customised
		// colour is left alone. Idempotent, so no migration flag is needed.
		const log = this.settings.typeStyles.find((t) => t.type === "log");
		if (log && log.color.toLowerCase() === "#3b3b3b") log.color = "#9a9a9a";
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
		// The archive root is the kit's canonical sink (§ Folders `<archive>`). Derive
		// the sink term from CONFIG so a renamed archive (e.g. `Cold-Store`) is still
		// dimmed — the manual default ["Archive"] only matches a folder literally
		// named with that word, which a renamed archive is not. Union with the user's
		// own sink additions (never clobber a hand-added term); contains-word match in
		// the decorator means the bare literal is the right term.
		if (f.archiveLiteral && !s.sinkFolders.some((t) => t.toLowerCase() === f.archiveLiteral.toLowerCase())) {
			s.sinkFolders = [f.archiveLiteral, ...s.sinkFolders.filter((t) => t.toLowerCase() !== "archive")];
		}
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

	/** One-time default layout (settings.layoutSeeded): the file explorer leads
	 * the left sidebar, ONE machine-queue checklist sits in the right sidebar,
	 * and For You opens in the main area. After this the panes behave like
	 * normal Obsidian panes — the plugin never forces, reinstalls, or
	 * detaches-on-content-change again; the user places the three views by
	 * hand (the open-view commands) and workspace persistence keeps them. */
	private seedLayout(): void {
		if (this.settings.layoutSeeded) return;
		this.settings.layoutSeeded = true;
		void this.saveData(this.settings);
		const ws = this.app.workspace;
		// Left: lead with the file explorer (the kit's primary navigation).
		const explorer = ws.getLeavesOfType("file-explorer")[0];
		if (explorer) void ws.revealLeaf(explorer);
		// Right: one machine-queue checklist, installed quietly (never focused).
		const hasSideQueue = ws.getLeavesOfType(QUEUE_VIEW_TYPE).some((l) => this.inSidebar(l));
		if (!hasSideQueue && this.settings.machineQueuePath) {
			const side = ws.getRightLeaf(false);
			if (side) {
				void side.setViewState({
					type: QUEUE_VIEW_TYPE,
					state: { file: this.settings.machineQueuePath },
					active: false,
				});
			}
		}
		// Main: the For You front page.
		if (ws.getLeavesOfType(NOW_VIEW_TYPE).length === 0) void this.activateNowView();
	}

	/** Honour the one remaining sidebar setting: `sidebarNow` OFF detaches the
	 * plugin's side leaves (the For You sidebar twin and any sidebar-resident
	 * queue views). ON means hands off — no install, no reveal, no
	 * detach-on-content-change; the panes are the user's. */
	private applySidebarNow(): void {
		if (this.settings.sidebarNow) return;
		const ws = this.app.workspace;
		// The side For You exists ONLY as a sidebar view, so it is swept
		// unfiltered; queues DO legitimately live in the main area (the routed
		// queue view) — only sidebar-resident ones detach. inSidebar() is
		// root-tolerant (mobile drawers report odd roots).
		const sideNow = ws.getLeavesOfType(NOW_SIDE_VIEW_TYPE);
		const sideQueues = ws.getLeavesOfType(QUEUE_VIEW_TYPE).filter((l) => this.inSidebar(l));
		for (const leaf of [...sideNow, ...sideQueues]) leaf.detach();
	}

	/** The right dock opens to the For You page ONLY (the user's default): keep one
	 * For You in the right sidebar, detach every other right-dock leaf a restored
	 * workspace re-piled, and close any For You that landed outside the right dock.
	 * Runs on layout-ready only — the user can still open more right-dock tabs in a
	 * session; this just sets the opening state. */
	private ensureRightForYouOnly(): void {
		const ws = this.app.workspace;
		const right = ws.rightSplit;
		if (!right) return;
		const isFY = (l: WorkspaceLeaf): boolean => {
			const t = l.view?.getViewType?.();
			return t === NOW_VIEW_TYPE || t === NOW_SIDE_VIEW_TYPE;
		};
		// Close any For You that isn't in the right dock (a stray main/left one).
		ws.iterateAllLeaves((l) => { if (isFY(l) && l.getRoot() !== right) l.detach(); });
		// Right dock: keep one For You, detach the rest.
		const rightLeaves: WorkspaceLeaf[] = [];
		ws.iterateAllLeaves((l) => { if (l.getRoot() === right) rightLeaves.push(l); });
		const fy = rightLeaves.find(isFY);
		for (const l of rightLeaves) if (l !== fy) l.detach();
		// None there → open one (kept inactive so it never steals focus on load).
		if (!fy) { const rl = ws.getRightLeaf(false); if (rl) void rl.setViewState({ type: NOW_VIEW_TYPE, active: false }); }
	}

	/** Open (or reveal) a queue file's clean view — the place-anywhere command
	 * target. An existing leaf on the same file is revealed wherever the user
	 * put it; otherwise the view opens as a main-area tab, ready to be dragged
	 * to a dock. */
	private async openQueueView(path: string): Promise<void> {
		if (!path) return;
		const ws = this.app.workspace;
		const existing = ws
			.getLeavesOfType(QUEUE_VIEW_TYPE)
			.find((l) => (l.getViewState().state as { file?: string } | undefined)?.file === path);
		if (existing) {
			await ws.revealLeaf(existing);
			return;
		}
		const leaf = ws.getLeaf("tab");
		await leaf.setViewState({ type: QUEUE_VIEW_TYPE, state: { file: path }, active: true });
	}

	/** Open (or reveal) the CONFIG editor view. Like openQueueView: an existing
	 * leaf is revealed wherever the user docked it; otherwise it opens as a
	 * main-area tab, ready to be dragged to a side dock (the place-anywhere
	 * philosophy — the plugin never forces a pane). Right-panel auto-routing is
	 * deliberately NOT wired (see the design note in onload): the existing
	 * right-dock / sidebar logic is delicate, so the conservative Pass-4 choice is
	 * a command + ribbon that open the view explicitly. */
	private async openConfigView(): Promise<void> {
		const ws = this.app.workspace;
		const existing = ws.getLeavesOfType(CONFIG_VIEW_TYPE)[0];
		if (existing) {
			await ws.revealLeaf(existing);
			return;
		}
		const leaf = ws.getLeaf("tab");
		await leaf.setViewState({ type: CONFIG_VIEW_TYPE, active: true });
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

	/** Note-open flash overlay (§ V contract / § Y item 5): a type-coloured overlay
	 * child of the active leaf's container fades out as the note renders, replacing
	 * the black gap that appeared before the content loaded. Only fires for main-area
	 * markdown leaves (sidebar queue/nowView leaves get their own background); no-op
	 * when animations are off or the file has no mapped type colour. */
	private injectFlash(file: TFile | null): void {
		if (!this.settings.animations) return;
		if (!file) return;
		const leaf = this.app.workspace.activeLeaf;
		if (!leaf) return;
		// Only the main tab area — sidebar leaves already have their background.
		if (leaf.getRoot() !== this.app.workspace.rootSplit) return;
		// Only markdown views — custom views (queue, now) own their own background.
		if (leaf.view?.getViewType() !== "markdown") return;
		const fm = this.app.metadataCache.getFileCache(file)?.frontmatter;
		const typeVal = fm?.[this.settings.typeField];
		if (!typeVal) return;
		const color = this.typeStyles().find((t) => t.type === String(typeVal))?.color;
		if (!color) return;
		const container = leaf.view.containerEl;
		// Remove any lingering flash from a prior open (a very fast tab-switch).
		container.querySelectorAll(".nkui-flash").forEach((el) => el.remove());
		const flash = container.createDiv("nkui-flash");
		flash.style.setProperty("--nkui-type-color", color);
		// Add is-fading on the next frame so the transition has a starting state.
		window.requestAnimationFrame(() => {
			flash.addClass("is-fading");
			// Fallback removal: parse --nkui-flash from the computed style; default 220ms.
			// --nkui-flash is calc(var/var) — getPropertyValue returns it unresolved,
			// so read the element's own resolved transition-duration ("0.22s"/"220ms").
			const dur = getComputedStyle(flash).transitionDuration.trim();
			const ms = dur.endsWith("ms") ? parseFloat(dur) : (parseFloat(dur) || 0.22) * 1000;
			flash.addEventListener("transitionend", () => flash.remove(), { once: true });
			window.setTimeout(() => flash.remove(), ms + 50);
		});
	}

	/** Reviewed header post-processor (§ Y item 9): inject a fixed header row at
	 * the top of the reading view for any note that has a `reviewed` frontmatter
	 * field. Fires on every section render; the first section (no prior header
	 * in the sizer) injects the row once. Subsequent sections no-op.
	 * Toggling goes through Obsidian's own processFrontMatter (the existing
	 * reviewed-toggle path — no custom file writer). */
	private injectReviewedHeader(el: HTMLElement, ctx: MarkdownPostProcessorContext): void {
		// Walk up to the preview sizer — the stable container that persists across
		// section re-renders. closest(), NOT parentElement: Obsidian doesn't
		// guarantee a section el's direct parent is the sizer (it isn't — the
		// header never injected with parentElement). A detached el at process time
		// returns null here, so a later attached section does the inject.
		const sizer = el.closest(".markdown-preview-sizer");
		if (!sizer) return;
		// Reading view only — never an embed (![[note]]) or a hover-preview. Their
		// render has a sizer too, so without this the header sprouts mid-document
		// and its checkbox would write the EMBEDDED file's frontmatter.
		if (el.closest(".markdown-embed, .popover, .hover-popover")) return;
		// Only inject once per full render (idempotent on repeated section calls).
		if (sizer.querySelector(".nkui-reviewed-header")) return;
		const file = this.app.vault.getAbstractFileByPath(ctx.sourcePath);
		if (!(file instanceof TFile)) return;
		const fm = this.app.metadataCache.getFileCache(file)?.frontmatter;
		// Only notes that carry the reviewed field get the header.
		if (!fm || !(this.settings.reviewedField in fm)) return;
		const reviewed = fm[this.settings.reviewedField] === true || fm[this.settings.reviewedField] === "true";
		const header = createDiv("nkui-reviewed-header");
		header.setAttr("data-reviewed", reviewed ? "true" : "false");
		// A <label> wraps both checkbox and text: clicking either toggles the box.
		const lbl = header.createEl("label", { cls: "nkui-reviewed-label" });
		const cb = lbl.createEl("input", {
			cls: "nkui-reviewed-checkbox",
			attr: { type: "checkbox" },
		});
		cb.checked = reviewed;
		lbl.createSpan({ cls: "nkui-reviewed-text", text: reviewed ? "reviewed" : "unreviewed" });
		cb.addEventListener("change", () => {
			void this.app.fileManager.processFrontMatter(file, (frontmatter) => {
				frontmatter[this.settings.reviewedField] = cb.checked;
			});
			// Update header state attribute and label text immediately.
			header.setAttr("data-reviewed", cb.checked ? "true" : "false");
			const text = lbl.querySelector(".nkui-reviewed-text");
			if (text) text.textContent = cb.checked ? "reviewed" : "unreviewed";
		});
		// Prepend so it anchors to the top of the sizer regardless of note content.
		sizer.prepend(header);
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
