import { Plugin, WorkspaceLeaf } from "obsidian";
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

		// Turn a new empty main-area tab into the For You page (a Home button).
		this.registerEvent(
			this.app.workspace.on("active-leaf-change", (leaf) => this.maybeReplaceEmpty(leaf))
		);

		// One tab per document: opening a file that is already open in another
		// main-area tab focuses the existing tab and closes the duplicate.
		this.registerEvent(
			this.app.workspace.on("file-open", (file) => this.dedupeTabsFor(file?.path))
		);

		// The palette is read from the app's CSS, so it follows the theme: re-derive
		// whenever the CSS changes (theme switch, snippet edit) and repaint if the
		// colors actually moved.
		this.registerEvent(this.app.workspace.on("css-change", () => this.onCssChange()));

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
	}

	private applyBodyClasses(): void {
		document.body.toggleClass("nkui-calm-reading", this.settings.calmReading);
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
		if (!this.settings.nowReplaceNewTab || !leaf) return;
		if (leaf.view?.getViewType() !== "empty") return;
		// Main area only — leave empty side-panel leaves alone.
		if (leaf.getRoot() !== this.app.workspace.rootSplit) return;
		void leaf.setViewState({ type: NOW_VIEW_TYPE });
	}

	async activateNowView(): Promise<void> {
		const { workspace } = this.app;
		let leaf = workspace.getLeavesOfType(NOW_VIEW_TYPE)[0];
		if (!leaf) {
			leaf = workspace.getLeaf("tab");
			await leaf.setViewState({ type: NOW_VIEW_TYPE, active: true });
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
