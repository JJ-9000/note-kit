import { ItemView, WorkspaceLeaf, TFile, setIcon, debounce } from "obsidian";
import type NoteKitUiPlugin from "./main";

export const NOW_VIEW_TYPE = "note-kit-now";

interface Entry {
	file: TFile;
	type: string | null;
	draft: boolean;
	queued: boolean;
	/** For a working-set gate: how many folded working members it stands in for. */
	setCount?: number;
	/** Folder-wide last activity (max mtime under the cover's folder) — a project is
	 * as fresh as its most recent file, not its cover note. */
	activity?: number;
	/** The inbox container (working set) this entry belongs to, when any —
	 * members render grouped under a quiet caption so their peer/approval
	 * relationship is visible at a glance. */
	container?: string;
	/** True for the container's gate file — the one document to actually read;
	 * approving it auto-approves the peers (CONFIG § Group approval). */
	isGate?: boolean;
}

interface RowOpts {
	showAge: boolean;
	showType: boolean;
	showDraft: boolean;
	showRowDot: boolean;
	/** When set, the row dot fades by age RELATIVE to this set: the freshest entry
	 * glows at full opacity, the oldest sits at the dim floor, everything else
	 * interpolates. Dimming means "stalest of what's here", not an absolute clock. */
	dimDotRange?: { newest: number; oldest: number };
}

/** One checkbox item parsed from the machine queue (a flat checklist). */
interface QueueItem {
	line: number;
	text: string;
	done: boolean;
}

/** One option line beneath a decision heading in the user queue. */
interface DecisionOption {
	text: string; // raw text, used to locate the line on write
	state: string; // raw checkbox char: " " open, "x"/"X" approved, "-" rejected
}

/**
 * One user-queue decision: a heading (`### …`) and the option checkboxes beneath
 * it. A single-choice item carries one option; a multiple-choice item lists
 * several, of which the user picks exactly one (action-agent SKILL § Proposal
 * shape). The "Decide" bucket counts decisions, not options — picking one
 * approves it and the whole decision clears from the pane. An empty `options`
 * means the item drifted off the queue format (heading with prose, no checkbox
 * lines) — it renders as a "needs reading" row instead of options.
 */
interface Decision {
	title: string | null;
	context: string;
	options: DecisionOption[];
}

const ARCHIVE = "99-Archive";
const BUCKET_CAP = 50;

/** Markdown checkbox line: groups are (lead)(state)(mid)(text)(trail). */
const CHECKBOX_RE = /^(\s*[-*]\s+\[)(.)(\]\s+)(.*\S)(\s*)$/;
/** A `##`/`###` heading line. */
const HEADING_RE = /^(#{2,})\s+(.*\S)\s*$/;

/**
 * The kit's front page. Surfaces what needs you (drafts grouped by type),
 * active work, the two kit queues as live checklists — the AI→you "Decide"
 * decisions and the you→AI "Queue" you can add to — and a recency feed.
 *
 * Reads metadataCache + file stats for the file lists; reads/writes only the two
 * queue files (toggling [x] and appending items), which exist for exactly that.
 */
export class NowView extends ItemView {
	plugin: NoteKitUiPlugin;
	private scheduleRender: () => void;
	private decisions: Decision[] = [];
	private machineItems: QueueItem[] = [];

	constructor(leaf: WorkspaceLeaf, plugin: NoteKitUiPlugin) {
		super(leaf);
		this.plugin = plugin;
		this.scheduleRender = debounce(() => void this.reloadAndRender(), 250, false);
	}

	getViewType(): string {
		return NOW_VIEW_TYPE;
	}
	getDisplayText(): string {
		return "For You";
	}
	getIcon(): string {
		return "sun";
	}

	async onOpen(): Promise<void> {
		this.registerEvent(this.app.metadataCache.on("resolved", this.scheduleRender));
		this.registerEvent(this.app.metadataCache.on("changed", this.scheduleRender));
		this.registerEvent(this.app.vault.on("create", this.scheduleRender));
		this.registerEvent(this.app.vault.on("delete", this.scheduleRender));
		this.registerEvent(this.app.vault.on("rename", this.scheduleRender));
		this.registerEvent(this.app.vault.on("modify", this.scheduleRender));
		// Column widths are measured from the DOM; a stylesheet arriving after a
		// render (theme switch, plugin CSS injecting on reload) changes the metrics,
		// so re-measure whenever the app's CSS changes.
		this.registerEvent(this.app.workspace.on("css-change", () => this.onResize()));
		this.render();
		await this.reloadAndRender();
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	/** Re-render now (used when settings change, which fires no vault event). */
	refresh(): void {
		void this.reloadAndRender();
	}

	private async reloadAndRender(): Promise<void> {
		await this.loadQueues();
		this.render();
	}

	// ── rendering ──────────────────────────────────────────────────────────────

	private render(): void {
		const s = this.plugin.settings;
		const c = this.contentEl;
		c.empty();
		c.addClass("nkui-now");

		const { needs, active } = this.collect();
		const openDecisions = this.decisions.filter(isOpenDecision);

		const head = c.createDiv("nkui-now-head");
		const tb = head.createDiv("nkui-now-titleblock");
		tb.createDiv({ cls: "nkui-now-title", text: formatToday() });
		tb.createDiv({
			cls: "nkui-now-subtitle",
			text: summary(needs.length, active.length, openDecisions.length),
		});
		const refresh = head.createEl("button", { cls: "clickable-icon nkui-now-refresh" });
		setIcon(refresh, "refresh-cw");
		refresh.setAttr("aria-label", "Refresh");
		refresh.addEventListener("click", () => void this.reloadAndRender());

		const groups = this.groupEntries(needs);
		const machineFile = this.app.vault.getAbstractFileByPath(s.machineQueuePath);

		// Both queues take precedence over the inbox drafts.
		// Decide — open user-queue decisions (AI → you); one entry per decision,
		// picking an option resolves it and clears the whole decision.
		if (openDecisions.length) this.renderDecideBucket(c, openDecisions);
		// Queue — you → AI checklist. Click an item's text to cross it off.
		if (machineFile instanceof TFile) {
			this.renderQueueBucket(c, "Queue/machine", "Queue", null, {
				items: this.machineItems,
				path: s.machineQueuePath,
				defaultOpen: true,
			});
		}

		const queuesShown = openDecisions.length > 0 || machineFile instanceof TFile;

		// Inbox drafts, grouped by type — below the queues.
		if (queuesShown && groups.size) c.createDiv("nkui-now-divider");
		for (const key of this.groupOrder([...groups.keys()])) {
			const items = groups.get(key);
			if (!items) continue;
			this.renderBucket(c, `Needs you/${key}`, key, this.colorFor(key), items, {
				showAge: false,
				showType: false,
				showDraft: false,
				showRowDot: false,
			});
		}

		// Active projects / areas.
		if (active.length) {
			c.createDiv("nkui-now-divider");
			const stamps = active.map((e) => e.activity ?? e.file.stat.mtime);
			this.renderBucket(
				c,
				"Active",
				"Active",
				null,
				active,
				{
					showAge: true,
					showType: true,
					showDraft: true,
					showRowDot: true,
					// Fade is relative to the set: freshest project = full glow,
					// oldest active project = the dim floor.
					dimDotRange: { newest: Math.max(...stamps), oldest: Math.min(...stamps) },
				},
				true
			);
		}
	}

	private renderBucket(
		parent: HTMLElement,
		id: string,
		label: string,
		color: string | null,
		entries: Entry[],
		rowOpts: RowOpts,
		defaultOpen = false
	): void {
		const b = parent.createDiv("nkui-now-group");
		b.toggleClass("is-collapsed", this.isCollapsed(id, defaultOpen));
		this.bucketHead(b, id, label, color, entries.length, defaultOpen);

		const wrap = b.createDiv("nkui-now-foldwrap");
		const list = wrap.createDiv("nkui-now-list");
		// Keep container members adjacent under one quiet caption: emit in input
		// order, starting a caption whenever the container changes. Gate-first
		// inside each container, so the document to read leads its peers.
		const shown = entries.slice(0, BUCKET_CAP);
		const ordered: (Entry | Entry[])[] = [];
		const byContainer = new Map<string, Entry[]>();
		for (const e of shown) {
			if (!e.container) {
				ordered.push(e);
				continue;
			}
			const g = byContainer.get(e.container);
			if (g) g.push(e);
			else {
				const arr = [e];
				byContainer.set(e.container, arr);
				ordered.push(arr);
			}
		}
		for (const item of ordered) {
			if (Array.isArray(item)) {
				item.sort((a, b) => Number(b.isGate ?? false) - Number(a.isGate ?? false));
				list.createDiv({ cls: "nkui-now-containercap", text: item[0].container as string });
				for (const m of item) this.renderRow(list, m, rowOpts, true);
			} else {
				this.renderRow(list, item, rowOpts);
			}
		}
		if (entries.length > BUCKET_CAP) {
			list.createDiv({ cls: "nkui-now-more", text: `+${entries.length - BUCKET_CAP} more…` });
		}
		this.equalizeMetaColumns(list);
	}

	/**
	 * Size each meta column (type, age) to its widest member, measured — not
	 * guessed in CSS. When a column's values are uniform (every Active row says
	 * "project") the box equals the text exactly and every gap in the block is
	 * exactly one spacing step; when they differ, the slack is the true minimum.
	 * The spacing standard's "no magic numbers": the content defines the column.
	 */
	private equalizeMetaColumns(scope: HTMLElement): void {
		for (const cls of ["nkui-now-metatype", "nkui-now-metaage"]) {
			const els = Array.from(scope.querySelectorAll<HTMLElement>(`.${cls}`));
			if (els.length < 2) continue;
			let max = 0;
			for (const el of els) {
				el.style.width = "";
				max = Math.max(max, el.getBoundingClientRect().width);
			}
			// Hidden view (background tab) measures 0 — leave natural; onResize
			// re-runs this when the view becomes visible.
			if (max <= 0) continue;
			// Exact sub-pixel width: rounding up would widen the box past the text
			// and the slack reads as an unequal gap beside an 8px neighbour.
			for (const el of els) el.style.width = `${max.toFixed(2)}px`;
		}
	}

	/** Re-measure the meta columns when the view's geometry changes — covers a
	 * render that happened while the tab was hidden (all widths measure 0 then). */
	onResize(): void {
		for (const list of Array.from(this.contentEl.querySelectorAll<HTMLElement>(".nkui-now-list"))) {
			this.equalizeMetaColumns(list);
		}
	}

	/** Decide bucket — each open decision is one entry; the count is decisions, not options. */
	private renderDecideBucket(parent: HTMLElement, decisions: Decision[]): void {
		const id = "Queue/decide";
		const b = parent.createDiv("nkui-now-group");
		b.toggleClass("is-collapsed", this.isCollapsed(id, true));
		this.bucketHead(b, id, "Decide", "var(--interactive-accent)", decisions.length, true);

		const wrap = b.createDiv("nkui-now-foldwrap");
		const list = wrap.createDiv("nkui-now-list");
		for (const d of decisions) this.renderDecision(list, d);
	}

	private renderDecision(list: HTMLElement, d: Decision): void {
		const path = this.plugin.settings.userQueuePath;
		const card = list.createDiv("nkui-now-decision");
		// Open the queue at this decision's heading, so a long queue lands you on
		// the item itself rather than the top of the file.
		const openQueue = () =>
			this.app.workspace.openLinkText(d.title ? `${path}#${d.title}` : path, "", false);
		if (d.title) {
			const title = card.createDiv({ cls: "nkui-now-decision-title", text: plainText(d.title) });
			title.addEventListener("click", openQueue);
		}
		if (d.context) card.createDiv({ cls: "nkui-now-decision-context", text: plainText(d.context) });

		// Drifted item — a heading with prose but no option checkboxes
		// (Format-User-Queue § No checkbox, no item). Nothing to pick, so render a
		// "needs reading" row that opens the queue at the heading; visible drift
		// beats an invisible drop.
		if (!d.options.length) {
			const row = card.createDiv("nkui-now-needsread");
			row.setAttr("role", "button");
			row.setAttr("tabindex", "0");
			setIcon(row.createSpan("nkui-now-needsread-icon"), "book-open");
			row.createSpan({ cls: "nkui-now-needsread-text", text: "Needs reading — no options to pick" });
			row.addEventListener("click", openQueue);
			row.addEventListener("keydown", (ev) => {
				if (ev.key === "Enter") openQueue();
			});
			return;
		}

		const selectable = d.options.filter((o) => o.state !== "-");
		const single = selectable.length <= 1;
		for (const o of selectable) {
			// Pick-ONE: checking an option approves it ([x]); the decision then
			// resolves and clears on the next render. The others are left as-is —
			// the action-agent clears the whole block when it acts.
			const row = card.createDiv("nkui-now-optrow");
			const cb = row.createEl("input", { cls: "nkui-now-qcheck", attr: { type: "checkbox" } });
			// Fill-in option (Format-User-Queue): a REPLACE-WITH-<WHAT> placeholder
			// renders as a text input. The typed value replaces the placeholder in
			// the queue file as the box checks; checking with it empty focuses the
			// input instead — the action-agent demotes an unedited placeholder, so
			// a bare checkbox here would only bounce.
			const fm = o.text.match(/REPLACE-WITH-([A-Z0-9-]+)/);
			if (fm) {
				const [pre, post] = o.text.split(fm[0], 2);
				if (pre.trim()) row.createSpan({ cls: "nkui-now-opttext", text: plainText(pre) });
				const fill = row.createEl("input", {
					cls: "nkui-now-fillin",
					attr: { type: "text", placeholder: fm[1].toLowerCase().replace(/-/g, " ") },
				});
				if (post && post.trim()) row.createSpan({ cls: "nkui-now-opttext", text: plainText(post) });
				const submit = () => {
					const v = fill.value.trim();
					if (!v) {
						cb.checked = false;
						fill.focus();
						return;
					}
					void this.resolveCard(card, () =>
					this.pickOptionFilled(path, o.text, o.text.replace(fm[0], v))
				);
				};
				cb.addEventListener("change", submit);
				fill.addEventListener("keydown", (ev) => {
					if (ev.key === "Enter" && fill.value.trim()) {
						ev.preventDefault();
						cb.checked = true;
						submit();
					}
				});
				continue;
			}
			cb.addEventListener("change", () =>
				void this.resolveCard(card, () => this.pickOption(path, o.text))
			);
			row.createSpan({ cls: "nkui-now-opttext", text: plainText(o.text) });
		}

		// Add-option input — a machine-queue-style way to propose another option.
		const add = card.createDiv("nkui-now-qadd");
		const input = add.createEl("input", {
			cls: "nkui-now-qaddinput",
			attr: { type: "text", placeholder: single ? "Add an option…" : "Add another option…" },
		});
		input.addEventListener("keydown", (ev) => {
			if (ev.key === "Enter" && input.value.trim()) {
				ev.preventDefault();
				void this.addOption(path, d, input.value.trim());
				input.value = "";
			}
		});
	}

	private renderQueueBucket(
		parent: HTMLElement,
		id: string,
		label: string,
		color: string | null,
		opts: { items: QueueItem[]; path: string; defaultOpen: boolean }
	): void {
		const openCount = opts.items.filter((i) => !i.done).length;
		const b = parent.createDiv("nkui-now-group");
		b.toggleClass("is-collapsed", this.isCollapsed(id, opts.defaultOpen));
		this.bucketHead(b, id, label, color, openCount, opts.defaultOpen);

		const wrap = b.createDiv("nkui-now-foldwrap");
		const list = wrap.createDiv("nkui-now-list");
		for (const item of opts.items) {
			// Machine queue: click the text to cross it off (toggles [x], matched by text).
			// Crossing off is not just visual — the agent's next-run sweep deletes [x]
			// lines from the file, so a done item shows its fate inline (tooltips
			// don't exist on mobile).
			const row = list.createDiv("nkui-now-qrow nkui-now-qrow-strike");
			const txt = row.createSpan({ cls: "nkui-now-qtext", text: item.text });
			if (item.done) {
				txt.addClass("is-done");
				row.createSpan({ cls: "nkui-now-qclear", text: "clears next agent run" });
				row.setAttr("aria-label", "Crossed off — the agent removes this from the queue on its next run. Click to restore.");
			} else {
				row.setAttr("aria-label", "Click to cross off — the agent removes crossed-off items from the queue on its next run.");
			}
			row.setAttr("title", row.getAttr("aria-label") ?? "");
			row.addEventListener("click", () =>
				void this.setItemChecked(opts.path, item.text, !item.done)
			);
		}
		if (!opts.items.length) {
			list.createDiv({ cls: "nkui-now-empty", text: "Nothing queued." });
		}
		const add = list.createDiv("nkui-now-qadd");
		const input = add.createEl("input", {
			cls: "nkui-now-qaddinput",
			attr: { type: "text", placeholder: "Add a task…" },
		});
		input.addEventListener("keydown", (ev) => {
			if (ev.key === "Enter" && input.value.trim()) {
				ev.preventDefault();
				void this.addMachineItem(input.value.trim());
				input.value = "";
			}
		});
	}

	/** Shared bucket header (count bubble + label, foldable). */
	private bucketHead(
		bucket: HTMLElement,
		id: string,
		label: string,
		color: string | null,
		count: number,
		defaultOpen: boolean
	): void {
		const gh = bucket.createDiv("nkui-now-group-head");
		gh.setAttr("role", "button");
		gh.setAttr("tabindex", "0");
		const cnt = gh.createSpan({ cls: "nkui-now-count", text: String(count) });
		if (color) cnt.style.background = color;
		else cnt.addClass("nkui-now-count-neutral");
		gh.createSpan({ cls: "nkui-now-group-title", text: label });

		const toggle = () => {
			const collapsed = bucket.classList.toggle("is-collapsed");
			this.setCollapsed(id, collapsed, defaultOpen);
		};
		gh.addEventListener("click", toggle);
		gh.addEventListener("keydown", (ev) => {
			if (ev.key === "Enter" || ev.key === " ") {
				ev.preventDefault();
				toggle();
			}
		});
	}

	private renderRow(list: HTMLElement, e: Entry, opts: RowOpts, contained = false): void {
		const row = list.createDiv("nkui-now-row");
		if (contained) row.addClass("nkui-now-row-contained");
		if (e.isGate) row.addClass("nkui-now-row-gate");
		row.setAttr("role", "button");
		row.setAttr("tabindex", "0");
		const open = (newLeaf: boolean) => this.app.workspace.openLinkText(e.file.path, "", newLeaf);
		row.addEventListener("click", (ev) => open(ev.ctrlKey || ev.metaKey));
		row.addEventListener("keydown", (ev) => {
			if (ev.key === "Enter") open(false);
		});

		if (opts.showRowDot) {
			const dot = row.createSpan("nkui-now-dot");
			const color = e.type ? this.colorFor(e.type) : null;
			if (color) dot.style.background = color;
			else dot.addClass("nkui-now-dot-empty");
			if (opts.dimDotRange) {
				// Fresh work glows, stale work fades — relative to THIS set: the
				// freshest row is full opacity, the oldest is the 0.25 floor.
				const { newest, oldest } = opts.dimDotRange;
				const ts = e.activity ?? e.file.stat.mtime;
				const span = newest - oldest;
				const t = span > 0 ? Math.min(1, Math.max(0, (newest - ts) / span)) : 0;
				dot.style.opacity = (1 - 0.75 * t).toFixed(2);
			}
		}

		row.createSpan({ cls: "nkui-now-rowtitle", text: this.displayName(e.file) });

		// The gate file is the one document to actually read — approving it
		// auto-approves its peers. The badge makes that dependency visible.
		if (e.isGate) {
			const gate = row.createSpan({ cls: "nkui-now-gatepill", text: "gate" });
			gate.setAttr(
				"aria-label",
				"The document to read — approving it approves this set's other files"
			);
			gate.setAttr("title", "Approving this approves the rest of the set");
		}

		// A gate stands in for a folded working-set: a quiet "+N" marks how many
		// working notes sit behind it, without listing them.
		if (e.setCount) {
			const set = row.createSpan({ cls: "nkui-now-setcount", text: `+${e.setCount}` });
			set.setAttr("aria-label", `${e.setCount} more in this set`);
		}

		const showType = opts.showType && !!e.type;
		if (showType || opts.showAge || (opts.showDraft && e.draft)) {
			// One meta block of columns — pill slot, type, age — sized to their
			// widest member by equalizeMetaColumns after the bucket renders, so the
			// block is the same width on every row and every internal gap is the
			// same spacing step. The pill slot always renders (empty when not a
			// draft); it right-aligns its pill so the slack sits invisibly on the left.
			const meta = row.createSpan("nkui-now-rowmeta");
			if (opts.showDraft) {
				const slot = meta.createSpan("nkui-now-pillslot");
				if (e.draft) slot.createSpan({ cls: "nkui-now-draftpill", text: "draft" });
			}
			if (showType) meta.createSpan({ cls: "nkui-now-metatype", text: e.type as string });
			if (opts.showAge) {
				meta.createSpan({ cls: "nkui-now-metaage", text: relAge(e.activity ?? e.file.stat.mtime) });
			}
		}
	}

	// ── queues (the only files this view writes to) ────────────────────────────

	private async loadQueues(): Promise<void> {
		const s = this.plugin.settings;
		this.decisions = await this.readDecisions(s.userQueuePath);
		this.machineItems = await this.readQueue(s.machineQueuePath);
	}

	private async readDecisions(path: string): Promise<Decision[]> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return [];
		return parseDecisions(await this.app.vault.cachedRead(f));
	}

	private async readQueue(path: string): Promise<QueueItem[]> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return [];
		const content = await this.app.vault.cachedRead(f);
		const items: QueueItem[] = [];
		content.split("\n").forEach((ln, i) => {
			const m = ln.match(CHECKBOX_RE);
			if (m) items.push({ line: i, text: m[4], done: m[2] !== " " });
		});
		return items;
	}

	/**
	 * Toggle a checkbox by matching its text, not a stored line index — so a
	 * pick is robust to the file shifting between the read and the write (e.g.
	 * the action-agent clearing a resolved item in between).
	 */
	private async setItemChecked(path: string, text: string, checked: boolean): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return;
		const lines = (await this.app.vault.read(f)).split("\n");
		for (let i = 0; i < lines.length; i++) {
			const m = lines[i].match(CHECKBOX_RE);
			if (m && m[4] === text) {
				lines[i] = `${m[1]}${checked ? "x" : " "}${m[3]}${m[4]}${m[5]}`;
				await this.app.vault.modify(f, lines.join("\n"));
				return;
			}
		}
	}

	/** Approve one option of a decision — writes [x] to its line, resolving the decision. */
	/** Collapse a resolved decision card (measured height -> 0) before the write
	 * re-renders the list, so the item leaves rather than blinks out. */
	private async resolveCard(card: HTMLElement, write: () => Promise<void>): Promise<void> {
		card.style.height = `${card.getBoundingClientRect().height}px`;
		void card.offsetHeight; // commit the measured height before transitioning
		card.addClass("is-resolving");
		card.style.height = "0px";
		await new Promise((r) => setTimeout(r, 190));
		await write();
	}

	private async pickOption(path: string, text: string): Promise<void> {
		await this.setItemChecked(path, text, true);
		await this.reloadAndRender();
	}

	/** Approve a fill-in option: replace its placeholder text and check it in one write. */
	private async pickOptionFilled(path: string, rawText: string, filledText: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return;
		const lines = (await this.app.vault.read(f)).split("\n");
		for (let i = 0; i < lines.length; i++) {
			const m = lines[i].match(CHECKBOX_RE);
			if (m && m[4] === rawText) {
				lines[i] = `${m[1]}x${m[3]}${filledText}${m[5]}`;
				await this.app.vault.modify(f, lines.join("\n"));
				break;
			}
		}
		await this.reloadAndRender();
	}

	/** Append a new option line beneath a decision's heading. */
	private async addOption(path: string, d: Decision, text: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return;
		const lines = (await this.app.vault.read(f)).split("\n");
		const at = lastOptionLine(lines, d);
		if (at < 0) return;
		lines.splice(at + 1, 0, `- [ ] ${text}`);
		await this.app.vault.modify(f, lines.join("\n"));
		await this.reloadAndRender();
	}

	private async addMachineItem(text: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(this.plugin.settings.machineQueuePath);
		if (!(f instanceof TFile)) return;
		const content = (await this.app.vault.read(f)).replace(/\s*$/, "");
		await this.app.vault.modify(f, `${content}\n- [ ] ${text}\n`);
	}

	// ── grouping ───────────────────────────────────────────────────────────────

	private groupEntries(entries: Entry[]): Map<string, Entry[]> {
		const groups = new Map<string, Entry[]>();
		for (const e of entries) {
			const key = this.groupKey(e);
			const arr = groups.get(key);
			if (arr) arr.push(e);
			else groups.set(key, [e]);
		}
		return groups;
	}

	private groupKey(e: Entry): string {
		if (e.type) return e.type;
		if (e.queued) return "queued";
		return "untyped";
	}

	private groupOrder(keys: string[]): string[] {
		const idx = new Map(this.plugin.typeStyles().map((t, i) => [t.type, i]));
		const rank = (k: string): number => {
			const i = idx.get(k);
			if (i !== undefined) return i;
			if (k === "queued") return 9000;
			if (k === "untyped") return 9002;
			return 9001;
		};
		return keys.sort((a, b) => rank(a) - rank(b));
	}

	// ── collapse state (persisted) ─────────────────────────────────────────────

	private isCollapsed(id: string, defaultOpen: boolean): boolean {
		if (defaultOpen) return this.plugin.settings.nowCollapsedSections.includes(id);
		return !this.plugin.settings.nowExpandedGroups.includes(id);
	}

	private setCollapsed(id: string, collapsed: boolean, defaultOpen: boolean): void {
		const set = defaultOpen
			? this.plugin.settings.nowCollapsedSections
			: this.plugin.settings.nowExpandedGroups;
		const store = defaultOpen ? collapsed : !collapsed;
		const i = set.indexOf(id);
		if (store && i < 0) set.push(id);
		else if (!store && i >= 0) set.splice(i, 1);
		void this.plugin.saveData(this.plugin.settings);
	}

	// ── data ─────────────────────────────────────────────────────────────────

	private collect(): { needs: Entry[]; active: Entry[] } {
		const s = this.plugin.settings;
		const activeTypes = new Set(s.nowActiveTypes);
		const queueFiles = new Set([s.userQueuePath, s.machineQueuePath]);

		const needs: Entry[] = [];
		const active: Entry[] = [];
		// Inbox working-sets: a container holding a nested working subfolder (a draft
		// two or more folders below the inbox) collapses to a single head row — the
		// shallowest draft, i.e. the gate the user reads — carrying a "+N" for the
		// working notes folded behind it (CONFIG § Inbox output convention / § Group
		// approval). A flat folder of independent peers (drafts only one level down,
		// e.g. session logs) stays expanded; loose drafts at the inbox root show as
		// themselves.
		const containers = new Map<string, { entry: Entry; depth: number }[]>();

		for (const f of this.app.vault.getMarkdownFiles()) {
			if (under(f.path, ARCHIVE)) continue;
			if (queueFiles.has(f.path)) continue; // queues are surfaced as items, not file rows

			const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
			const type = fm?.[s.typeField] != null ? String(fm[s.typeField]) : null;
			const draft = fm ? this.isUnreviewed(fm) : false;
			const inbox = s.inboxFolders.find((p) => under(f.path, p));
			const inQueue = s.nowQueueFolders.some((p) => under(f.path, p));
			const entry: Entry = { file: f, type, draft, queued: inQueue };

			if (draft && inbox) {
				const segs = f.path.slice(inbox.length + 1).split("/");
				if (segs.length === 1) {
					needs.push(entry); // loose draft at the inbox root
				} else {
					const container = `${inbox}/${segs[0]}`;
					const arr = containers.get(container);
					if (arr) arr.push({ entry, depth: segs.length });
					else containers.set(container, [{ entry, depth: segs.length }]);
				}
			} else if (inQueue) {
				needs.push(entry);
			} else if (type && activeTypes.has(type)) {
				active.push(entry);
			}
		}

		for (const [cpath, ds] of containers.entries()) {
			const cname = cpath.split("/").pop() ?? cpath;
			if (!ds.some((d) => d.depth >= 3)) {
				// flat peer folder — show each, grouped under its container caption,
				// with the gate file (a single 00-prefixed root member) badged so the
				// read-this-one / approves-the-rest relationship is visible.
				const gates = ds.filter((d) => d.depth === 1 && /^00[-_ ]/.test(d.entry.file.name));
				for (const d of ds) {
					d.entry.container = cname;
					needs.push(d.entry);
				}
				if (gates.length === 1) gates[0].entry.isGate = true;
				continue;
			}
			// nested working tree — collapse to its shallowest draft, fold the rest
			ds.sort((a, b) => a.depth - b.depth || a.entry.file.path.localeCompare(b.entry.file.path));
			const head = ds[0].entry;
			head.container = cname;
			head.isGate = true;
			if (ds.length > 1) head.setCount = ds.length - 1;
			needs.push(head);
		}

		// One row per project/area. A second active-typed file inside another
		// entry's folder is the same unit, not a new one (a mistyped child note
		// would otherwise duplicate its project's row): keep the shallower entry;
		// within one folder, the first by name wins, which keeps a 00- cover. A
		// file sitting loose at the vault root or a top-level root never folds
		// anything (its folder spans unrelated siblings).
		const folds = (o: Entry, e: Entry): boolean => {
			const op = o.file.parent?.path ?? "";
			const ep = e.file.parent?.path ?? "";
			if (!op.includes("/")) return false;
			if (op === ep) return o.file.name.localeCompare(e.file.name) < 0;
			return ep.startsWith(op + "/");
		};
		const folded = active.filter((e) => !active.some((o) => o !== e && folds(o, e)));
		active.length = 0;
		active.push(...folded);

		// An active project/area is as fresh as its most recently touched file, not
		// its cover note — compute folder-wide last activity for sort and age. A cover
		// sitting loose at a top-level root keeps its own mtime (its folder would span
		// unrelated siblings).
		const all = this.app.vault.getMarkdownFiles();
		for (const e of active) {
			const dir = e.file.parent?.path ?? "";
			if (dir.includes("/")) {
				let max = e.file.stat.mtime;
				for (const f of all) if (under(f.path, dir) && f.stat.mtime > max) max = f.stat.mtime;
				e.activity = max;
			}
		}

		const byMtime = (a: Entry, b: Entry) => b.file.stat.mtime - a.file.stat.mtime;
		const byActivity = (a: Entry, b: Entry) =>
			(b.activity ?? b.file.stat.mtime) - (a.activity ?? a.file.stat.mtime);
		needs.sort(byMtime);
		active.sort(byActivity);
		return { needs, active };
	}

	private isUnreviewed(fm: Record<string, unknown>): boolean {
		const v = fm[this.plugin.settings.reviewedField];
		return v === false || v === "false";
	}

	private colorFor(type: string): string | null {
		const t = this.plugin.typeStyles().find((x) => x.type === type);
		return t?.color || null;
	}

	private displayName(file: TFile): string {
		let name = file.basename;
		for (const p of this.plugin.settings.prefixStyles) {
			const re = new RegExp(`^${p.prefix.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}[ _-]+`);
			if (re.test(name)) {
				name = name.replace(re, "");
				break;
			}
		}
		return name;
	}
}

/** True if `path` is the folder `root` or sits anywhere beneath it. */
function under(path: string, root: string): boolean {
	return path === root || path.startsWith(root + "/");
}

/**
 * Parse the user queue into decisions. A heading (`##`/`###`) starts a decision;
 * the checkbox lines beneath it are its options, and the first prose line between
 * the heading and the options is its context. Checkboxes before any heading each
 * stand alone as a single-option decision.
 *
 * A heading with prose but NO checkboxes is format drift (Format-User-Queue
 * § No checkbox, no item) — kept as an option-less decision so the Decide bucket
 * can surface it as "needs reading" instead of silently dropping it. A bare
 * heading with no body at all is a producer/date group header, not an item.
 */
function parseDecisions(content: string): Decision[] {
	const out: Decision[] = [];
	let cur: { title: string | null; context: string[]; options: DecisionOption[] } | null = null;
	const flush = () => {
		if (cur && (cur.options.length || cur.context.length))
			out.push({ title: cur.title, context: cur.context.join(" "), options: cur.options });
	};
	for (const ln of content.split("\n")) {
		const hm = ln.match(HEADING_RE);
		if (hm) {
			flush();
			cur = { title: hm[2], context: [], options: [] };
			continue;
		}
		const cm = ln.match(CHECKBOX_RE);
		if (cm) {
			if (!cur) cur = { title: null, context: [], options: [] };
			cur.options.push({ text: cm[4], state: cm[2] });
			continue;
		}
		const t = ln.trim();
		// Context: prose between the heading and its first option (skip rules,
		// attribution lines, and anything after the options begin).
		if (cur && !cur.options.length && t && !t.startsWith("---") && !t.startsWith("_")) {
			cur.context.push(t);
		}
	}
	flush();
	return out;
}

/** A decision still needs you when it has a selectable option and none approved yet.
 * An option-less (drifted) decision always needs you — there is nothing to check off. */
function isOpenDecision(d: Decision): boolean {
	if (!d.options.length) return true;
	const approved = d.options.some((o) => o.state === "x" || o.state === "X");
	const selectable = d.options.some((o) => o.state === " ");
	return selectable && !approved;
}

/**
 * Line index of decision `d`'s last option, re-located by its heading then the
 * contiguous run of checkboxes beneath — so an appended option lands in the right
 * block even if line numbers shifted since the read.
 */
function lastOptionLine(lines: string[], d: Decision): number {
	let i = 0;
	if (d.title) {
		for (; i < lines.length; i++) {
			const hm = lines[i].match(HEADING_RE);
			if (hm && hm[2] === d.title) {
				i++;
				break;
			}
		}
		if (i >= lines.length) return -1;
	}
	let last = -1;
	let started = false;
	for (; i < lines.length; i++) {
		if (lines[i].match(HEADING_RE)) break;
		if (lines[i].match(CHECKBOX_RE)) {
			last = i;
			started = true;
		} else if (started && lines[i].trim()) {
			break; // the option run ended
		}
	}
	return last;
}

/** Strip light inline markdown (bold/italic/code markers) for plain display. */
function plainText(s: string): string {
	return s
		.replace(/\*\*(.+?)\*\*/g, "$1")
		.replace(/`([^`]+)`/g, "$1")
		.replace(/(^|\s)[*_]{1,2}(\S.*?\S|\S)[*_]{1,2}(?=\s|$)/g, "$1$2");
}

/** Primary title line — today's date, e.g. "Sunday, June 7". */
function formatToday(): string {
	return new Date().toLocaleDateString(undefined, {
		weekday: "long",
		month: "long",
		day: "numeric",
	});
}

/** Secondary title line — a quiet status summary. */
function summary(waiting: number, active: number, decide: number): string {
	const parts: string[] = [];
	if (decide > 0) parts.push(`${decide} to decide`);
	if (waiting > 0) parts.push(`${waiting} waiting`);
	if (active > 0) parts.push(`${active} active`);
	return parts.length ? parts.join("  ·  ") : "All clear";
}

/** Compact relative age, news-feed style: just now / 5m / 3h / 2d / 3w / 4mo / 1y.
 * No padding — column alignment is owned by equalizeMetaColumns, which sizes the
 * age column to its widest member; the text left-aligns inside, so the gap before
 * every age is identical regardless of the string. */
function relAge(mtime: number): string {
	const sec = Math.max(0, (Date.now() - mtime) / 1000);
	if (sec < 60) return "now";
	const min = Math.floor(sec / 60);
	if (min < 60) return `${min}m`;
	const hr = Math.floor(min / 60);
	if (hr < 24) return `${hr}h`;
	const day = Math.floor(hr / 24);
	if (day < 7) return `${day}d`;
	const wk = Math.floor(day / 7);
	if (wk < 5) return `${wk}w`;
	const mo = Math.floor(day / 30);
	if (mo < 12) return `${mo}mo`;
	return `${Math.floor(day / 365)}y`;
}
