import { ItemView, WorkspaceLeaf, TFile, TFolder, setIcon, debounce, Platform } from "obsidian";
import { typeClass } from "./settings";
import { toneVars } from "./palette";
import { attachHold, attachKeyActivate } from "./holds";
import * as queueWrites from "./queueWrites";
import { CHECKBOX_RE, HEADING_RE, SKIP_MARK_RE, EXEC_MARK_RE } from "./queueWrites";
import type NoteKitUiPlugin from "./main";

export const NOW_VIEW_TYPE = "note-kit-now";
/** The sidebar twin — the SAME full For You page, registered under a second
 * view type so one instance can live in the dock (mobile swipe-left) while
 * the main tab keeps its own. The sidebar never gets a lesser version. */
export const NOW_SIDE_VIEW_TYPE = "note-kit-now-side";

interface Entry {
	file: TFile;
	type: string | null;
	draft: boolean;
	queued: boolean;
	/** For a working-set gate: how many folded working members it stands in for. */
	setCount?: number;
	/** The member files this gate gates (the folded children) — used to un-approve
	 * the whole set from the Waiting section. */
	setFiles?: TFile[];
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
	/** The gate is already approved while members are still drafts: the set is
	 * done deciding and waits on the filing-agent, not the user. */
	awaitingFiling?: boolean;
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
	/** This section can approve drafts: every draft row (gate or lone file) is
	 * itself a press-hold — hold the whole row to approve it. */
	approve?: boolean;
}

/** One checkbox item parsed from the machine queue (a flat checklist).
 * Exported for the standalone queue view. */
export interface QueueItem {
	line: number;
	text: string;
	done: boolean;
}

/** One option line beneath a decision heading in the user queue. */
export interface DecisionOption {
	text: string; // raw text, used to locate the line on write
	state: string; // raw checkbox char: " " open, "x"/"X" approved, "-" rejected
}

/**
 * One user-queue decision: a heading (`### …`) and the option checkboxes beneath
 * it. A single-choice item carries one option; a multiple-choice item lists
 * several, of which the user picks exactly one (action-agent SKILL § Proposal
 * shape). The "Decide" bucket counts decisions, not options — picking one
 * approves it ([x]); the decision stays rendered as resolved until the
 * action-agent's next pass clears it from the file, and unchecking re-opens
 * it. An empty `options`
 * means the item drifted off the queue format (heading with prose, no checkbox
 * lines) — it renders as a "needs reading" row instead of options.
 */
export interface Decision {
	title: string | null;
	context: string;
	options: DecisionOption[];
}

/** Fallback archive root when neither CONFIG nor a root-folder name resolves one. */
const ARCHIVE_FALLBACK = "99-Archive";
const BUCKET_CAP = 50;

/** Gate-member unfolds, keyed by gate file path — so reviewing a set's children
 * survives the re-render every file visit triggers (the unfold used to reset,
 * forcing the gate open again after each member). In-memory at module scope,
 * the nowExpandedGroups pattern minus the persistence: shared by the main and
 * sidebar twins, reset only by an app restart. */
const expandedGates = new Set<string>();

/** A loose file (or folder) dropped in the outbox folder — queued work in file
 * form, surfaced as a Queue row. `open` is what a click opens: the file itself,
 * or a folder's first file. */
interface OutboxDrop {
	name: string;
	folder: boolean;
	open: TFile | null;
	type: string | null;
}

// The checkbox/heading/marker literals live in queueWrites (the shared write
// conventions module) — imported above for parsing and row rendering.

/**
 * The kit's front page. Surfaces what needs you (drafts grouped by type),
 * active work, and the two kit queues as live checklists — the AI→you
 * "Decide" decisions and the you→AI "Queue" you can add to.
 *
 * Reads metadataCache + file stats for the file lists; reads/writes only the two
 * queue files (toggling [x] and appending items), which exist for exactly that.
 */
export class NowView extends ItemView {
	plugin: NoteKitUiPlugin;
	private scheduleRender: () => void;
	private decisions: Decision[] = [];
	private machineItems: QueueItem[] = [];
	/** The Queue section's add-task textarea (re-created every render) — kept so
	 * addMachineItem can restore focus after its immediate re-render. */
	private machineAddInput: HTMLTextAreaElement | null = null;

	/** True when this instance is the sidebar twin — same full render, second
	 * registered view type (the only difference is where the leaf lives). */
	private side: boolean;

	constructor(leaf: WorkspaceLeaf, plugin: NoteKitUiPlugin, side = false) {
		super(leaf);
		this.plugin = plugin;
		this.side = side;
		this.scheduleRender = debounce(() => {
			// Never wipe a field the user is typing in (add a task, edit one, pick an
			// option): a background vault change must not blow away in-progress text.
			// Defer until the field loses focus, then catch up.
			const ae = document.activeElement;
			if (
				ae instanceof HTMLElement &&
				(ae.tagName === "TEXTAREA" || ae.tagName === "INPUT") &&
				this.contentEl.contains(ae)
			) {
				this.scheduleRender();
				return;
			}
			void this.reloadAndRender();
		}, 250, false);
	}

	getViewType(): string {
		return this.side ? NOW_SIDE_VIEW_TYPE : NOW_VIEW_TYPE;
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
		// FIRST open: the renders above can run before the view has laid out
		// (pane box 0, pre-font metrics), so the screen-centering shift measures
		// nothing useful until something re-triggers onResize. Measure again now
		// the initial load is in; a still-hidden pane (box 0) keeps its last
		// value and the next onResize catches it, as ever.
		this.measurePaneShift();
	}

	async onClose(): Promise<void> {
		this.contentEl.empty();
	}

	/** Re-render now (used when settings change, which fires no vault event).
	 * Forced: a settings change can alter presentation (colours, bias) without
	 * moving anything the render signature tracks. */
	refresh(): void {
		void this.reloadAndRender(true);
	}

	/** `force` bypasses the no-change signature bail (render) — for callers
	 * that destroyed DOM they need rebuilt (a cancelled inline edit) or whose
	 * change is invisible to the signature (settings). */
	private async reloadAndRender(force = false): Promise<void> {
		await this.loadQueues();
		this.render(force);
	}

	/**
	 * THE way a For You row/member/drop opens its file. Never this view's own
	 * leaf: replacing it tore the page down and visibly flashed before the file
	 * appeared. Reuse the most recently active main-area markdown leaf; with
	 * none (or on a ctrl/meta press), a fresh tab. `subpath` ("#Heading") lands
	 * a queue open on the item itself, like the old openLinkText fragment.
	 */
	private openPath(path: string, newTab: boolean, subpath?: string): void {
		const file = this.app.vault.getAbstractFileByPath(path);
		if (!(file instanceof TFile)) return;
		const ws = this.app.workspace;
		let leaf: WorkspaceLeaf | null = null;
		if (!newTab) {
			// Most recently used markdown leaf in the MAIN area (activeTime is the
			// workspace's own recency stamp). The For You leaf is never markdown,
			// so it can't be picked; the sidebar twin is excluded by root.
			let best = -1;
			for (const l of ws.getLeavesOfType("markdown")) {
				if (l.getRoot() !== ws.rootSplit) continue;
				const t = (l as unknown as { activeTime?: number }).activeTime ?? 0;
				if (t > best) {
					best = t;
					leaf = l;
				}
			}
		}
		if (!leaf) leaf = ws.getLeaf("tab");
		void leaf.openFile(file, {
			active: true,
			eState: subpath ? { subpath } : undefined,
		});
	}

	/** Brief exit beat after a hold commit: mark the element settling (the
	 * stylesheet fades/folds `.is-settling` over --nkui-settle) and wait that
	 * beat, so committed rows visibly leave BEFORE the re-render removes them —
	 * no instant snap under a still-pressed cursor. No-op with animations off
	 * (body.nkui-anim absent). */
	private async settle(el: HTMLElement | null | undefined): Promise<void> {
		if (!el || !document.body.hasClass("nkui-anim")) return;
		el.addClass("is-settling");
		await new Promise((r) => setTimeout(r, settleMs()));
	}

	// ── rendering ──────────────────────────────────────────────────────────────

	/** Last rendered page's signature (renderSig) — the no-change bail. */
	private lastSig: string | null = null;

	/** Cheap digest of everything the page actually shows. Equal signatures
	 * mean a rebuild would paint the identical page — so the broad event
	 * listeners (resolved/changed fire on plenty that this view doesn't show,
	 * e.g. whatever a folder-open touches) can bail instead of tearing the
	 * DOM down and re-shuffling it under the user. Real changes — paths,
	 * types, draft/approved flags, mtimes (the rendered ages), queue text and
	 * states, drops, the date title — all move the signature. */
	private renderSig(needs: Entry[], active: Entry[], waiting: Entry[], drops: OutboxDrop[]): string {
		const ent = (e: Entry): string =>
			`${e.file.path}|${e.type ?? ""}|${e.draft ? 1 : 0}|${e.setCount ?? 0}|${
				e.awaitingFiling ? 1 : 0
			}|${e.activity ?? e.file.stat.mtime}`;
		const dec = (d: Decision): string =>
			`${d.title ?? ""}|${d.context}|${d.options.map((o) => o.state + o.text).join(",")}`;
		return [
			formatToday(),
			needs.map(ent).join(";"),
			active.map(ent).join(";"),
			waiting.map(ent).join(";"),
			this.decisions.map(dec).join(";"),
			this.machineItems.map((i) => `${i.done ? 1 : 0}${i.text}`).join(";"),
			drops.map((d) => `${d.name}|${d.folder ? 1 : 0}|${d.type ?? ""}`).join(";"),
		].join("\n");
	}

	private render(force = false): void {
		const s = this.plugin.settings;
		const { needs, active, waiting } = this.collect();
		const drops = this.collectOutboxDrops();
		// No-change bail: nothing the view shows moved, so keep the live DOM —
		// rebuilding it anyway is what made the list visibly shuffle on broad
		// events (every fold state, scroll position and row identity survives).
		const sig = this.renderSig(needs, active, waiting, drops);
		if (!force && sig === this.lastSig) return;
		this.lastSig = sig;

		const c = this.contentEl;
		c.empty();
		this.machineAddInput = null; // re-set when the Queue bucket renders
		c.addClass("nkui-now");

		const openDecisions = this.decisions.filter(isOpenDecision);
		const resolvedDecisions = this.decisions.filter(isResolvedDecision);

		const head = c.createDiv("nkui-now-head");
		const tb = head.createDiv("nkui-now-titleblock");
		tb.createDiv({ cls: "nkui-now-title", text: formatToday() });
		// Build the subtitle as DOM so each segment can carry twin spans for the
		// container-query shortening (item W3). Plain-text segments stay as text
		// nodes; count segments wrap their label in nkui-twin-long / nkui-twin-short.
		renderSubtitle(
			tb,
			needs.length,
			active.length,
			openDecisions.length,
			waiting.length + resolvedDecisions.length
		);
		const refresh = head.createEl("button", { cls: "clickable-icon nkui-now-refresh" });
		setIcon(refresh, "refresh-cw");
		refresh.setAttr("aria-label", "Refresh");
		refresh.addEventListener("click", () => void this.reloadAndRender());

		const groups = this.groupEntries(needs);
		const machineFile = this.app.vault.getAbstractFileByPath(s.machineQueuePath);

		// Queues first — the configured user-interaction surfaces. Decide shows
		// only OPEN decisions; resolved ones drop to the Waiting section below.
		if (openDecisions.length) this.renderDecideBucket(c, openDecisions);
		// Queue — you → AI checklist. Click an item's text to cross it off.
		// Outbox drops (loose files beside the queue file) render as rows too.
		if (machineFile instanceof TFile || drops.length) {
			this.renderQueueBucket(c, "Queue/machine", "Queue", "var(--nkui-queue-color, var(--interactive-accent))", {
				items: this.machineItems,
				drops,
				path: s.machineQueuePath,
				defaultOpen: true,
			});
		}

		const queuesShown = openDecisions.length > 0 || machineFile instanceof TFile || drops.length > 0;

		// Inbox drafts that still need you, grouped by type — below the queues.
		if (queuesShown && groups.size) c.createDiv("nkui-now-divider");
		for (const key of this.groupOrder([...groups.keys()])) {
			const items = groups.get(key);
			if (!items) continue;
			this.renderBucket(
				c,
				`Needs you/${key}`,
				pluralLabel(key, items.length),
				this.colorFor(key),
				items,
				{ showAge: false, showType: false, showDraft: false, showRowDot: false },
				false,
				true
			);
		}

		// Waiting — approved gates awaiting the filing-agent and resolved
		// decisions awaiting the action-agent: settled by you, pending an agent.
		// One quiet neutral-grey section gathered right above Active.
		if (waiting.length || resolvedDecisions.length) {
			c.createDiv("nkui-now-divider");
			this.renderWaitingSection(c, waiting, resolvedDecisions);
		}

		// Active projects / areas.
		if (active.length) {
			c.createDiv("nkui-now-divider");
			const stamps = active.map((e) => e.activity ?? e.file.stat.mtime);
			// The Active header takes the colour of the top item — under the
			// type-major order that's the freshest entry of the first CONFIG
			// type (a project, when any is active) — its count pill and
			// open-emphasis follow.
			const activeColor = active[0].type ? this.colorFor(active[0].type) : null;
			this.renderBucket(
				c,
				"Active",
				"Active",
				activeColor,
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

		this.measurePaneShift();
		// The view's FIRST render runs before its bands have laid out (pane box 0
		// or pre-font metrics), so the pass above measures nothing useful until
		// something re-triggers onResize. One second pass on the next frame
		// measures the settled layout; a hidden pane (box 0) keeps its last value
		// and the next onResize picks it up, as today.
		window.requestAnimationFrame(() => this.measurePaneShift());

		// Column widths come from live boxes; a render that raced the injected
		// stylesheet or webfont measured the fallback font and locked stale
		// widths in. Re-measure once fonts settle (Format-UI-Spacing:
		// "first-render metrics can predate the stylesheet") — but ONLY when fonts
		// aren't already loaded. Once they are (every render after the first), the
		// promise resolves a microtask later and the redundant re-equalize was an
		// extra reflow for nothing.
		if (document.fonts && document.fonts.status !== "loaded") {
			void document.fonts.ready.then(() => this.onResize());
		}

		// Version footer — appended LAST so it anchors to the bottom of the pane
		// content rather than sitting directly under the final section (W1).
		c.createDiv({ cls: "nkui-now-version", text: `v${this.plugin.manifest.version}` });
	}

	/** Publish the mobile/desktop vertical-centering shift (applyScreenShift)
	 * from the pane's measured box. (§ GG retired the squared count pill — the
	 * head count is now bare text, so there is no pill box left to measure;
	 * only the screen-shift measurement remains. Kept as one method so its call
	 * sites, which fire exactly when the pane geometry settles, still drive the
	 * centering; a hidden pane measures 0 and the next onResize re-runs.) */
	private measurePaneShift(): void {
		this.applyScreenShift(this.contentEl.getBoundingClientRect());
	}

	/** Screen-space centering (mobile): the For You pane doesn't fill the
	 * screen — the navbar and headers eat into it — so content centered to the
	 * PANE sits visibly low on the phone. Publish how far the pane's center
	 * sits from the SCREEN's center as --nkui-screen-shift on contentEl:
	 * 2 × (screen center − pane center) px, positive when the pane's center is
	 * above the screen's and the content must move DOWN. The stylesheet turns
	 * the var into spacer min-heights.
	 *
	 * The user's vertical-placement bias (settings.nowVerticalBias, a
	 * percent-ish share of the screen height; negative = higher) adds straight
	 * into the shift target. Desktop has no navbar offset to correct but still
	 * emits the BIAS as the same var, so the spacers honour the setting
	 * everywhere; a hidden pane (height 0) keeps its last value and the next
	 * onResize re-measures. */
	private applyScreenShift(pane: DOMRect): void {
		const bias = ((this.plugin.settings.nowVerticalBias ?? 0) / 100) * window.innerHeight;
		if (!Platform.isMobile) {
			this.contentEl.style.setProperty("--nkui-screen-shift", `${bias}px`);
			return;
		}
		if (pane.height <= 0) return;
		const shift = window.innerHeight / 2 - (pane.top + pane.height / 2);
		this.contentEl.style.setProperty("--nkui-screen-shift", `${2 * shift + bias}px`);
	}

	private renderBucket(
		parent: HTMLElement,
		id: string,
		label: string,
		color: string | null,
		entries: Entry[],
		rowOptsIn: RowOpts,
		defaultOpen = false,
		approvable = false
	): void {
		const b = parent.createDiv("nkui-now-group");
		b.toggleClass("is-collapsed", this.isCollapsed(id, defaultOpen));
		// The count is what actually needs the user — an approved gate awaiting
		// filing is done, so it drops out of the bubble (a section of only approved
		// gates reads 0).
		const needsUser = entries.filter((e) => !e.awaitingFiling).length;
		// A draft section's HEADER is its "approve all": hold the header to stamp
		// reviewed: true on every draft row here (a folded set's gate included,
		// cascading to its peers via group approval); a tap still folds/unfolds.
		// Drafts only: an awaiting-filing gate is already approved and left alone.
		const drafts = approvable ? entries.filter((e) => e.draft) : [];
		// § GG: the sum of folded children behind every still-pending gate in
		// this section — the "+N gated" the head's notification dot resolves to on
		// open. An approved (awaiting-filing) gate is settled, so it doesn't count.
		const gatedCount = entries
			.filter((e) => e.isGate && !e.awaitingFiling)
			.reduce((sum, e) => sum + (e.setCount ?? e.setFiles?.length ?? 0), 0);
		const head = this.bucketHead(
			b,
			id,
			label,
			color,
			needsUser,
			defaultOpen,
			drafts.length ? () => this.approveAll(drafts, b) : undefined,
			gatedCount
		);
		if (drafts.length) {
			const noun = drafts.length === 1 ? "draft" : "drafts";
			const hint = `Hold to approve all ${drafts.length} ${noun} in this section; tap to fold`;
			head.setAttr("aria-label", hint);
			head.setAttr("title", hint);
		}

		const wrap = b.createDiv("nkui-now-foldwrap");
		// nkui-now-colgrid: the list is the shared column grid its rows (nkui-cols)
		// subgrid onto — cross-row alignment by tracks, not measured widths.
		const list = wrap.createDiv("nkui-now-list nkui-now-colgrid");
		// An approvable section lets each non-gate draft row carry its own inline
		// "approve" hold (rendered in the gate column), so single drafts approve like
		// the gated sets do.
		const rowOpts = { ...rowOptsIn, approve: approvable };
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
		// The stylesheet scales the unroll duration by the row count.
		wrap.style.setProperty("--nkui-rows", String(list.childElementCount));
		this.equalizeMetaColumns(list);
	}

	/**
	 * Size each gate-trio column to its widest member, measured — not guessed in
	 * CSS. When a column's values are uniform the box equals the text exactly and
	 * every gap in the block is exactly one spacing step; when they differ, the
	 * slack is the true minimum. The spacing standard's "no magic numbers": the
	 * content defines the column.
	 *
	 * Scope is the SECTION (one bucket's list), deliberately: columns align
	 * within their section and sections stay independent, each sized to its
	 * own content. A view-wide pass was tried and reverted — reserving another
	 * section's wide column (the wait note) tore this section's pills apart
	 * (Format-UI-Columns-Are-Section-Scoped).
	 *
	 * Only the gate pair (+N, pill) inside the action cell still measures; the
	 * meta columns (type, age, pill slot) moved to the shared column grid
	 * (nkui-cols), which aligns by tracks instead of widths.
	 */
	private equalizeMetaColumns(scope: HTMLElement): void {
		// A slot column nobody in this list fills disappears entirely — left in
		// place it would read as a double gap between its neighbours.
		for (const cls of ["nkui-now-gateslot", "nkui-now-countslot", "nkui-now-pillslot"]) {
			const els = Array.from(scope.querySelectorAll<HTMLElement>(`.${cls}`));
			const empty = els.length > 0 && els.every((el) => el.childElementCount === 0);
			for (const el of els) el.toggleClass("nkui-now-slot-void", empty);
		}
		// Mobile: don't lock columns to measured widths. A narrow viewport needs the
		// meta to flow and wrap (the CSS stacks an awaiting-filing row so its
		// "approved — awaiting filing" note lands on its own line rather than being
		// pushed off the right edge). Clear any width a prior desktop-width render
		// left behind and bail before the measuring pass.
		if (Platform.isMobile) {
			for (const cls of ["nkui-now-gateslot", "nkui-now-countslot"]) {
				for (const el of Array.from(scope.querySelectorAll<HTMLElement>(`.${cls}`))) el.style.width = "";
			}
			return;
		}
		// Equalize each column to its widest member — in THREE batched phases (clear
		// all → measure all → assign all), never read-after-write per row. Interleaving
		// a width write with a getBoundingClientRect read forces a synchronous reflow on
		// every iteration; over several columns × N rows that layout-thrash is the visible
		// "stutter, dots then text" on the Active section. Batched, the browser does one
		// layout flush for the whole measuring pass instead of dozens.
		const groups = ["nkui-now-gateslot", "nkui-now-countslot"]
			.map((cls) => Array.from(scope.querySelectorAll<HTMLElement>(`.${cls}`)))
			.filter((els) => els.length >= 2);
		// Phase 1 — clear every width (writes only).
		for (const els of groups) for (const el of els) el.style.width = "";
		// Phase 2 — measure every column (reads only; a single layout flush). Exact
		// sub-pixel width: rounding up widens the box past the text and the slack reads
		// as an unequal gap beside an 8px neighbour.
		const widths = groups.map((els) => Math.max(...els.map((el) => el.getBoundingClientRect().width)));
		// Phase 3 — assign each column its width (writes only). A column measuring 0 (a
		// hidden background tab) is left natural; onResize re-runs when it's visible.
		groups.forEach((els, i) => {
			if (widths[i] <= 0) return;
			for (const el of els) el.style.width = `${widths[i].toFixed(2)}px`;
		});
	}

	/** Re-measure each section's columns when the view's geometry changes —
	 * covers a render that happened while the tab was hidden (all widths
	 * measure 0 then). Per-list, matching the section scope of the columns. */
	onResize(): void {
		for (const list of Array.from(this.contentEl.querySelectorAll<HTMLElement>(".nkui-now-list"))) {
			this.equalizeMetaColumns(list);
		}
		// Also fires when a hidden leaf becomes visible — the render-time pass
		// measures 0 on a background tab, so this is where the centering shift
		// re-measures.
		this.measurePaneShift();
	}

	/** Decide bucket — each open decision is one entry; the count is OPEN decisions.
	 * It is a configured user-interaction surface, so it carries the queue styling
	 * and sits at the top. Resolved decisions move to the Waiting section. */
	private renderDecideBucket(parent: HTMLElement, decisions: Decision[]): void {
		const id = "Queue/decide";
		const b = parent.createDiv("nkui-now-group nkui-now-group-queue");
		b.toggleClass("is-collapsed", this.isCollapsed(id, true));
		this.bucketHead(b, id, "Decide", "var(--nkui-decide-color, var(--interactive-accent))", decisions.length, true);

		const wrap = b.createDiv("nkui-now-foldwrap");
		const list = wrap.createDiv("nkui-now-list");
		for (const d of decisions) this.renderDecision(list, d);
		// The stylesheet scales the unroll duration by the row count.
		wrap.style.setProperty("--nkui-rows", String(list.childElementCount));
	}

	/** A resolved decision: approved in the file, not yet executed. Renders in the
	 * shared reduced shape inside the Waiting section; unchecking re-opens it. */
	private renderResolvedDecision(list: HTMLElement, d: Decision): void {
		const path = this.plugin.settings.userQueuePath;
		const picked = d.options.find((o) => o.state === "x" || o.state === "X");
		if (!picked) return;
		const label = d.title ? `${plainText(d.title)} — ${plainText(picked.text)}` : plainText(picked.text);
		// No per-row "awaiting action agent" note — the Waiting sub-header names
		// the agent once for the whole group. No hold either: the only write here
		// is the uncheck, and that stays on the checkbox.
		renderReducedRow(list, {
			title: label,
			struck: true,
			checkbox: {
				checked: true,
				onChange: async () => {
					await this.setItemChecked(path, picked.text, false);
					await this.reloadAndRender();
				},
			},
			onOpen: (newLeaf) => this.openPath(path, newLeaf, d.title ? `#${d.title}` : undefined),
			ariaLabel: "Approved — the agent executes and clears this on its next pass. Uncheck to re-open.",
		});
	}

	private renderDecision(list: HTMLElement, d: Decision): void {
		const path = this.plugin.settings.userQueuePath;
		const card = list.createDiv("nkui-now-decision");
		// Open the queue at this decision's heading, so a long queue lands you on
		// the item itself rather than the top of the file.
		const openQueue = () => this.openPath(path, false, d.title ? `#${d.title}` : undefined);
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
			row.createSpan({ cls: "nkui-now-needsread-text", text: "Read-only — open to read" });
			row.addEventListener("click", openQueue);
			attachKeyActivate(row, openQueue);
			// A non-choice notification has nothing to pick — the default action is to
			// acknowledge it: a checked line is written under its heading so it
			// resolves like a picked decision (dims, then the action-agent clears it
			// next pass). Needs a heading to locate the write target.
			if (d.title) {
				const ack = row.createEl("button", { cls: "nkui-now-ackbtn", text: "Acknowledge" });
				ack.setAttr("aria-label", "Acknowledge — cross this off; the agent clears it on its next pass");
				ack.addEventListener("click", (ev) => {
					ev.stopPropagation();
					void this.fadeThen(card, () => this.acknowledge(path, d));
				});
			}
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
					void this.fadeThen(card, () =>
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
				void this.fadeThen(card, () => this.pickOption(path, o.text))
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
		opts: { items: QueueItem[]; drops: OutboxDrop[]; path: string; defaultOpen: boolean }
	): void {
		// A drop is a queued ask in file form, so it counts toward the bubble.
		const openCount = opts.items.filter((i) => !i.done).length + opts.drops.length;
		const b = parent.createDiv("nkui-now-group nkui-now-group-queue");
		b.toggleClass("is-collapsed", this.isCollapsed(id, opts.defaultOpen));
		this.bucketHead(b, id, label, color, openCount, opts.defaultOpen);

		const wrap = b.createDiv("nkui-now-foldwrap");
		const list = wrap.createDiv("nkui-now-list");
		for (const item of opts.items) {
			renderMachineItemRow(list, item, {
				onSkip: (it) => void this.skipMachineItem(it),
				onEdit: (main, it) => this.editMachineItem(main, it),
			});
		}
		// Loose drops beside the queue file — one row each, after the checklist.
		for (const d of opts.drops) this.renderDropRow(list, d);
		if (!opts.items.length && !opts.drops.length) {
			list.createDiv({ cls: "nkui-now-empty", text: "Nothing queued." });
		}
		this.machineAddInput = renderAddTaskBox(list, (text) => void this.addMachineItem(text));
		// The stylesheet scales the unroll duration by the row count.
		wrap.style.setProperty("--nkui-rows", String(list.childElementCount));
	}

	/** One outbox drop — a loose file (or folder) in the queue folder. The row
	 * shows the name and opens the file on click; a folder row opens its first
	 * file (chosen over reveal-in-explorer: no private explorer API needed). */
	private renderDropRow(list: HTMLElement, d: OutboxDrop): void {
		const row = list.createDiv("nkui-now-qrow nkui-now-qdrop");
		this.applyTint(row, d.type);
		setIcon(row.createSpan("nkui-now-qdropicon"), d.folder ? "folder" : "file");
		row.createSpan({ cls: "nkui-now-qdropname", text: d.name });
		const aria = d.folder
			? "A folder dropped in the outbox — click to open its first file"
			: "A file dropped in the outbox — click to open it";
		row.setAttr("aria-label", aria);
		row.setAttr("title", aria);
		const open = d.open;
		if (!open) return;
		row.setAttr("role", "button");
		row.setAttr("tabindex", "0");
		row.addEventListener("click", (ev) => this.openPath(open.path, ev.ctrlKey || ev.metaKey));
		attachKeyActivate(row, () => this.openPath(open.path, false));
		this.attachPreview(row, open, d.type);
	}

	/** THE outbox — the machine queue's own folder (the queue-anchored fact),
	 * not a guess at nowQueueFolders[0]; the configured queue folders stay for
	 * any additional queue locations. A root-level queue file (no parent
	 * folder) falls back to the first configured queue folder. */
	private outboxPath(): string {
		const p = this.plugin.settings.machineQueuePath;
		const i = p.lastIndexOf("/");
		if (i > 0) return p.slice(0, i);
		return this.plugin.settings.nowQueueFolders[0] ?? "";
	}

	/** Loose files and folders dropped in the outbox, excluding the
	 * machine-queue file itself. A folder is one row standing for
	 * everything inside it. */
	private collectOutboxDrops(): OutboxDrop[] {
		const s = this.plugin.settings;
		const root = this.app.vault.getAbstractFileByPath(this.outboxPath());
		if (!(root instanceof TFolder)) return [];
		const drops: OutboxDrop[] = [];
		for (const child of root.children) {
			if (child instanceof TFile) {
				if (child.path === s.machineQueuePath) continue;
				const fm = this.app.metadataCache.getFileCache(child)?.frontmatter;
				const type = fm?.[s.typeField] != null ? String(fm[s.typeField]) : null;
				drops.push({ name: this.displayName(child), folder: false, open: child, type });
			} else if (child instanceof TFolder) {
				drops.push({ name: child.name, folder: true, open: firstFileIn(child), type: null });
			}
		}
		drops.sort((a, b) => a.name.localeCompare(b.name));
		return drops;
	}

	/** Shared bucket header (count bubble + label, foldable). With `holdCommit`
	 * the header itself is a hold: tap folds, press-and-hold commits (the
	 * approve-all redesign — no separate button target), arming the section's
	 * rows while held. */
	private bucketHead(
		bucket: HTMLElement,
		id: string,
		label: string,
		color: string | null,
		count: number,
		defaultOpen: boolean,
		holdCommit?: () => void | Promise<void>,
		gatedCount = 0
	): HTMLElement {
		// An empty section (count 0 — an all-approved drafts bucket, a queue with
		// nothing open, only-resolved decisions) carries nothing the user must act
		// on. Tag it so the stylesheet recedes it (the group dims; the count pill
		// keeps its full size — only the GROUP class changes, never a pill class):
		// a quiet "0" that never competes with a live count for attention.
		bucket.toggleClass("nkui-now-group-empty", count <= 0);
		// A COLLAPSED quiet surface (the Waiting group, or a closed queue-ish
		// section — Decide / Queue) renders diminished like an empty one; the
		// stylesheet keys the look off `is-diminished`. Re-synced on every fold
		// toggle below.
		const syncDiminished = (): void => {
			const quiet =
				bucket.hasClass("nkui-now-group-queue") || bucket.hasClass("nkui-now-group-waiting");
			bucket.toggleClass("is-diminished", quiet && bucket.hasClass("is-collapsed"));
		};
		syncDiminished();
		// Open-section emphasis colour: the section's own pill (type) colour. The
		// stylesheet applies it only while the section is unfolded. When it's a real
		// type hex, derive its sub-tones too, so the section header + "approve all"
		// follow the canon hue rather than a mixed-toward-white wash.
		bucket.style.setProperty("--nkui-section-color", color ?? "var(--interactive-accent)");
		if (color && color.startsWith("#")) {
			for (const [k, v] of Object.entries(toneVars(color))) bucket.style.setProperty(k, v);
		}
		const gh = bucket.createDiv("nkui-now-group-head");
		gh.setAttr("role", "button");
		gh.setAttr("tabindex", "0");
		const cnt = gh.createSpan({ cls: "nkui-now-count" });
		// Inner span wraps the digit so a CSS transform: scale() can animate the
		// glyph on a count change without disturbing the surrounding layout.
		cnt.createSpan({ cls: "nkui-now-count-digit", text: String(count) });
		// The count is a square pill (§ GG): a real type colour fills it inline; a
		// colour-less section (Decide / Queue / Waiting) takes the muted neutral pill.
		if (color) cnt.style.background = color;
		else cnt.addClass("nkui-now-count-neutral");
		gh.createSpan({ cls: "nkui-now-group-title", text: label });
		// Notification pip (§ GG): a populated section that is NOT the settled
		// Waiting group carries a dot at the head's right edge while collapsed —
		// the file-explorer "this has something" mark. On open the dot gives way
		// in place to a quiet "+N gated" label when the set actually holds folded
		// children (N = their sum); a set with none simply drops the dot on open.
		if (count > 0 && !bucket.hasClass("nkui-now-group-waiting")) {
			const pip = gh.createSpan({ cls: "nkui-now-gatepip" });
			pip.createSpan({ cls: "nkui-now-gatepip-dot" });
			if (gatedCount > 0) {
				pip.createSpan({ cls: "nkui-now-gatepip-label", text: `+${gatedCount} gated` });
			}
		}

		const toggle = () => {
			const collapsed = bucket.classList.toggle("is-collapsed");
			this.setCollapsed(id, collapsed, defaultOpen);
			syncDiminished();
		};
		if (holdCommit) {
			// The header is the approve-all control: a tap folds (today's gesture),
			// the hold commits. While held the section gains `is-arming`, lighting
			// the candidate rows in their type colour. `nkui-row-hold` marks every
			// holdable surface for the stylesheet's row-sweep fill.
			gh.addClass("nkui-row-hold");
			attachHold(gh, {
				onCommit: () => {
					// A collapsed section hides its candidate rows — a hold there
					// unfolds instead of committing blind (the old "approve all?"
					// control hid itself while folded).
					if (bucket.hasClass("is-collapsed")) {
						toggle();
						return;
					}
					return holdCommit();
				},
				onTap: toggle,
				armClass: "is-arming",
				armTarget: bucket,
				keyHold: true,
			});
		} else {
			gh.addEventListener("click", toggle);
			gh.addEventListener("keydown", (ev) => {
				if (ev.key === "Enter" || ev.key === " ") {
					ev.preventDefault();
					toggle();
				}
			});
		}
		return gh;
	}

	/** Stamp reviewed: true on each draft via the frontmatter-safe API. Then the
	 * exit beat: the section folds its list closed (`is-settling`, the foldwrap's
	 * grid-rows pattern at --nkui-settle pace) so the committed rows visibly
	 * leave before the repaint removes them — not an instant snap that shoves
	 * the next section up under the cursor. */
	private async approveAll(drafts: Entry[], bucket: HTMLElement | null): Promise<void> {
		const field = this.plugin.settings.reviewedField;
		for (const e of drafts) {
			await this.app.fileManager.processFrontMatter(e.file, (fm) => {
				fm[field] = true;
			});
		}
		await this.settle(bucket);
		await this.reloadAndRender();
	}

	/** Waiting — settled-by-you items pending an agent, in TWO labelled sub-groups
	 * under small quiet headers: "filing agent" (approved gates/files awaiting
	 * filing) and "action agent" (resolved decisions awaiting execution). The
	 * sub-header names the agent once, so the rows drop their repeated per-row
	 * agent notes. One quiet neutral-grey section, default collapsed, gathered
	 * right above Active. */
	private renderWaitingSection(parent: HTMLElement, waiting: Entry[], resolved: Decision[]): void {
		const id = "Waiting";
		const b = parent.createDiv("nkui-now-group nkui-now-group-waiting");
		b.toggleClass("is-collapsed", this.isCollapsed(id, false));
		this.bucketHead(b, id, "Waiting", null, waiting.length + resolved.length, false);
		const wrap = b.createDiv("nkui-now-foldwrap");
		// The waiting list shares the column-grid contract so every row's cells
		// land in the same tracks (no measured widths).
		const list = wrap.createDiv("nkui-now-list nkui-now-colgrid");
		if (waiting.length) {
			list.createDiv({ cls: "nkui-now-subhead", text: "filing agent" });
			for (const e of waiting) this.renderWaitingGate(list, e);
		}
		if (resolved.length) {
			list.createDiv({ cls: "nkui-now-subhead", text: "action agent" });
			for (const d of resolved) this.renderResolvedDecision(list, d);
		}
		// The stylesheet scales the unroll duration by the row count.
		wrap.style.setProperty("--nkui-rows", String(list.childElementCount));
	}

	/** An approved waiting set: the gate (or lone approved file), folded with a
	 * passive "+N" marking its members. The ROW is the control: hold to undo —
	 * sends the gate and its members back to drafts (Needs-you); a tap unfolds
	 * the gated member files beneath the row (a tap on the title, or a row with
	 * no members, opens the file instead). The sub-header above names the agent,
	 * so the row carries no per-row wait note. */
	private renderWaitingGate(list: HTMLElement, e: Entry): void {
		const row = list.createDiv("nkui-now-reducedrow nkui-now-waitgate nkui-cols nkui-row-hold");
		// A waiting child keeps its kind's tint, like every top-level row.
		this.applyTint(row, e.type);
		const members = !!e.setFiles?.length;
		const aria = members
			? "Approved — the filing-agent files this set next pass. Hold to undo — sends the set back to drafts. Tap to peek at the set."
			: "Approved — the filing-agent files this next pass. Hold to undo — sends it back to drafts. Tap to open.";
		row.setAttr("aria-label", aria);
		row.setAttr("title", aria);
		row.setAttr("role", "button");
		row.setAttr("tabindex", "0");
		const title = row.createSpan({
			cls: "nkui-now-reducedtitle nkui-now-reducedrow-click nkui-col-main",
			text: this.displayName(e.file),
		});
		// A passive "+N" marks the folded set (the old "undo +N" control is gone —
		// the row itself holds to undo).
		if (e.setCount) {
			const n = row.createSpan({ cls: "nkui-now-setcount nkui-col-action", text: `+${e.setCount}` });
			n.setAttr("aria-label", `${e.setCount} more in this set`);
		}
		attachHold(row, {
			onCommit: () => this.unapproveSet(e, row),
			onTap: (ev) => {
				// Title taps open the gate document (what title clicks always did);
				// anywhere else on a gated row peeks at the folded set.
				const onTitle = ev?.target instanceof Node && title.contains(ev.target);
				if (members && !onTitle) this.toggleGateMembers(row, e);
				else this.openPath(e.file.path, ev ? ev.ctrlKey || ev.metaKey : false);
			},
			keyHold: true,
		});
		// The hold owns the long-press here — no mobile long-press preview, or the
		// popover would fire mid-arm; desktop right-click preview stays.
		this.attachPreview(row, e.file, e.type, false);
		// A persisted unfold survives the re-render (expandedGates) — re-open it.
		if (members && expandedGates.has(e.file.path)) this.openGateMembers(row, e);
	}

	/** Send an approved waiting set back to Needs-you: clear `reviewed` on the gate
	 * and every member it gated, dropping the `auto-reviewed` tag the cascade added.
	 * Members still drafts are left as-is. The committed row settles for a beat
	 * before the repaint moves it. */
	private async unapproveSet(e: Entry, row?: HTMLElement): Promise<void> {
		const field = this.plugin.settings.reviewedField;
		for (const f of [e.file, ...(e.setFiles ?? [])]) {
			await this.app.fileManager.processFrontMatter(f, (fm) => {
				if (fm[field] === true || fm[field] === "true") fm[field] = false;
				if (Array.isArray(fm.tags)) {
					const kept = fm.tags.filter((t: unknown) => t !== "auto-reviewed");
					if (kept.length !== fm.tags.length) fm.tags = kept;
				}
			});
		}
		await this.settle(row);
		await this.reloadAndRender();
	}

	/** Native page-preview on demand: right-click (desktop) or long-press (mobile)
	 * fires Obsidian's own hover-link, so the core Page Preview plugin shows its
	 * popover — no bespoke window to manage. A no-op when that plugin is off.
	 * `longPress: false` keeps only the right-click path — a row whose long-press
	 * already commits a hold must not also pop the preview mid-arm. */
	private attachPreview(el: HTMLElement, file: TFile, type?: string | null, longPress = true): void {
		const fire = (event: Event): void => {
			this.app.workspace.trigger("hover-link", {
				event,
				source: NOW_VIEW_TYPE,
				hoverParent: this,
				targetEl: el,
				linktext: file.path,
				sourcePath: file.path,
			});
			this.stampPopover(type ?? null);
		};
		el.addEventListener("contextmenu", (ev) => {
			ev.preventDefault();
			fire(ev);
		});
		if (!longPress) return;
		let timer: number | undefined;
		const clear = (): void => {
			if (timer) {
				window.clearTimeout(timer);
				timer = undefined;
			}
		};
		el.addEventListener(
			"touchstart",
			(ev) => {
				clear();
				timer = window.setTimeout(() => fire(ev), 500);
			},
			{ passive: true }
		);
		el.addEventListener("touchmove", clear, { passive: true });
		el.addEventListener("touchend", clear);
		el.addEventListener("touchcancel", clear);
	}

	/** Tint the page-preview popover by the previewed note's type: the core popover
	 * carries no type class, so stamp the freshly-created one (it appears a beat after
	 * the hover-link fires) with the type class + colour, which the stylesheet keys
	 * off to give the popover the note's accent. */
	private stampPopover(type: string | null): void {
		if (!type) return;
		const color = this.colorFor(type);
		if (!color) return;
		window.setTimeout(() => {
			const pops = Array.from(document.body.querySelectorAll<HTMLElement>(".popover.hover-popover"));
			const pop = pops[pops.length - 1];
			if (!pop || pop.classList.contains("nkui-typed")) return;
			pop.addClass("nkui-typed");
			pop.addClass(typeClass(type));
			pop.style.setProperty("--nkui-type-color", color);
		}, 80);
	}

	/** Single tap on a gate row (Needs or Waiting): unfold the gated member files
	 * inline beneath the row; tap again to fold. The hold (approve / undo) is
	 * unchanged — the tap only reveals what the hold would commit. The unfold
	 * PERSISTS across re-renders for the session (expandedGates, keyed by gate
	 * path), so visiting a member file no longer folds the set behind you. */
	private toggleGateMembers(row: HTMLElement, e: Entry): void {
		const next = row.nextElementSibling;
		if (next instanceof HTMLElement && next.hasClass("nkui-now-gatemembers")) {
			expandedGates.delete(e.file.path);
			this.foldGateMembers(next);
			return;
		}
		if (!(e.setFiles ?? []).length) return;
		expandedGates.add(e.file.path);
		this.openGateMembers(row, e);
	}

	/** Build and unfold the member box beneath a gate row (the DOM half of
	 * toggleGateMembers — also replayed on render for a persisted unfold). */
	private openGateMembers(row: HTMLElement, e: Entry): void {
		const files = e.setFiles ?? [];
		if (!files.length) return;
		const box = createDiv("nkui-now-gatemembers");
		// The stylesheet scales the unroll duration by the row count.
		box.style.setProperty("--nkui-rows", String(files.length));
		// The grid-rows fold pattern (the foldwrap's 0fr ⇄ 1fr) needs an inner
		// box to clip: members live in it, the outer element animates the track.
		const inner = box.createDiv("nkui-now-gatemembers-inner");
		for (const f of files) {
			const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
			const type = fm?.[this.plugin.settings.typeField] != null
				? String(fm[this.plugin.settings.typeField])
				: null;
			const m = inner.createDiv("nkui-now-gatemember");
			this.applyTint(m, type);
			m.setAttr("role", "button");
			m.setAttr("tabindex", "0");
			m.createSpan({ cls: "nkui-now-gatemember-name", text: this.displayName(f) });
			m.addEventListener("click", (ev) => this.openPath(f.path, ev.ctrlKey || ev.metaKey));
			attachKeyActivate(m, () => this.openPath(f.path, false));
			this.attachPreview(m, f, type);
		}
		row.insertAdjacentElement("afterend", box);
		// Unfold on the NEXT frame so the inserted box transitions 0fr → 1fr
		// instead of appearing at full height; with animations off the
		// stylesheet's body:not(.nkui-anim) gate makes the same flip instant.
		window.requestAnimationFrame(() => box.addClass("is-open"));
	}

	/** Fold an open member list with the same grid-rows transition, detaching it
	 * only after the close has played (transitionend, with a timeout fallback —
	 * a hidden pane fires no transition events). Animations off → instant. */
	private foldGateMembers(box: HTMLElement): void {
		if (!document.body.hasClass("nkui-anim") || !box.hasClass("is-open")) {
			box.remove();
			return;
		}
		box.removeClass("is-open");
		let detached = false;
		const detach = (): void => {
			if (detached) return;
			detached = true;
			box.remove();
		};
		box.addEventListener("transitionend", detach, { once: true });
		window.setTimeout(detach, 600);
	}

	/** Stamp an element with its type tint — --nkui-row-color plus the derived
	 * tone vars — so it takes the same title colour and hover wash as a
	 * top-level row. A typeless element is left to the theme defaults. */
	private applyTint(el: HTMLElement, type: string | null | undefined): void {
		const rc = type ? this.colorFor(type) : null;
		if (!rc) return;
		// The class keys the tinted hover wash (the stylesheet cannot select on
		// an inline custom property); a colour-less row keeps the neutral hover.
		el.addClass("nkui-tinted");
		el.style.setProperty("--nkui-row-color", rc);
		for (const [k, v] of Object.entries(toneVars(rc))) el.style.setProperty(k, v);
	}

	/** Approve a gate (stamp reviewed: true) — group approval cascades to its peers
	 * via the filing-agent; the set drops to Waiting next render. The committed
	 * row settles (fades) for a beat before the repaint moves it. */
	private async approveGate(e: Entry, row?: HTMLElement): Promise<void> {
		const field = this.plugin.settings.reviewedField;
		await this.app.fileManager.processFrontMatter(e.file, (fm) => {
			fm[field] = true;
		});
		await this.settle(row);
		await this.reloadAndRender();
	}

	private renderRow(list: HTMLElement, e: Entry, opts: RowOpts, contained = false): void {
		const row = list.createDiv("nkui-now-row nkui-cols");
		if (contained) row.addClass("nkui-now-row-contained");
		if (e.isGate) row.addClass("nkui-now-row-gate");
		// Every row carries its type colour as --nkui-row-color: the stylesheet tints
		// the row title with it (like the explorer file names), and the approve-all
		// header hold lights the draft candidates up in their own colour.
		this.applyTint(row, e.type);
		if (e.draft) row.addClass("nkui-now-row-draft");
		row.setAttr("role", "button");
		row.setAttr("tabindex", "0");
		const open = (newLeaf: boolean) => this.openPath(e.file.path, newLeaf);
		// In an approvable section every draft row IS its approve control: hold
		// the row to commit (the gate cascade or the single-file stamp); a tap
		// keeps today's gestures — a gate row peeks at its set, a file row opens
		// its file. Non-draft rows (Active, queued, untyped) keep the plain click.
		const canHold = !!opts.approve && e.draft;
		if (!canHold) {
			row.addEventListener("click", (ev) => open(ev.ctrlKey || ev.metaKey));
			attachKeyActivate(row, () => open(false));
		}
		// A holdable row's long-press is the commit — no mobile long-press preview.
		this.attachPreview(row, e.file, e.type, !canHold);

		// Main cell — dot + title. The action and meta cells land in the section
		// grid's shared tracks (the nkui-cols contract), aligned across rows.
		const main = row.createSpan("nkui-col-main");
		let titleDim = 1;
		if (opts.showRowDot) {
			const dot = main.createSpan("nkui-now-dot");
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
				// The title fades to MATCH the dot's emphasis, on a higher floor so it
				// stays readable.
				titleDim = 1 - 0.45 * t;
			}
		}

		const titleEl = main.createSpan({ cls: "nkui-now-rowtitle", text: this.displayName(e.file) });
		if (titleDim < 1) titleEl.style.opacity = titleDim.toFixed(2);

		if (canHold) {
			// The ROW is the approve control (no per-row button targets — the
			// small pills kept getting mis-pressed on a phone). `nkui-row-hold`
			// marks it for the stylesheet's whole-row hold fill.
			row.addClass("nkui-row-hold");
			const peekable = e.isGate && !!e.setFiles?.length;
			const hint = e.isGate
				? "Hold to approve the gate — approving it approves the rest of the set. Tap to peek at the set; tap the title to open it."
				: "Hold to approve this draft (reviewed: true). Tap to open.";
			row.setAttr("aria-label", hint);
			row.setAttr("title", hint);
			attachHold(row, {
				onCommit: () => this.approveGate(e, row),
				onTap: (ev) => {
					// A gate row's tap unfolds/folds its gated children — except on
					// the title, which keeps its open-the-document click. A plain
					// file row's tap opens the file, like its title always did.
					const onTitle = ev?.target instanceof Node && titleEl.contains(ev.target);
					if (peekable && !onTitle) this.toggleGateMembers(row, e);
					else open(ev ? ev.ctrlKey || ev.metaKey : false);
				},
				keyHold: true,
			});
		}

		// The gate trio renders as slot columns sized per section, packed against
		// the right edge with the constant-width gate pill OUTERMOST: the pill is
		// the same thing in every section, so it anchors the same x everywhere,
		// while the variable-width "+N" stacks inward — no section ever reserves
		// a column another section's content created.
		if (e.isGate || e.setCount) {
			const set = row.createSpan("nkui-now-rowset nkui-col-action");

			// A gate stands in for a folded working-set: a quiet "+N" marks how
			// many working notes sit behind it, without listing them.
			const cslot = set.createSpan("nkui-now-countslot");
			// A gate carries its own "+N" in its badge label, so only a non-gate
			// folded head shows the standalone count here.
			if (e.setCount && !e.isGate) {
				const n = cslot.createSpan({ cls: "nkui-now-setcount", text: `+${e.setCount}` });
				n.setAttr("aria-label", `${e.setCount} more in this set`);
			}

			// The gate file is the one document to actually read — approving it
			// auto-approves its peers. The badge makes that dependency visible.
			// PASSIVE now: the row itself carries the hold and the tap.
			const gslot = set.createSpan("nkui-now-gateslot");
			if (e.isGate) {
				const label = e.setCount ? `+${e.setCount} gated` : "gated";
				gslot.createSpan({ cls: "nkui-now-gatepill", text: label });
			}
		}

		// A persisted unfold survives the re-render (expandedGates) — re-open it.
		if (e.isGate && e.setFiles?.length && expandedGates.has(e.file.path)) {
			this.openGateMembers(row, e);
		}

		const showType = opts.showType && !!e.type;
		if (showType || opts.showAge || (opts.showDraft && e.draft)) {
			// One meta cell of columns — type, age, pill slot — subgridded onto the
			// section grid's meta tracks, so the block is the same width on every
			// row and every internal gap is the same spacing step. The constant-
			// width pill anchors the outer edge (Format-UI-Columns-Are-Section-
			// Scoped); its slot always renders (empty when not a draft) and
			// collapses only section-wide-empty.
			const meta = row.createSpan("nkui-now-rowmeta nkui-col-meta");
			if (showType) meta.createSpan({ cls: "nkui-now-metatype", text: e.type as string });
			if (opts.showAge) {
				meta.createSpan({ cls: "nkui-now-metaage", text: relAge(e.activity ?? e.file.stat.mtime) });
			}
			if (opts.showDraft) {
				const slot = meta.createSpan("nkui-now-pillslot");
				if (e.draft) slot.createSpan({ cls: "nkui-now-draftpill", text: "draft" });
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
		return parseQueueItems(await this.app.vault.cachedRead(f));
	}

	/**
	 * Toggle a checkbox by matching its text, not a stored line index — so a
	 * pick is robust to the file shifting between the read and the write (e.g.
	 * the action-agent clearing a resolved item in between).
	 */
	private async setItemChecked(path: string, text: string, checked: boolean): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return;
		await queueWrites.setChecked(this.app.vault, f, text, checked);
	}

	/** Approve one option of a decision — writes [x] to its line, resolving the decision. */
	/** Soften the card before the write re-renders it into its resolved row, so
	 * the pick reads as a transition rather than a snap. */
	private async fadeThen(card: HTMLElement, write: () => Promise<void>): Promise<void> {
		card.addClass("is-resolving");
		await new Promise((r) => setTimeout(r, 78));
		await write();
	}

	private async pickOption(path: string, text: string): Promise<void> {
		await this.setItemChecked(path, text, true);
		await this.reloadAndRender();
	}

	/** Approve a fill-in option: replace its placeholder text and check it in one write. */
	private async pickOptionFilled(path: string, rawText: string, filledText: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (f instanceof TFile) await queueWrites.pickOptionFilled(this.app.vault, f, rawText, filledText);
		await this.reloadAndRender();
	}

	/** Acknowledge a non-choice notification — the shared write appends the
	 * sanctioned option text checked under its heading, so it parses as a
	 * resolved decision (the dimmed, struck "awaiting action agent" row) and
	 * the action-agent clears it next pass. */
	private async acknowledge(path: string, d: Decision): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (f instanceof TFile) await queueWrites.acknowledge(this.app.vault, f, d);
		await this.reloadAndRender();
	}

	/** Append a new option line beneath a decision's heading. */
	private async addOption(path: string, d: Decision, text: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return;
		await queueWrites.appendOption(this.app.vault, f, d, text);
		await this.reloadAndRender();
	}

	/** Cross off / restore a machine-queue item — the shared skip semantics
	 * (the "*(item skipped)*" marker beside the [x]). */
	private async skipMachineItem(item: QueueItem): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(this.plugin.settings.machineQueuePath);
		if (!(f instanceof TFile)) return;
		await queueWrites.toggleSkip(this.app.vault, f, item);
		await this.reloadAndRender();
	}

	private async addMachineItem(text: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(this.plugin.settings.machineQueuePath);
		if (!(f instanceof TFile)) return;
		await queueWrites.appendTask(this.app.vault, f, text);
		// Surface the new task at once — straight to reloadAndRender, which the
		// focused-field guard can't defer (the guard lives only in scheduleRender).
		// The rebuild destroys the focused add box, so re-focus its re-created
		// twin and re-fit its height: users type tasks in a run.
		await this.reloadAndRender();
		const box = this.machineAddInput;
		if (box) {
			autoGrow(box);
			// Desktop types tasks in a run — re-focus the re-created box. Mobile
			// must NOT: re-focusing re-summons the just-dismissed iOS keyboard.
			if (!Platform.isMobile) box.focus();
		}
	}

	/** Replace an open task's text in place, re-located by its current text (robust
	 * to the file shifting), keeping its checkbox state. */
	private async updateMachineItem(oldText: string, newText: string): Promise<void> {
		const f = this.app.vault.getAbstractFileByPath(this.plugin.settings.machineQueuePath);
		if (!(f instanceof TFile)) return;
		await queueWrites.updateTask(this.app.vault, f, oldText, newText);
		await this.reloadAndRender();
	}

	/** Turn an open task row back into a live editor: a wrapping textarea seeded with
	 * the task text. Enter saves, Esc or blur cancels. */
	private editMachineItem(main: HTMLElement, item: QueueItem): void {
		main.empty();
		const ta = main.createEl("textarea", {
			cls: "nkui-now-qaddinput nkui-now-qedit-field",
			attr: { rows: "1" },
		});
		ta.value = item.text;
		ta.addEventListener("input", () => autoGrow(ta));
		// Measure after layout so scrollHeight is real, then focus at the end.
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
			if (v && v !== item.text) void this.updateMachineItem(item.text, v);
			// Nothing changed in the FILE, but the row's DOM was emptied for the
			// editor — force past the signature bail to rebuild it.
			else void this.reloadAndRender(true);
		};
		ta.addEventListener("keydown", (ev) => {
			if (ev.key === "Enter" && !ev.shiftKey) {
				ev.preventDefault();
				save();
			} else if (ev.key === "Escape") {
				ev.preventDefault();
				done = true;
				void this.reloadAndRender(true); // same: restore the emptied row
			}
		});
		ta.addEventListener("blur", save);
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
		// CONFIG § Types row order is THE type order — the same order the
		// explorer floats files by — so the For You sections and the file pane
		// never disagree. The palette list is the CONFIG-less fallback.
		const idx = this.configTypeIndex();
		const rank = (k: string): number => {
			const i = idx.get(k);
			if (i !== undefined) return i;
			if (k === "queued") return 9000;
			if (k === "untyped") return 9002;
			return 9001;
		};
		// Name tiebreak: several types outside CONFIG all rank 9001 — without it
		// their section order depended on map insertion order (a shuffle source).
		return keys.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
	}

	/** type → CONFIG § Types row index (palette order when CONFIG is absent). */
	private configTypeIndex(): Map<string, number> {
		const types = this.plugin.kitFacts?.types?.length
			? this.plugin.kitFacts.types
			: this.plugin.typeStyles().map((t) => t.type);
		return new Map(types.map((t, i) => [t, i]));
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

	/** The archive root to exclude: CONFIG's `<archive>` literal when the kit
	 * facts carry one, else the root folder whose name (numeric prefix
	 * stripped) reads "archive" — so a plain-name install ("Archive") excludes
	 * correctly too — else the kit default. */
	private archiveRoot(): string {
		const lit = this.plugin.kitFacts?.archiveLiteral;
		if (lit) return lit;
		for (const child of this.app.vault.getRoot().children) {
			if (child instanceof TFolder && /^archive$/i.test(child.name.replace(/^\d+-/, ""))) {
				return child.name;
			}
		}
		return ARCHIVE_FALLBACK;
	}

	private collect(): { needs: Entry[]; active: Entry[]; waiting: Entry[] } {
		const s = this.plugin.settings;
		const activeTypes = new Set(s.nowActiveTypes);
		const queueFiles = new Set([s.userQueuePath, s.machineQueuePath]);
		const archive = this.archiveRoot();

		const needs: Entry[] = [];
		const active: Entry[] = [];
		// Approved gates awaiting the filing-agent (settled by the user, pending an
		// agent) are collected apart from `needs` and surfaced in their own quiet
		// Waiting section above Active — never scattered as 0-count type groups.
		const waiting: Entry[] = [];
		// Inbox working-sets: a container (a subfolder of the inbox) with a resolvable
		// gate folds to ONE row — the gate, the document the user reads — carrying a
		// "+N" for the members folded behind it (CONFIG § Group approval). When the
		// gate is approved the whole set drops to the Waiting section, still folded,
		// until the filing-agent files it; un-approving there reverses gate + members.
		// Each member remembers whether it's a draft or already approved, so a set
		// stays visible (in Waiting) even after "approve all" stamps every member. A
		// container with no single resolvable gate lists its members instead.
		const containers = new Map<string, { entry: Entry; depth: number; approved: boolean }[]>();

		for (const f of this.app.vault.getMarkdownFiles()) {
			if (under(f.path, archive)) continue;
			if (queueFiles.has(f.path)) continue; // queues are surfaced as items, not file rows

			const fm = this.app.metadataCache.getFileCache(f)?.frontmatter;
			const type = fm?.[s.typeField] != null ? String(fm[s.typeField]) : null;
			const draft = fm ? this.isUnreviewed(fm) : false;
			const approved = fm ? this.isApproved(fm) : false;
			const inbox = s.inboxFolders.find((p) => under(f.path, p));
			const inQueue = s.nowQueueFolders.some((p) => under(f.path, p));
			const entry: Entry = { file: f, type, draft, queued: inQueue };

			if (inbox) {
				// Only reviewable notes form a set; an asset or fieldless file is neither
				// draft nor approved and is ignored.
				if (!draft && !approved) continue;
				const segs = f.path.slice(inbox.length + 1).split("/");
				if (segs.length === 1) {
					// Loose at the inbox root: a draft needs you; a lone approved file
					// awaits the filing-agent.
					if (draft) needs.push(entry);
					else waiting.push({ ...entry, awaitingFiling: true });
					continue;
				}
				const container = `${inbox}/${segs[0]}`;
				const arr = containers.get(container);
				if (arr) arr.push({ entry, depth: segs.length, approved });
				else containers.set(container, [{ entry, depth: segs.length, approved }]);
				continue;
			}
			if (inQueue) {
				// Outbox drops surface as Queue rows (collectOutboxDrops), not Needs
				// rows; any other configured queue folder keeps its Needs-you rows.
				const outbox = this.outboxPath();
				if (!outbox || !under(f.path, outbox)) needs.push(entry);
			} else if (type && activeTypes.has(type)) active.push(entry);
		}

		for (const [cpath, members] of containers.entries()) {
			const cname = cpath.split("/").pop() ?? cpath;
			const roots = members.filter((m) => m.depth === 2);
			const gate = pickGate(roots.map((m) => m.entry), cname);
			const gateMember = gate ? members.find((m) => m.entry === gate) : undefined;
			const count = members.length;

			// A resolvable gate with tag-alongs folds to one row carrying "+N".
			// Approved → Waiting (with its member files for un-approve); a draft →
			// Needs-you. The gate keeps the badge; the members fold behind it.
			if (gate && count > 1) {
				const head: Entry = {
					...gate,
					isGate: true,
					setCount: count - 1,
					// Path-sorted: vault iteration order is not guaranteed stable, and
					// the member list renders (the gate unfold) — keep it deterministic.
					setFiles: members
						.filter((m) => m.entry !== gate)
						.map((m) => m.entry.file)
						.sort((a, b) => a.path.localeCompare(b.path)),
				};
				if (gateMember?.approved) waiting.push({ ...head, awaitingFiling: true });
				else needs.push(head);
				continue;
			}

			// A lone file in a container — no tag-alongs, so no gate tag.
			if (count === 1) {
				const only = members[0].entry;
				only.container = cname;
				if (members[0].approved) waiting.push({ ...only, awaitingFiling: true });
				else needs.push(only);
				continue;
			}

			// Several members but no single gate. A nested set folds to its shallowest
			// member, unbadged (a non-gate never claims approval power); a flat peer
			// folder lists each. Either way a draft needs you, an approved one waits.
			if (members.some((m) => m.depth >= 3)) {
				members.sort((a, b) => a.depth - b.depth || a.entry.file.path.localeCompare(b.entry.file.path));
				// Build a fresh head (don't mutate the shared collected entry) — matches
				// the gate branch and keeps the source entry reusable.
				const head: Entry = {
					...members[0].entry,
					setCount: count - 1,
					setFiles: members.slice(1).map((m) => m.entry.file),
				};
				if (members[0].approved) waiting.push({ ...head, awaitingFiling: true });
				else needs.push(head);
				continue;
			}
			for (const m of members) {
				m.entry.container = cname;
				if (m.approved) waiting.push({ ...m.entry, awaitingFiling: true });
				else needs.push(m.entry);
			}
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

		// Every comparator ends on the path — a DETERMINISTIC tiebreaker, so two
		// files sharing an mtime (a sync batch stamps many at once) can never
		// swap places between renders (the FYP shuffle).
		const byPath = (a: Entry, b: Entry) => a.file.path.localeCompare(b.file.path);
		const byMtime = (a: Entry, b: Entry) =>
			b.file.stat.mtime - a.file.stat.mtime || byPath(a, b);
		const byActivity = (a: Entry, b: Entry) =>
			(b.activity ?? b.file.stat.mtime) - (a.activity ?? a.file.stat.mtime) || byPath(a, b);
		// Mixed-type lists sort type-major in CONFIG § Types row order (the
		// same order the explorer floats by and the sections stack in), recency
		// within a type — so every surface tells one ordering story. Needs-you
		// is grouped per type already; recency alone orders inside a section.
		const typeIdx = this.configTypeIndex();
		const typeRank = (e: Entry) => (e.type ? typeIdx.get(e.type) ?? 900 : 900);
		needs.sort(byMtime);
		waiting.sort((a, b) => typeRank(a) - typeRank(b) || byMtime(a, b));
		active.sort((a, b) => typeRank(a) - typeRank(b) || byActivity(a, b));
		return { needs, active, waiting };
	}

	private isUnreviewed(fm: Record<string, unknown>): boolean {
		const v = fm[this.plugin.settings.reviewedField];
		return v === false || v === "false";
	}

	/** Explicitly stamped reviewed: true — distinct from merely not-a-draft
	 * (a file with no reviewed field is neither). */
	private isApproved(fm: Record<string, unknown>): boolean {
		const v = fm[this.plugin.settings.reviewedField];
		return v === true || v === "true";
	}

	private colorFor(type: string): string | null {
		const t = this.plugin.typeStyles().find((x) => x.type === type);
		return t?.color || null;
	}

	/** Row display name — the basename minus a structural numeric prefix
	 * ("00-Cover" → "Cover"). Exactly two digits, the kit's § Numbering shape
	 * (the per-prefix styling rules were retired with settings.prefixStyles):
	 * a date-named file (2026-06-12-…) keeps its date. */
	private displayName(file: TFile): string {
		return file.basename.replace(/^\d{2}[-_ ]+/, "");
	}
}

/** True if `path` is the folder `root` or sits anywhere beneath it. */
function under(path: string, root: string): boolean {
	return path === root || path.startsWith(root + "/");
}

/**
 * Resolve a working set's gate among its root members. A lone root file over
 * supporting subfolders IS the gate — "a folder with one human facing file"
 * (CONFIG § Group approval). Among several roots, naming decides: a single
 * 00-prefixed cover/manifest, else a single folder-note (basename equal to the
 * container, the plain scheme's cover convention), else a single date-named
 * file (a handoff set's gate is its session log, not a 00- cover). Two
 * candidates of the same rank resolve to none — no guessing. The ranks mirror
 * decorator.ts's pickGateName, its name-only twin.
 */
function pickGate(roots: Entry[], containerName: string): Entry | null {
	const one = (xs: Entry[]) => (xs.length === 1 ? xs[0] : null);
	return (
		one(roots) ??
		one(roots.filter((e) => /^00[-_ ]/.test(e.file.name))) ??
		one(roots.filter((e) => e.file.basename === containerName)) ??
		one(roots.filter((e) => /^\d{4}-\d{2}-\d{2}/.test(e.file.name)))
	);
}

/** First file inside a folder, depth-first by name — what a folder drop row opens. */
function firstFileIn(folder: TFolder): TFile | null {
	const kids = [...folder.children].sort((a, b) => a.name.localeCompare(b.name));
	for (const k of kids) if (k instanceof TFile) return k;
	for (const k of kids) {
		if (k instanceof TFolder) {
			const f = firstFileIn(k);
			if (f) return f;
		}
	}
	return null;
}

/** Size a textarea to its content so a wrapped task shows in full.
 * Exported for the standalone queue view. */
export function autoGrow(ta: HTMLTextAreaElement): void {
	ta.style.height = "auto";
	ta.style.height = `${ta.scrollHeight}px`;
}

/** Parse the machine queue's flat checklist into items.
 * Exported for the standalone queue view. */
export function parseQueueItems(content: string): QueueItem[] {
	const items: QueueItem[] = [];
	content.split("\n").forEach((ln, i) => {
		const m = ln.match(CHECKBOX_RE);
		if (m) items.push({ line: i, text: m[4], done: m[2] !== " " });
	});
	return items;
}

/** The one shared "reduced" row — a compact, faded block (title, with a small
 * fate note beneath when given) used for every settled item: crossed-off queue
 * lines, resolved decisions, awaiting-filing gates. The note sits BELOW the
 * title so a long title is never smushed to make room for it; omit it where a
 * group sub-header already says the same thing for every row. A `holdCommit`
 * makes the WHOLE ROW a press-and-hold (the kit's commit gesture) — the
 * destructive write rides the hold, never a stray tap.
 * Exported for the standalone queue view. */
export function renderReducedRow(
	list: HTMLElement,
	o: {
		title: string;
		note?: string;
		struck: boolean;
		onOpen?: (newLeaf: boolean) => void;
		checkbox?: { checked: boolean; onChange: () => void | Promise<void> };
		ariaLabel?: string;
		bullet?: boolean;
		/** Whole-row press-and-hold commit (e.g. restore a crossed-off task). */
		holdCommit?: () => void | Promise<void>;
	}
): void {
	const row = list.createDiv("nkui-now-reducedrow");
	if (o.bullet) row.createSpan({ cls: "nkui-now-qbullet", text: "•" });
	if (o.ariaLabel) {
		row.setAttr("aria-label", o.ariaLabel);
		row.setAttr("title", o.ariaLabel);
	}
	if (o.checkbox) {
		const box = o.checkbox;
		const cb = row.createEl("input", { cls: "nkui-now-qcheck", attr: { type: "checkbox" } });
		cb.checked = box.checked;
		cb.addEventListener("change", () => void box.onChange());
	}
	const main = row.createDiv("nkui-now-reducedmain");
	const title = main.createSpan({ cls: "nkui-now-reducedtitle", text: o.title });
	if (o.struck) title.addClass("is-struck");
	if (o.note) main.createSpan({ cls: "nkui-now-fatenote", text: o.note });
	if (o.holdCommit) {
		const commit = o.holdCommit;
		row.addClass("nkui-row-hold");
		row.setAttr("role", "button");
		row.setAttr("tabindex", "0");
		attachHold(row, {
			onCommit: commit,
			onTap: o.onOpen ? (ev) => o.onOpen?.(ev ? ev.ctrlKey || ev.metaKey : false) : undefined,
			keyHold: true,
		});
		return;
	}
	if (o.onOpen) {
		const open = o.onOpen;
		// With a checkbox the title opens its source; without one the whole row is
		// the affordance (e.g. click a resolved decision to open its heading).
		const target = o.checkbox ? title : row;
		target.addClass("nkui-now-reducedrow-click");
		target.addEventListener("click", (ev) => open(ev.ctrlKey || ev.metaKey));
	}
}

/** Handlers a machine-queue row needs from its host view. */
export interface MachineRowHandlers {
	onSkip: (item: QueueItem) => void;
	onEdit: (main: HTMLElement, item: QueueItem) => void;
}

/** One machine-queue row — the crossed-off reduced shape, or the open shape
 * (bullet · text). The ROW is the control (no pencil button): hold to skip,
 * tap to edit the text in place. Exported for the standalone queue view. */
export function renderMachineItemRow(list: HTMLElement, item: QueueItem, h: MachineRowHandlers): void {
	// A crossed-off item (skipped by the user or executed by the agent) is a
	// receipt, not a live ask — it renders in the shared reduced shape (small,
	// faded, fate note beneath the text) so it never crowds the open items.
	// Restoring it is a write, so it rides the hold, not a stray tap.
	if (item.done) {
		const skipped = SKIP_MARK_RE.test(item.text);
		const executed = EXEC_MARK_RE.test(item.text);
		renderReducedRow(list, {
			title: item.text.replace(SKIP_MARK_RE, "").replace(EXEC_MARK_RE, ""),
			note: skipped
				? "skipped — clears next agent run"
				: executed
					? "executed — clears next run"
					: "done — clears next agent run",
			struck: true,
			bullet: true,
			holdCommit: () => h.onSkip(item),
			ariaLabel: executed
				? "Executed by the action agent; clears on its next run. Hold to restore — it will run again."
				: "Crossed off — the agent clears it next run without executing. Hold to restore.",
		});
		return;
	}
	// Open item: HOLD the row to cross it off (a SKIP — the line gains the
	// literal "*(item skipped)*" marker with its [x] so the agent's sweep tells
	// it from a real completion); a tap re-opens the text for editing.
	const row = list.createDiv("nkui-now-qrow nkui-now-qrow-strike nkui-row-hold");
	row.createSpan({ cls: "nkui-now-qbullet", text: "•" });
	const main = row.createDiv("nkui-now-qmain");
	main.createSpan({ cls: "nkui-now-qtext", text: item.text });
	row.setAttr("aria-label", "Hold the task to skip it; tap to edit its text.");
	row.setAttr("title", row.getAttr("aria-label") ?? "");
	row.setAttr("role", "button");
	row.setAttr("tabindex", "0");
	attachHold(row, {
		onCommit: () => h.onSkip(item),
		onTap: () => h.onEdit(main, item),
		keyHold: true,
	});
}

/** The add-task box — a growing textarea plus a "+" submit. A textarea, not a
 * one-line input: a long task wraps so it can be read back (especially on
 * mobile, where a single line scrolls off-screen). The "+" is the reliable
 * submit on mobile, where the soft-keyboard return inserts a newline; desktop
 * Enter submits, Shift+Enter breaks a line. Returns the textarea so the host
 * can re-focus it after a render. Exported for the standalone queue view. */
export function renderAddTaskBox(parent: HTMLElement, onAdd: (text: string) => void): HTMLTextAreaElement {
	// `nkui-now-qadd-live` exempts the box from the empty-group fade: an empty
	// Queue section dims, but the way INTO it (add a task) stays full-strength.
	const add = parent.createDiv("nkui-now-qadd nkui-now-qadd-live");
	const input = add.createEl("textarea", {
		cls: "nkui-now-qaddinput",
		attr: { rows: "1", placeholder: "Add a task…" },
	});
	const submit = (): void => {
		const v = input.value.trim();
		if (!v) return;
		input.value = "";
		autoGrow(input);
		onAdd(v);
	};
	const submitBtn = add.createEl("button", { cls: "nkui-now-qaddsubmit" });
	setIcon(submitBtn, "plus");
	submitBtn.setAttr("aria-label", "Add task");
	// preventDefault on pointerdown stops the tap stealing focus from the input
	// (and, on some mobile webviews, suppresses the click — the attachHold
	// lesson), so on mobile the submit must ride pointerup; desktop keeps click.
	submitBtn.addEventListener("pointerdown", (ev) => ev.preventDefault());
	const fireSubmit = (ev: Event): void => {
		ev.preventDefault();
		submit();
		// Desktop keeps focus for the next task; mobile blurs so the iOS
		// keyboard retracts once the task is in.
		if (Platform.isMobile) input.blur();
		else input.focus();
	};
	if (Platform.isMobile) submitBtn.addEventListener("pointerup", fireSubmit);
	else submitBtn.addEventListener("click", fireSubmit);
	input.addEventListener("input", () => autoGrow(input));
	input.addEventListener("keydown", (ev) => {
		if (ev.key === "Enter" && !ev.shiftKey) {
			ev.preventDefault();
			submit();
		}
	});
	// (The mobile "done" checkmark was removed per user request — redundant now
	// that the + button blurs the input on submit on mobile, which dismisses the
	// soft keyboard. The .nkui-now-qadddone CSS is now dead — sweep in § DD.)
	return input;
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
 *
 * Exported for the standalone queue view.
 */
export function parseDecisions(content: string): Decision[] {
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
/** Approved in the file ([x]) and awaiting the action-agent's clearing pass. */
export function isResolvedDecision(d: Decision): boolean {
	return d.options.some((o) => o.state === "x" || o.state === "X");
}

export function isOpenDecision(d: Decision): boolean {
	if (!d.options.length) return true;
	const approved = d.options.some((o) => o.state === "x" || o.state === "X");
	const selectable = d.options.some((o) => o.state === " ");
	return selectable && !approved;
}

/** Strip light inline markdown (bold/italic/code markers) for plain display.
 * Exported for the standalone queue view. */
export function plainText(s: string): string {
	return s
		.replace(/\*\*(.+?)\*\*/g, "$1")
		.replace(/`([^`]+)`/g, "$1")
		.replace(/(^|\s)[*_]{1,2}(\S.*?\S|\S)[*_]{1,2}(?=\s|$)/g, "$1$2");
}

/** The stylesheet's --nkui-settle token in ms — the exit-animation beat the
 * settling waits out. Read live so a token change stays one edit; falls back
 * to the shipped 170ms when unset (e.g. the stylesheet not yet injected). */
function settleMs(): number {
	const raw = getComputedStyle(document.body).getPropertyValue("--nkui-settle").trim();
	const v = parseFloat(raw);
	if (!Number.isFinite(v) || v <= 0) return 170;
	// "170ms" parses to 170; a bare seconds value ("0.17s") scales up.
	return raw.endsWith("ms") ? v : v < 10 ? v * 1000 : v;
}

/** Primary title line — today's date, e.g. "Sunday, June 7". */
function formatToday(): string {
	return new Date().toLocaleDateString(undefined, {
		weekday: "long",
		month: "long",
		day: "numeric",
	});
}

/** A Needs-you section label that agrees with its count: "session" / "sessions",
 * "note" / "notes", "index" / "indexes". The header CSS capitalises it. The
 * non-type buckets ("queued", "untyped") read as-is. */
function pluralLabel(key: string, n: number): string {
	if (key === "queued" || key === "untyped" || n === 1) return key;
	return /(?:[sxz]|ch|sh)$/.test(key) ? `${key}es` : `${key}s`;
}

/** Build the subtitle DOM with twin-span labels so the container query can
 * shorten them on narrow panes (item W3). Each count segment wraps its label
 * in nkui-twin-long / nkui-twin-short pairs; the dot separators are plain text
 * nodes so they inherit the surrounding style. */
function renderSubtitle(
	parent: HTMLElement,
	review: number,
	active: number,
	decide: number,
	waiting: number
): void {
	const sub = parent.createDiv("nkui-now-subtitle");
	// Collect segments (decide first, then review, active, waiting — matching the
	// old summary() order: most urgent to least). Zero-count segments are omitted.
	const segments: Array<{ count: number; long: string; short: string }> = [];
	if (decide > 0) segments.push({ count: decide, long: "to decide", short: "" });
	if (review > 0) segments.push({ count: review, long: "to review", short: "" });
	if (active > 0) segments.push({ count: active, long: "active", short: "" });
	if (waiting > 0)
		segments.push({ count: waiting, long: "waiting for agent", short: "waiting" });

	if (!segments.length) {
		sub.appendText("All clear");
		return;
	}
	segments.forEach((seg, i) => {
		if (i > 0) sub.appendText("  ·  ");
		// The count digit is plain text — no twin needed; only the label varies.
		sub.appendText(`${seg.count} `);
		if (seg.short) {
			// Responsive label: long text on wide panes, short on narrow.
			sub.createSpan({ cls: "nkui-twin-long", text: seg.long });
			sub.createSpan({ cls: "nkui-twin-short", text: seg.short });
		} else {
			// Single-form label — no shortening needed.
			sub.appendText(seg.long);
		}
	});
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
