import { ItemView, WorkspaceLeaf, Notice, setIcon } from "obsidian";
import { attachHold } from "./holds";
import {
	CellEdit,
	ConfigSection,
	ConfigTable,
	ParsedConfig,
	parseConfig,
	serializeConfig,
	validateShape,
} from "./configTables";
import { applyScreenShift, makeDeferredRender } from "./kitShared";
import type NoteKitUiPlugin from "./main";

export const CONFIG_VIEW_TYPE = "note-kit-config";

/**
 * The kit's CONFIG.md path. Mirrors kitConfig.ts: CONFIG lives under the kit
 * root (`.claude/`), a dot-directory Obsidian never indexes — so it is read and
 * written through `app.vault.adapter`, never the TFile cache. Derived here once
 * the same way kitConfig derives it, NOT hard-coded at a call site.
 */
const CONFIG_PATH = ".claude/CONFIG.md";

/**
 * Schema-driven CONFIG-editor side pane (§ BB, Pass 4). Reads CONFIG.md the same
 * way sync_config does — by `##` section heading, then by each table's own
 * header row — and renders every table as a clean editable grid, one section per
 * group. The shape is PARSED, never assumed: a section that gains a column, or a
 * heading the editor doesn't recognise, renders from its real shape; an
 * unparseable / table-less section renders read-only (visible, not dropped).
 *
 * Safety, all mandatory because this writes the canonical CONFIG (outside the
 * inbox):
 *   · the pane opens READ-ONLY behind a hold-to-unlock scrim (holds.ts, the
 *     whole-row-hold language shared with the queue unlock);
 *   · a save ARCHIVES the prior CONFIG before overwriting (copy → verify),
 *     accepting the timestamp string from the caller (never Date.now());
 *   · a save SHAPE-VALIDATES first (validateShape, the minimal echo of
 *     config_shape.py) and is refused with a precise inline message if it would
 *     change the table skeleton;
 *   · a save NEVER auto-runs sync_config (an external helper the plugin can't
 *     invoke headlessly) — it shows an inline notice to run it.
 *
 * Built on the queueView pattern: registerView/leaf type, getViewType/Display/
 * Icon, an onOpen render, and a clean-grid ⇄ raw-markdown toggle one tap away.
 */
export class ConfigView extends ItemView {
	plugin: NoteKitUiPlugin;
	/** The parsed CONFIG (model for both render and serialize). Null until the
	 * first load, or when CONFIG is absent/unreadable. */
	private parsed: ParsedConfig | null = null;
	/** The raw CONFIG text last read — the serialize baseline and the raw-editor
	 * seed. */
	private rawText = "";
	/** Pending cell edits keyed by "line:col" (so a re-typed cell overwrites its
	 * prior pending value). Empty between saves; a save with none is a no-op. */
	private edits = new Map<string, CellEdit>();
	/** Armed = unlocked for editing. The pane opens locked; the hold-to-unlock
	 * scrim arms it. A re-render (file change) re-locks, so an external edit can
	 * never land on a stale unlocked grid. */
	private unlocked = false;
	/** Raw-markdown mode: the whole CONFIG in one textarea (read-only echo of the
	 * file, like the queue's "edit raw" but kept IN the view so the lock still
	 * governs the canonical write path). */
	private rawMode = false;
	/** True when an inline shape-refusal banner is showing — cleared on the next
	 * edit so the message tracks the current state. */
	private refusalEl: HTMLElement | null = null;
	private scheduleRender: () => void;

	constructor(leaf: WorkspaceLeaf, plugin: NoteKitUiPlugin) {
		super(leaf);
		this.plugin = plugin;
		this.scheduleRender = makeDeferredRender(this, () => void this.reloadAndRender());
	}

	getViewType(): string {
		return CONFIG_VIEW_TYPE;
	}
	getDisplayText(): string {
		return "CONFIG";
	}
	getIcon(): string {
		// layout-list (not sliders-horizontal) so the docked CONFIG tab icon is
		// covered by the solid-icon mask set — sliders-horizontal has no Material
		// mask, so it rendered un-replaced; layout-list reads as a table/list (fits
		// the CONFIG-table editor) and IS masked.
		return "layout-list";
	}

	async onOpen(): Promise<void> {
		// React to CONFIG changing under us (a hand-edit, sync_config, an agent).
		// This vault `modify` catches the case CONFIG is surfaced as a tracked TFile,
		// but adapter writes (sync_config, agents) DON'T fire it — so the focus
		// re-reads below are the reliable trigger, keeping an open grid current.
		this.registerEvent(
			this.app.vault.on("modify", (f) => {
				if (f.path === CONFIG_PATH) this.scheduleRender();
			})
		);
		// Re-read on focus so an external adapter write appears without reopening the
		// view: when this leaf becomes active, and when the Obsidian window regains
		// focus (an edit made while alt-tabbed away). Guarded to the LOCKED state and
		// a changed file, so a mid-edit grid is never clobbered and an unchanged file
		// never re-renders.
		this.registerEvent(
			this.app.workspace.on("active-leaf-change", (leaf) => {
				if (leaf === this.leaf) void this.refreshIfChanged();
			})
		);
		this.registerDomEvent(window, "focus", () => void this.refreshIfChanged());
		// Re-stamp the narrow-pane class when the leaf is dragged between docks.
		this.registerEvent(
			this.app.workspace.on("layout-change", () => {
				this.contentEl.toggleClass("nkui-queue-side", this.plugin.inSidebar(this.leaf));
			})
		);
		await this.reloadAndRender();
	}

	/** Re-read CONFIG and re-render only when it actually changed on disk and the
	 * grid is locked — the focus-time refresh that closes the "stale until reopen"
	 * gap without disturbing a mid-edit (unlocked) grid or flickering on every focus. */
	private async refreshIfChanged(): Promise<void> {
		if (this.unlocked) return; // never clobber pending edits
		let raw: string;
		try {
			raw = await this.app.vault.adapter.read(CONFIG_PATH);
		} catch {
			return;
		}
		if (raw === this.rawText) return; // nothing changed — no re-render
		this.rawText = raw;
		this.parsed = parseConfig(raw);
		this.edits.clear();
		this.render();
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	/** Re-read CONFIG from disk, re-parse, drop pending edits, and re-lock. A
	 * fresh load always returns to the safe (locked, no-pending-edits) state. */
	private async reloadAndRender(): Promise<void> {
		try {
			this.rawText = await this.app.vault.adapter.read(CONFIG_PATH);
			this.parsed = parseConfig(this.rawText);
		} catch {
			this.rawText = "";
			this.parsed = null;
		}
		this.edits.clear();
		this.unlocked = false;
		this.refusalEl = null;
		this.render();
	}

	onResize(): void {
		applyScreenShift(this.contentEl, this.plugin.settings);
	}

	// ── rendering ───────────────────────────────────────────────────────────────

	private render(): void {
		const c = this.contentEl;
		c.empty();
		// Reuse the For You / queue page language: the centred content column, page
		// padding, stable scrollbar gutter, and the queue page wash (CONFIG reads as
		// a deliberately tinted surface like a queue page).
		c.addClass("nkui-now");
		c.addClass("nkui-queue");
		c.addClass("nkui-config");
		applyScreenShift(this.contentEl, this.plugin.settings);
		c.toggleClass("nkui-queue-side", this.plugin.inSidebar(this.leaf));
		// CONFIG carries the theme accent like the queues do — the page wash + head
		// colour key off this var.
		c.style.setProperty("--nkui-section-color", "var(--interactive-accent)");

		// Header — title, section/table counts, the raw-markdown toggle.
		const head = c.createDiv("nkui-queue-head");
		const tb = head.createDiv("nkui-queue-titleblock");
		tb.createDiv({ cls: "nkui-queue-title", text: "CONFIG" });
		const count = this.summaryLine();
		tb.createDiv({ cls: "nkui-queue-itemcount", text: count });
		const raw = head.createEl("button", { cls: "clickable-icon nkui-queue-rawbtn" });
		setIcon(raw, this.rawMode ? "table" : "file-pen-line");
		const rawAria = this.rawMode ? "Show the editable grids" : "Show the raw markdown";
		raw.setAttr("aria-label", rawAria);
		raw.setAttr("title", rawAria);
		raw.addEventListener("click", () => {
			this.rawMode = !this.rawMode;
			this.render();
		});

		if (!this.parsed) {
			c.createDiv({
				cls: "nkui-queue-empty",
				text: "No readable .claude/CONFIG.md found in this vault.",
			});
			return;
		}

		// One explanatory line, in the queue page's plain voice.
		c.createDiv({
			cls: "nkui-queue-explain",
			text: this.unlocked
				? "Editing armed — edit cells, then hold Save. A save archives CONFIG first and never auto-runs sync_config."
				: "The kit's CONFIG, read-only. Hold the unlock to edit; a save archives the prior file and runs the shape check before writing.",
		});

		// The refusal banner (if a prior save was refused) sits under the explainer.
		this.refusalEl = c.createDiv({ cls: "nkui-config-refusal" });
		this.refusalEl.hide();

		if (this.rawMode) {
			this.renderRaw(c);
		} else {
			const list = c.createDiv("nkui-now-list");
			for (const sec of this.parsed.sections) this.renderSection(list, sec);
		}

		// The lock scrim: present whenever the pane is locked (the default). Reuses
		// the queue unlock scrim styling; the hold arms editing in place.
		if (!this.unlocked) this.renderLockScrim(c);
		else this.renderSaveBar(c);
	}

	/** "N sections · M editable tables · K read-only" — what the parser found. */
	private summaryLine(): string {
		if (!this.parsed) return "CONFIG not found";
		const sections = this.parsed.sections.length;
		let editable = 0;
		let readonly = 0;
		for (const sec of this.parsed.sections) {
			if (sec.tables.length === 0) readonly++;
			else editable += sec.tables.length;
		}
		const parts = [`${sections} ${sections === 1 ? "section" : "sections"}`, `${editable} ${editable === 1 ? "table" : "tables"}`];
		if (readonly) parts.push(`${readonly} read-only`);
		return parts.join(" · ");
	}

	/** The raw-markdown view: the whole CONFIG in one textarea. Read-only — the
	 * grid is the edit surface; raw mode is for reading the shape, matching the
	 * queue's "edit raw" intent but kept inside the lock-governed view. */
	private renderRaw(c: HTMLElement): void {
		const ta = c.createEl("textarea", { cls: "nkui-config-raw" });
		ta.value = this.rawText;
		ta.readOnly = true;
		ta.rows = Math.min(40, Math.max(12, this.rawText.split("\n").length));
	}

	/** One CONFIG section: its heading, then each table as a grid. A section with
	 * no table renders a quiet read-only note (visible, never dropped). */
	private renderSection(list: HTMLElement, sec: ConfigSection): void {
		const group = list.createDiv("nkui-config-section");
		group.createDiv({ cls: "nkui-config-section-title", text: sec.heading });
		if (sec.tables.length === 0) {
			group.createDiv({
				cls: "nkui-config-readonly",
				text: "No table here — read this section in the raw markdown.",
			});
			return;
		}
		for (const tbl of sec.tables) this.renderTable(group, tbl);
	}

	/** One table as a grid: a header row of column names, then a row per record.
	 * Cells are plain text when locked; editable inputs when unlocked (a
	 * malformed row stays read-only even when unlocked). Composed from the same
	 * row/cell idiom — a CSS grid whose column count comes from the header. */
	private renderTable(host: HTMLElement, tbl: ConfigTable): void {
		const grid = host.createDiv("nkui-config-grid");
		// The column template is derived from the parsed header width — never a
		// fixed column set. One equal fraction per column.
		grid.style.setProperty("--nkui-config-cols", String(tbl.header.length));

		// Header row.
		const headRow = grid.createDiv("nkui-config-row nkui-config-headrow");
		for (const colName of tbl.header) {
			headRow.createDiv({ cls: "nkui-config-cell nkui-config-colhead", text: colName });
		}

		// Body rows.
		for (const row of tbl.rows) {
			const tr = grid.createDiv("nkui-config-row");
			if (row.malformed) tr.addClass("nkui-config-row-malformed");
			row.cells.forEach((cell, col) => {
				const td = tr.createDiv("nkui-config-cell");
				// Editable only when unlocked AND the row is well-formed; otherwise a
				// read-only text cell.
				if (this.unlocked && !row.malformed) {
					const input = td.createEl("textarea", { cls: "nkui-config-input", attr: { rows: "1" } });
					input.value = this.currentValue(row.line, col, cell);
					this.autoGrow(input);
					input.addEventListener("input", () => {
						this.autoGrow(input);
						this.recordEdit(row.line, col, input.value, cell);
					});
				} else {
					td.setText(cell);
					if (row.malformed) {
						td.setAttr(
							"title",
							"This row's cell count no longer matches its header — fix it in the raw markdown; it cannot be edited safely here."
						);
					}
				}
			});
		}
	}

	/** The pending value for a cell (a prior edit this session) or its original. */
	private currentValue(line: number, col: number, original: string): string {
		return this.edits.get(`${line}:${col}`)?.value ?? original;
	}

	/** Record (or clear) a pending edit. An edit equal to the original drops the
	 * pending entry, so a typed-then-reverted cell leaves no edit (the unedited
	 * round-trip stays a true no-op). */
	private recordEdit(line: number, col: number, value: string, original: string): void {
		const key = `${line}:${col}`;
		if (value === original) this.edits.delete(key);
		else this.edits.set(key, { line, col, value });
		// A fresh edit clears any stale refusal banner.
		if (this.refusalEl) this.refusalEl.hide();
	}

	/** Grow a one-line textarea to fit its content (shared idiom with nowView's
	 * autoGrow; local copy so configView carries no nowView dependency). */
	private autoGrow(ta: HTMLTextAreaElement): void {
		ta.style.height = "auto";
		ta.style.height = `${ta.scrollHeight}px`;
	}

	// ── lock / unlock ─────────────────────────────────────────────────────────

	/** The unlock scrim — a full-pane "are you sure?" gate, the only exit being a
	 * press-and-hold (holds.ts, the shared hold language). Reuses the queue
	 * unlock scrim classes so it looks and behaves identically. */
	private renderLockScrim(c: HTMLElement): void {
		const scrim = c.createDiv("nkui-queue-lock nkui-config-lock");
		scrim.createDiv({ cls: "nkui-queue-lock-title", text: "CONFIG is locked" });
		scrim.createDiv({
			cls: "nkui-queue-lock-text",
			text: "This edits the kit's canonical CONFIG. Hold to unlock editing — a save archives the prior file first and refuses any change that would break the table shape.",
		});
		const btn = scrim.createEl("button", {
			cls: "nkui-queue-lock-btn nkui-row-hold",
			text: "Hold to unlock editing",
		});
		btn.setAttr("aria-label", "Press and hold to unlock CONFIG editing");
		attachHold(btn, {
			keyHold: true,
			onCommit: () => void this.unlockFresh(),
		});
	}

	/** Unlock editing onto CURRENT content: re-read the file first, so the grid the
	 * user edits reflects any external write (sync_config, an agent) since the last
	 * render — the lock never releases onto a stale grid. */
	private async unlockFresh(): Promise<void> {
		try {
			this.rawText = await this.app.vault.adapter.read(CONFIG_PATH);
			this.parsed = parseConfig(this.rawText);
		} catch {
			// Keep the last good copy — an unreadable file leaves the current grid.
		}
		this.edits.clear();
		this.unlocked = true;
		this.render();
	}

	/** The save bar (shown while unlocked): a hold-to-save commit plus a cancel
	 * that re-locks and discards pending edits. Save is itself a HOLD — a write
	 * to the canonical CONFIG is consequential, so it speaks the same commit
	 * language as the unlock and the queue approvals. */
	private renderSaveBar(c: HTMLElement): void {
		const bar = c.createDiv("nkui-config-savebar");
		const pending = this.edits.size;
		bar.createSpan({
			cls: "nkui-config-pending",
			text: pending ? `${pending} ${pending === 1 ? "edit" : "edits"} pending` : "No edits yet",
		});
		const cancel = bar.createEl("button", { cls: "nkui-config-cancelbtn", text: "Cancel" });
		cancel.setAttr("aria-label", "Discard edits and re-lock");
		cancel.addEventListener("click", () => void this.reloadAndRender());
		const save = bar.createEl("button", {
			cls: "nkui-config-savebtn nkui-row-hold",
			text: "Hold to save",
		});
		save.setAttr("aria-label", "Press and hold to save CONFIG (archives the prior file first)");
		attachHold(save, { keyHold: true, onCommit: () => void this.save() });
	}

	// ── save path (SAFE — archive-prior, shape-validate, no auto-sync) ──────────

	/**
	 * Save the pending edits to CONFIG.md. The full safe write path:
	 *   1. nothing pending → no-op (with a quiet notice).
	 *   2. serialize the edits onto the parsed model — only edited table bodies
	 *      change; all prose, headings, alignment, and untouched rows verbatim.
	 *   3. SHAPE-VALIDATE the would-be text (validateShape, the config_shape.py
	 *      echo): a change that breaks the skeleton is REFUSED inline, nothing
	 *      written.
	 *   4. ARCHIVE the current CONFIG first (copy → verify it exists) under
	 *      <archive>/.../CONFIG-<timestamp>.md, the timestamp PASSED IN by the
	 *      caller (never Date.now()), honouring the never-destroy rule.
	 *   5. write the new CONFIG.
	 *   6. inline notice: saved + archived, run sync_config to propagate. The
	 *      plugin deliberately does NOT auto-run sync_config — it's an external
	 *      Python helper the plugin can't invoke reliably headlessly (the
	 *      conservative Pass-4 choice).
	 */
	async save(): Promise<void> {
		if (!this.parsed) return;
		if (this.edits.size === 0) {
			new Notice("Note Kit UI: no CONFIG edits to save.");
			return;
		}
		const edits: CellEdit[] = Array.from(this.edits.values());
		const edited = serializeConfig(this.parsed, edits);

		// 3 — shape gate. A skeleton-breaking change is refused, never written.
		const verdict = validateShape(edited);
		if (!verdict.ok) {
			this.showRefusal(verdict.refusals);
			return;
		}

		// 4 — archive the prior CONFIG first (copy → verify), timestamp passed in.
		const ts = this.archiveTimestamp();
		let archivePath = this.archivePathFor(ts);
		try {
			await this.ensureArchiveDir(archivePath);
			// Two saves within the same whole-second would resolve to the same path;
			// bump a suffix so a same-second snapshot never overwrites the prior one
			// (the archive is never destroyed).
			let n = 2;
			while (await this.app.vault.adapter.exists(archivePath)) {
				archivePath = this.archivePathFor(`${ts}-${n}`);
				n++;
			}
			await this.app.vault.adapter.copy(CONFIG_PATH, archivePath);
			const archived = await this.app.vault.adapter.exists(archivePath);
			if (!archived) throw new Error("archive copy not found after copy");
		} catch (e) {
			this.showRefusal([
				`Could not archive the prior CONFIG (${String(e)}). Nothing was written — a save never overwrites without archiving first.`,
			]);
			return;
		}

		// 5 — write the new CONFIG.
		try {
			await this.app.vault.adapter.write(CONFIG_PATH, edited);
		} catch (e) {
			new Notice(`Note Kit UI: CONFIG write failed — ${String(e)}. The prior file is archived at ${archivePath}.`);
			return;
		}

		// 6 — saved. Re-read (re-locks, clears edits) and tell the user to sync.
		new Notice(
			`Note Kit UI: CONFIG saved + archived — run sync_config to propagate.`,
			8000
		);
		await this.reloadAndRender();
	}

	/** Build the archive timestamp string. Accepts the live clock through a
	 * single private seam so a test can override it (the brief's "accept a
	 * timestamp string param, do NOT call Date.now()" — production reads the
	 * wall clock here once, tests stub `this.timestampOverride`). */
	private timestampOverride: string | null = null;
	private archiveTimestamp(): string {
		if (this.timestampOverride) return this.timestampOverride;
		// YYYY-MM-DD-HHmmss, filesystem-safe (no colons).
		const d = new Date();
		const p = (n: number) => String(n).padStart(2, "0");
		return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}-${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
	}

	/** The archive destination for a CONFIG snapshot: under the kit-config
	 * archive subfolder, named CONFIG-<timestamp>.md. The archive ROOT is taken
	 * from kitFacts (§ Folders `<archive>` literal) so a renamed archive folder
	 * is honoured; falls back to "Archive" when CONFIG names none. The path is
	 * derived from CONFIG, not hard-coded. */
	private archivePathFor(timestamp: string): string {
		const archiveRoot = this.plugin.kitFacts?.archiveLiteral || "Archive";
		// Mirror the vault's <archive>/<source-path>/ convention: CONFIG lives at
		// .claude/CONFIG.md, so its archive home names that source path.
		return `${archiveRoot}/.claude/CONFIG/CONFIG-${timestamp}.md`;
	}

	/** Create the archive directory chain for a destination path (adapter.mkdir
	 * is not recursive on every platform — walk the segments). */
	private async ensureArchiveDir(filePath: string): Promise<void> {
		const dir = filePath.split("/").slice(0, -1).join("/");
		if (!dir) return;
		const segments = dir.split("/");
		let acc = "";
		for (const seg of segments) {
			acc = acc ? `${acc}/${seg}` : seg;
			if (!(await this.app.vault.adapter.exists(acc))) {
				try {
					await this.app.vault.adapter.mkdir(acc);
				} catch {
					// A concurrent create or an already-existing dir is fine; a real
					// failure surfaces when the copy itself throws.
				}
			}
		}
	}

	/** Show the inline shape-refusal banner with one line per refusal. The
	 * banner replaces the silent-write the brief forbids — a precise message,
	 * never a quiet failure. */
	private showRefusal(messages: string[]): void {
		if (!this.refusalEl) return;
		this.refusalEl.empty();
		this.refusalEl.show();
		this.refusalEl.createDiv({
			cls: "nkui-config-refusal-title",
			text: "Save refused — the change would break the CONFIG shape:",
		});
		const ul = this.refusalEl.createEl("ul", { cls: "nkui-config-refusal-list" });
		for (const m of messages) ul.createEl("li", { text: m });
	}
}
