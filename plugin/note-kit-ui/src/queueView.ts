import { ItemView, MarkdownView, WorkspaceLeaf, TFile, ViewStateResult, setIcon, Platform } from "obsidian";
import {
	Decision,
	QueueItem,
	autoGrow,
	isOpenDecision,
	isResolvedDecision,
	markReopenEdit,
	parseDecisions,
	parseQueueItems,
	plainText,
	renderAddTaskBox,
	renderDecisionCard,
	renderMachineItemRow,
} from "./nowView";
import { applyScreenShift, makeDeferredRender } from "./kitShared";
import * as queueWrites from "./queueWrites";
import { CHECKBOX_RE } from "./queueWrites";
import { attachHold, attachKeyActivate } from "./holds";
import type NoteKitUiPlugin from "./main";

export const QUEUE_VIEW_TYPE = "note-kit-queue";

/**
 * Per-session raw-escape memory: queue paths the user switched to the raw
 * markdown editor. While a path is here, main.ts does NOT re-route opens of
 * that file to the clean view — the user has escaped until the session ends
 * or the "Reopen queue in clean view" command (main.ts) clears it. Module
 * scope, deliberately: it survives view re-creation but not an app restart.
 */
export const rawEscapes = new Set<string>();

/**
 * Standalone clean view for a queue FILE — the user queue (decisions) or the
 * machine queue (checklist), path carried in the view state. Renders the same
 * clean rows the For You buckets show, composed from nowView's exported
 * renderers, and writes with the same file conventions ([x] pick, the
 * "*(item skipped)*" marker, appended `- [ ]` tasks).
 */
export class QueueView extends ItemView {
	plugin: NoteKitUiPlugin;
	/** Vault path of the queue file this leaf shows (view state). */
	private filePath = "";
	private decisions: Decision[] = [];
	private items: QueueItem[] = [];
	/** Machine mode: lines the clean checklist hides (headings, prose) — counted
	 * so the view can say they exist instead of silently dropping them. */
	private otherLines = 0;
	/** The add-task box (machine mode) — re-focused after an add re-render. */
	private addInput: HTMLTextAreaElement | null = null;
	private scheduleRender: () => void;
	// sideLocked / hold-to-unlock scrim removed (§ Y item 6 — "no gate").
	// The whole-row holds on each item already guard against stray taps.

	constructor(leaf: WorkspaceLeaf, plugin: NoteKitUiPlugin) {
		super(leaf);
		this.plugin = plugin;
		this.scheduleRender = makeDeferredRender(this, () => void this.reloadAndRender());
	}

	getViewType(): string {
		return QUEUE_VIEW_TYPE;
	}
	getDisplayText(): string {
		// `.pop()` on an unset path yields "" (never undefined) — `||` catches it.
		const base = this.filePath.split("/").pop() || "Queue";
		return base.replace(/\.md$/, "");
	}
	getIcon(): string {
		// list-checks is the right glyph for a queue but has no Material mask yet, so
		// the docked tab icon isn't solid-masked (queued follow-up: add the mask).
		// (check-square IS masked but is not a valid Obsidian icon name → ghost.)
		return "list-checks";
	}

	getState(): Record<string, unknown> {
		return { file: this.filePath };
	}

	async setState(state: unknown, result: ViewStateResult): Promise<void> {
		const file = (state as { file?: unknown } | null)?.file;
		this.filePath = typeof file === "string" ? file : "";
		await super.setState(state as Record<string, unknown>, result);
		await this.reloadAndRender();
	}

	async onOpen(): Promise<void> {
		// React to the queue file changing under us (an agent pass, a sync).
		this.registerEvent(
			this.app.vault.on("modify", (f) => {
				if (f.path === this.filePath) this.scheduleRender();
			})
		);
		this.registerEvent(
			this.app.vault.on("rename", (f, oldPath) => {
				// A renamed file carries its raw-escape with it — the old path's
				// entry would otherwise go stale (and the new path lose its escape).
				if (rawEscapes.has(oldPath)) {
					rawEscapes.delete(oldPath);
					rawEscapes.add(f.path);
				}
				if (oldPath === this.filePath) this.filePath = f.path;
				this.scheduleRender();
			})
		);
		// Dragging the leaf between the sidebar and the main area changes which
		// styling applies — re-stamp on layout changes, render() reads it fresh.
		this.registerEvent(
			this.app.workspace.on("layout-change", () => {
				this.contentEl.toggleClass("nkui-queue-side", this.isSideLeaf());
			})
		);
		await this.reloadAndRender();
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	private file(): TFile | null {
		const f = this.app.vault.getAbstractFileByPath(this.filePath);
		return f instanceof TFile ? f : null;
	}

	/** True when this leaf shows the user queue (decisions); else the machine
	 * checklist shape applies. */
	private isUserQueue(): boolean {
		return this.filePath === this.plugin.settings.userQueuePath;
	}

	/** True when this leaf lives in a sidebar dock, where the narrow-width
	 * styling and the mobile unlock gate apply. Delegates to the plugin's
	 * root-tolerant test (split equality on desktop — a popout window wants
	 * the normal page treatment; anything outside the main area on mobile,
	 * where drawers can report a different root). */
	private isSideLeaf(): boolean {
		return this.plugin.inSidebar(this.leaf);
	}

	private async reloadAndRender(): Promise<void> {
		const f = this.file();
		this.otherLines = 0;
		if (f) {
			const content = await this.app.vault.cachedRead(f);
			if (this.isUserQueue()) {
				this.decisions = parseDecisions(content);
				this.items = [];
			} else {
				this.items = parseQueueItems(content);
				this.decisions = [];
				this.otherLines = countOtherLines(content);
			}
		} else {
			this.decisions = [];
			this.items = [];
		}
		this.render();
	}

	onResize(): void {
		applyScreenShift(this.contentEl, this.plugin.settings);
	}

	// ── rendering ──────────────────────────────────────────────────────────────

	private render(): void {
		const c = this.contentEl;
		c.empty();
		this.addInput = null;
		// `nkui-now` carries the For You page language: the centred 760px content
		// column (`.nkui-now > *`), the page padding, the stable scrollbar gutter.
		c.addClass("nkui-now");
		c.addClass("nkui-queue");
		applyScreenShift(this.contentEl, this.plugin.settings);
		// A sidebar dock is narrow — the stylesheet keys compact spacing off this.
		c.toggleClass("nkui-queue-side", this.isSideLeaf());

		const f = this.file();
		const user = this.isUserQueue();
		// Which queue this page is — semantic markers (no CSS keys on them yet).
		c.toggleClass("nkui-queue-user", user);
		c.toggleClass("nkui-queue-machine", !user);
		// The queue pages share the theme accent tint (both queues render with
		// the accent in the For You view); the stylesheet keys the page wash and
		// header colour off this var, like the For You section colour.
		c.style.setProperty("--nkui-section-color", "var(--interactive-accent)");
		const openCount = user
			? this.decisions.filter(isOpenDecision).length
			: this.items.filter((i) => !i.done).length;

		// Header — file name, open-item count, and the raw-editor escape.
		const head = c.createDiv("nkui-queue-head");
		const tb = head.createDiv("nkui-queue-titleblock");
		tb.createDiv({ cls: "nkui-queue-title", text: this.getDisplayText() });
		tb.createDiv({
			cls: "nkui-queue-itemcount",
			text: f
				? `${openCount} open ${openCount === 1 ? "item" : "items"}`
				: "file not found",
		});
		const raw = head.createEl("button", { cls: "clickable-icon nkui-queue-rawbtn" });
		setIcon(raw, "file-pen-line");
		raw.setAttr("aria-label", "Edit raw — open the markdown file (re-open restores the clean view via the command)");
		raw.setAttr("title", "Edit raw");
		raw.addEventListener("click", () => void this.openRaw());

		if (!f) return;
		// ONE explanatory sentence under the header — what this queue is and who
		// acts on it, in the For You page's plain voice.
		c.createDiv({
			cls: "nkui-queue-explain",
			text: user
				? "Proposals from the agents — check a box to decide; the action-agent executes and clears it."
				: "Your checklist for the agents — add a task; the action-agent runs it on its next pass.",
		});
		const list = c.createDiv("nkui-now-list");
		if (user) this.renderDecisions(list);
		else this.renderChecklist(list);
	}

	/** Switch this leaf to the normal markdown view for the file. The path joins
	 * the session's raw-escape set first, so the file-open router doesn't bounce
	 * the editor straight back into this view. */
	private async openRaw(): Promise<void> {
		const f = this.file();
		if (!f) return;
		rawEscapes.add(f.path);
		await this.leaf.setViewState({
			type: "markdown",
			state: { file: f.path },
			active: true,
		});
		// Swapping the view type for the SAME file does not re-fire file-open, so
		// the file-open handler's clean-view header action never mounts — the raw
		// editor would strand the user with no way back. Mount it directly here,
		// deferred a tick so the markdown view has loaded its file first.
		window.setTimeout(() => {
			const v = this.leaf.view;
			if (v instanceof MarkdownView) this.plugin.addCleanViewAction(v, f.path);
		}, 0);
	}

	// ── machine queue — checklist rows + add box ───────────────────────────────

	private renderChecklist(list: HTMLElement): void {
		for (const item of this.items) {
			renderMachineItemRow(list, item, {
				onSkip: (it) => void this.skipItem(it),
				onEdit: (main, it) => this.editItem(main, it),
			});
		}
		if (!this.items.length) {
			list.createDiv({ cls: "nkui-queue-empty", text: "Nothing queued." });
		}
		// The clean checklist hides any non-checklist lines — say so, faintly,
		// instead of letting them vanish; the row opens the raw editor.
		if (this.otherLines > 0) {
			const n = this.otherLines;
			const row = list.createDiv({
				cls: "nkui-now-more nkui-queue-otherlines",
				text: `${n} other ${n === 1 ? "line" : "lines"} — open raw`,
			});
			const aria = "Lines the clean checklist hides (headings, prose) — open the raw markdown to see them";
			row.setAttr("aria-label", aria);
			row.setAttr("title", aria);
			row.setAttr("role", "button");
			row.setAttr("tabindex", "0");
			row.addEventListener("click", () => void this.openRaw());
			attachKeyActivate(row, () => void this.openRaw());
		}
		this.addInput = renderAddTaskBox(list, (text) => void this.addItem(text));
	}

	/** Cross off / restore an item — the shared skip semantics (the
	 * "*(item skipped)*" marker beside the [x]). */
	private async skipItem(item: QueueItem): Promise<void> {
		const f = this.file();
		if (f) await queueWrites.toggleSkip(this.app.vault, f, item);
		await this.reloadAndRender();
	}

	private async addItem(text: string): Promise<void> {
		const f = this.file();
		if (!f) return;
		await queueWrites.appendTask(this.app.vault, f, text);
		await this.reloadAndRender();
		const box = this.addInput;
		if (box) {
			autoGrow(box);
			// Desktop types tasks in a run — re-focus the re-created box. Mobile
			// must NOT: re-focusing re-summons the just-dismissed iOS keyboard.
			if (!Platform.isMobile) box.focus();
		}
	}

	/** Replace an open task's text in place, keeping its checkbox state. */
	private async updateItem(oldText: string, newText: string): Promise<void> {
		const f = this.file();
		if (f) await queueWrites.updateTask(this.app.vault, f, oldText, newText);
		await this.reloadAndRender();
	}

	/** Turn a task row into a live editor — Enter saves, Esc or blur cancels.
	 * Mirrors NowView.editMachineItem. */
	private editItem(main: HTMLElement, item: QueueItem): void {
		main.empty();
		const ta = main.createEl("textarea", {
			cls: "nkui-now-qaddinput nkui-now-qedit-field",
			attr: { rows: "1" },
		});
		ta.value = item.text;
		ta.addEventListener("input", () => autoGrow(ta));
		window.setTimeout(() => {
			autoGrow(ta);
			ta.focus();
			ta.setSelectionRange(ta.value.length, ta.value.length);
		}, 0);
		let done = false;
		const save = (): void => {
			if (done) return;
			done = true;
			const v = ta.value.trim();
			if (v && v !== item.text) void this.updateItem(item.text, v);
			else void this.reloadAndRender();
		};
		ta.addEventListener("keydown", (ev) => {
			if (ev.key === "Enter" && !ev.shiftKey) {
				ev.preventDefault();
				save();
			} else if (ev.key === "Escape") {
				ev.preventDefault();
				done = true;
				void this.reloadAndRender();
			}
		});
		ta.addEventListener("blur", save);
	}

	// ── user queue — decision rows ─────────────────────────────────────────────

	private renderDecisions(list: HTMLElement): void {
		const open = this.decisions.filter(isOpenDecision);
		const resolved = this.decisions.filter(isResolvedDecision);
		// The shared decision-card renderer (the For You Decide bucket uses it too).
		// The queue view opens the raw markdown, leaves the title non-clickable,
		// writes immediately (no fade), and offers no add-option box.
		for (const d of open) {
			renderDecisionCard(list, d, {
				onOpen: () => void this.openRaw(),
				readOnlyText: "Read-only — open the raw file to read",
				titleOpens: false,
				onPick: (text) => this.pickOption(text),
				onPickFilled: (raw, filled) => this.pickOptionFilled(raw, filled),
				onAcknowledge: () => this.acknowledge(d),
				onEditOption: (raw, next) => this.updateOption(d, raw, next),
			});
		}
		for (const d of resolved) this.renderResolvedDecision(list, d);
		if (!open.length && !resolved.length) {
			list.createDiv({ cls: "nkui-queue-empty", text: "Nothing to decide." });
		}
	}

	/** A resolved decision lingers as a struck reduced row; the checkbox OR a
	 * whole-row hold re-opens it (§ Y item 1 — the whole-row hold replaced the
	 * old tap path, so we expose both affordances: the familiar checkbox remains,
	 * and the hold gesture mirrors every other queue row). attachHold exempts
	 * interactive children (checkboxes), so both paths remain live. */
	private renderResolvedDecision(list: HTMLElement, d: Decision): void {
		const picked = d.options.find((o) => o.state === "x" || o.state === "X");
		if (!picked) return;
		const label = d.title ? `${plainText(d.title)} — ${plainText(picked.text)}` : plainText(picked.text);
		const restore = async (): Promise<void> => {
			// Re-open the pick as editable (B5): mark it so the card pre-fills the
			// committed response, whose fill-in placeholder the write consumed.
			markReopenEdit(picked.text);
			await this.setChecked(picked.text, false);
			await this.reloadAndRender();
		};
		const aria = "Approved — hold or uncheck to re-open; the agent executes and clears this on its next pass.";
		const row = list.createDiv("nkui-now-reducedrow nkui-row-hold");
		row.setAttr("aria-label", aria);
		row.setAttr("title", aria);
		row.setAttr("role", "button");
		row.setAttr("tabindex", "0");
		const cb = row.createEl("input", { cls: "nkui-now-qcheck", attr: { type: "checkbox" } });
		cb.checked = true;
		cb.addEventListener("change", () => void restore());
		const main = row.createDiv("nkui-now-reducedmain");
		main.createSpan({ cls: "nkui-now-reducedtitle is-struck", text: label });
		main.createSpan({ cls: "nkui-now-fatenote", text: "awaiting action agent" });
		attachHold(row, { onCommit: restore, keyHold: true });
	}

	/** Toggle a checkbox by matching its text — robust to the file shifting. */
	private async setChecked(text: string, checked: boolean): Promise<void> {
		const f = this.file();
		if (f) await queueWrites.setChecked(this.app.vault, f, text, checked);
	}

	private async pickOption(text: string): Promise<void> {
		await this.setChecked(text, true);
		await this.reloadAndRender();
	}

	/** Approve a fill-in option: replace its placeholder and check it in one write. */
	private async pickOptionFilled(rawText: string, filledText: string): Promise<void> {
		const f = this.file();
		if (f) await queueWrites.pickOptionFilled(this.app.vault, f, rawText, filledText);
		await this.reloadAndRender();
	}

	/** Rewrite one decision option's text in place (tap-to-edit), keeping its state. */
	private async updateOption(d: Decision, oldText: string, newText: string): Promise<void> {
		const f = this.file();
		if (f) await queueWrites.updateOption(this.app.vault, f, d, oldText, newText);
		await this.reloadAndRender();
	}

	/** Acknowledge a non-choice notification — the shared write appends the
	 * sanctioned option text checked under its heading, so it resolves like a
	 * picked decision (NowView.acknowledge). */
	private async acknowledge(d: Decision): Promise<void> {
		const f = this.file();
		if (f) await queueWrites.acknowledge(this.app.vault, f, d);
		await this.reloadAndRender();
	}
}

/** Lines the machine checklist's clean render hides: not blank, not a checkbox
 * item, not the YAML frontmatter block. */
function countOtherLines(content: string): number {
	const lines = content.split("\n");
	let i = 0;
	if (lines[0]?.trim() === "---") {
		for (i = 1; i < lines.length; i++) {
			if (lines[i].trim() === "---") {
				i++;
				break;
			}
		}
	}
	let n = 0;
	for (; i < lines.length; i++) {
		if (lines[i].trim() && !lines[i].match(CHECKBOX_RE)) n++;
	}
	return n;
}
