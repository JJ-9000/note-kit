import { TFile, TFolder, Vault, debounce, type Debouncer } from "obsidian";
import { isApproved, isUnreviewed, pickGateName } from "./kitShared";
import type NoteKitUiPlugin from "./main";

/**
 * Decorates file-explorer rows with data attributes the stylesheet keys off:
 *   data-nkui-sink     (a) declared sink folder (settings.sinkFolders) — the
 *                      title AND its wrapping .nav-folder carry it, so the
 *                      stylesheet can dim the whole subtree
 *   data-nkui-uncat    (a2) vault-root item the kit doesn't recognise
 *   data-nkui-empty    (b) empty content — a 0-byte file, or a folder with no
 *                      children (dimmer + smaller, like sink; stylesheet)
 *   data-nkui-nonmd    (e4) non-markdown file (asset/binary) — diminutive,
 *                      sink-like dim + small (stylesheet)
 *   data-nkui-type     (c) note type (only for types with a configured colour)
 *   data-nkui-reviewed (d) "false" on unreviewed drafts
 *   data-nkui-weight   (w) frontmatter `weight` > 0, with a w<n> badge and a
 *                      per-folder --nkui-weight-heat custom property (0..1)
 * Also injects the live "needs you" pill on inbox folder rows (d) — drafts
 * awaiting a decision, not sets already approved and waiting on the
 * filing-agent. Folder-role matching still strips a leading `NN-` token so
 * legacy numeric-prefix vaults resolve their roles, but no styling or
 * attribute is keyed on prefixes anymore.
 *
 * Also ORDERS the explorer when float-to-top is on (settings.floatTopTypes) —
 * not by moving DOM nodes, but by patching the file-explorer view's internal
 * `getSortedFolderItems` (see patchExplorerSort) so rows are BORN in kit
 * order: inside each folder the two configured queue files lead, then the
 * floated types (weighted siblings sort by weight desc within their band when
 * settings.sortByWeight is on), then the cover/index note, then everything
 * else in native order; at the vault root the kit folders take their semantic
 * order (inbox → outbox → projects → areas → references → snippets, archive
 * last). The previous mechanism — post-hoc insertBefore moves after Obsidian
 * rendered — was removed in 0.4.65: the virtualized explorer re-renders rows
 * in ITS order on every change and moving them afterwards read as the list
 * shuffling, forever. (CSS flex/order overrides were ruled out even earlier,
 * in 0.4.52 — they broke the collapse height animation.)
 *
 * All DOM writes are idempotent: re-running on an already-decorated row
 * produces no mutation, so the MutationObserver that drives re-decoration
 * cannot loop.
 */
export class ExplorerDecorator {
	private plugin: NoteKitUiPlugin;
	private observers: MutationObserver[] = [];
	private redraw: Debouncer<[], void>;

	/** Declared sink terms (settings.sinkFolders), prefix-stripped and
	 * lower-cased — a folder anywhere whose name CONTAINS any term is a sink
	 * (contains-word match; exact match is the degenerate case). */
	private sinkTerms: string[] = [];
	private typeColors = new Set<string>();
	private floatSet = new Set<string>();
	private floatEnabled = false;
	/** name (and prefix-stripped name) → CONFIG table-row index. Row order in
	 * § Folders / § Subfolders / § Types IS the display order. */
	private rootOrderMap = new Map<string, number>();
	private subOrderMap = new Map<string, number>();
	private typeOrderMap = new Map<string, number>();
	/** Counts-only refresh — much cheaper than decorateAll; the explorer is a
	 * virtualized list, so scroll fires mutation batches constantly and a full
	 * decorate+reorder pass per batch reads as dropped frames. */
	private countsRefresh: Debouncer<[], void>;
	/** Scroll-settle re-decoration — the explorer recycles rows as it scrolls,
	 * and a reused row can surface carrying another path's decoration (a wash
	 * that "follows scroll") or none at all (a folder-note index blank until
	 * scrolled away and back). Decoration-only (no counts; row order is the
	 * sort patch's, nothing here ever moves a node), debounced trailing so a
	 * fling costs ONE pass after the scroll settles, not work per frame. */
	private scrollRedraw: Debouncer<[], void>;
	/** Containers whose scroll listener is already attached — attachObservers
	 * re-runs on every layout-change and must not stack duplicate listeners. */
	private scrollBound = new WeakSet<HTMLElement>();
	/** The path each element was last decorated as — recycling implies a
	 * data-path swap, so the stale-mark hygiene only needs to run when the
	 * stored path differs; same path means nothing can be stale. */
	private lastDecoratedPath = new WeakMap<HTMLElement, string>();
	/** folderPath → {primary, cover} borrowed colours. computeFolderTypeColors
	 * sorts + reads metadata per call — the heaviest per-row cost in decorate —
	 * so each pass pays it once per unique folder; vault/metadata events invalidate. */
	private folderColorCache = new Map<string, { primary: string | null; cover: string | null }>();
	/** Explorer views whose getSortedFolderItems is wrapped → what restores it
	 * (the saved own-property value, or "delete the wrapper" when the method
	 * lived on the prototype). Patched per view INSTANCE, never the prototype,
	 * so another plugin's prototype patch is left alone. */
	private sortPatched = new Map<Record<string, unknown>, { hadOwn: boolean; original: unknown }>();
	/** Log the missing-hook warning once per session, not per leaf per layout. */
	private sortPatchWarned = false;
	/** path → the metadata the sort rank reads (`type|weight`). 'changed' fires
	 * continuously while the user types; only an edit that MOVES a row should
	 * cost a re-sort, so the handler compares against this and nudges the
	 * explorer only on a real rank-input change. */
	private rankKeys = new Map<string, string>();
	/** Debounced explorer re-sort for metadata-driven rank changes. */
	private sortRefresh: Debouncer<[], void>;
	/** Pending post-expand catch-up timer (onMutations defers the full pass past
	 * the native folder-expand animation; cleared on stop so it can't fire into a
	 * dead plugin's explorer). */
	private expandRedrawTimer: number | undefined;
	/** Max-wait guard for redraw(): redraw's debounce is resetTimer:true, so a
	 * sustained create/delete/resolved burst (bulk filing/sync, sub-150ms gaps)
	 * would push the trailing deadline forever and starve the full pass.
	 * redrawBounded() forces a flush once the burst passes the window. 0 = idle. */
	private redrawBurstStart = 0;

	constructor(plugin: NoteKitUiPlugin) {
		this.plugin = plugin;
		// resetTimer TRUE so a burst of resolved/rename/create/delete events (the
		// metadataCache 'resolved' wave during indexing especially) coalesces to ONE
		// trailing full pass instead of a re-stamp + forced reflow per 150ms window —
		// the repeated late re-stamp that reads as the explorer "snapping".
		this.redraw = debounce(() => this.decorateAll(), 150, true);
		this.countsRefresh = debounce(() => void this.updateCounts(), 600, false);
		// resetTimer: TRUE — each scroll event pushes the deadline back, so the
		// pass runs once on settle; with false this is a 10Hz throttle that runs
		// the full pass DURING the fling, the exact churn the counts-only fix
		// removed. flush=false: a settled scroll re-stamps recycled rows but must
		// NOT force the offsetHeight reflow (that is only for a style/rounded change).
		this.scrollRedraw = debounce(() => this.decorateRendered(false), 100, true);
		// 600ms (resetTimer) so transient frontmatter values while the user TYPES a
		// type/weight edit (weight: 1 → 12 passes through valid ranks) settle before
		// one re-sort fires — the explorer no longer shuffles rows mid-keystroke.
		this.sortRefresh = debounce(() => this.resortExplorer(), 600, true);
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
				// Targeted row + counts only — "changed" fires continuously while the
				// user types; a debounced FULL pass here cost visible frame drops.
				this.folderColorCache.clear();
				this.decorateByPath(file.path);
				this.countsRefresh();
				// Row ORDER is born in the patched sort, which only reruns when the
				// explorer is asked — so a type/weight edit (the rank inputs) nudges
				// it. Body edits leave the key unchanged and never cost a re-sort.
				const key = this.rankKey(file.path);
				if (this.rankKeys.get(file.path) !== key) {
					this.rankKeys.set(file.path, key);
					this.sortRefresh();
				}
			})
		);
		// resolved/create/delete/rename are the BURST sources (indexing, bulk
		// filing/sync) — route them through the max-wait so a sustained burst can't
		// starve the full pass (see redrawBounded).
		this.plugin.registerEvent(app.metadataCache.on("resolved", () => this.redrawBounded()));
		this.plugin.registerEvent(
			app.vault.on("rename", (file) => {
				this.folderColorCache.clear();
				this.decorateByPath(file.path);
				this.redrawBounded();
			})
		);
		this.plugin.registerEvent(
			app.vault.on("create", () => {
				this.folderColorCache.clear();
				this.redrawBounded();
			})
		);
		this.plugin.registerEvent(
			app.vault.on("delete", () => {
				this.folderColorCache.clear();
				this.redrawBounded();
			})
		);
		// A queue's actionable state changes by CONTENT (a box toggled, a task
		// added/edited) — that fires 'modify', not a metadata change — so refresh the
		// queue highlight on it.
		this.plugin.registerEvent(
			app.vault.on("modify", (file) => {
				const s = this.plugin.settings;
				if (file.path === s.userQueuePath || file.path === s.machineQueuePath) {
					void this.updateCounts();
				}
			})
		);
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
		// A pending refresh firing after unload would re-inject badges into the
		// explorer of a dead plugin.
		this.redraw.cancel();
		this.countsRefresh.cancel();
		this.scrollRedraw.cancel();
		this.sortRefresh.cancel();
		if (this.expandRedrawTimer) {
			window.clearTimeout(this.expandRedrawTimer);
			this.expandRedrawTimer = undefined;
		}
		this.clearAll();
		// Unwrap getSortedFolderItems, then hand the row order back to Obsidian.
		this.unpatchExplorerSort();
		this.resortExplorer();
	}

	refresh(): void {
		this.compile();
		this.attachObservers();
		this.clearAll();
		// Settings may have moved any rank input (float types, queue paths,
		// weight sorting, ordering off) — re-sort through the patched hook, which
		// reads floatEnabled live and yields native order when ordering is off.
		this.resortExplorer();
		this.decorateAll();
	}

	// ── setup ────────────────────────────────────────────────────────────────

	private compile(): void {
		const s = this.plugin.settings;
		this.folderColorCache.clear(); // type colours / settings may have changed
		// A "sink" is a declared low-attention folder (settings.sinkFolders, e.g.
		// "Archive").  Each entry (already a separate array element — the settings
		// UI splits on commas when saving) is prefix-stripped and lower-cased;
		// a folder anywhere whose own name CONTAINS any term is a sink
		// (contains-word semantics; exact match is the degenerate case where the
		// term equals the full bare name).  No hard-coded folder literals here —
		// everything derives from settings.sinkFolders.
		this.sinkTerms = (s.sinkFolders ?? [])
			.map((n) => n.replace(/^\d{2,}[-_ ]+/, "").trim().toLowerCase())
			.filter(Boolean);
		this.typeColors = new Set(this.plugin.typeStyles().filter((t) => t.color).map((t) => t.type));
		this.floatSet = new Set(s.floatTopTypes ?? []);
		// One gate for ALL explorer ordering (per-folder float AND the root's
		// semantic folder order): the "Float types to top" setting, off when empty.
		this.floatEnabled = this.floatSet.size > 0;
		// CONFIG table-row order drives display order. Each literal maps by its
		// exact name AND its prefix-stripped form, so 00-Inbox and Inbox both
		// resolve on either scheme.
		const orderMap = (literals: string[] | undefined): Map<string, number> => {
			const m = new Map<string, number>();
			(literals ?? []).forEach((lit, i) => {
				m.set(lit.toLowerCase(), i);
				m.set(lit.replace(/^\d{2,}[-_ ]+/, "").toLowerCase(), i);
			});
			return m;
		};
		const f = this.plugin.kitFacts;
		this.rootOrderMap = orderMap(f?.rootOrder);
		this.subOrderMap = orderMap(f?.subfolderOrder);
		// Floated types order: § Types row order when CONFIG is read, else the
		// manual floatTopTypes list order.
		this.typeOrderMap = new Map(
			(f?.types?.length ? f.types : (s.floatTopTypes ?? [])).map((t, i) => [t, i])
		);
		// Seed the rank-input keys so the FIRST 'changed' per file compares
		// against reality instead of reading every keystroke as a rank change.
		this.rankKeys.clear();
		for (const file of this.plugin.app.vault.getMarkdownFiles()) {
			this.rankKeys.set(file.path, this.rankKey(file.path));
		}
	}

	/** The metadata the sort rank reads for one path, as a comparable key. */
	private rankKey(path: string): string {
		return `${this.typeOfPath(path) ?? ""}|${this.weightOfPath(path)}`;
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
		// Wrap any explorer leaf not yet carrying the sort patch (a new leaf
		// appears via layout-change, which re-runs this) — and re-sort once when
		// a wrap landed, so the new leaf's rows take kit order immediately.
		if (this.patchExplorerSort()) this.resortExplorer();
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
			// debounced pass and snaps. `style` is watched too: Obsidian writes each
			// row's per-depth indent as an inline `padding-inline-start`, and when a
			// recycled row's padding updates in a different tick than its data-path the
			// band would keep a stale --nkui-row-indent until the next decorate /
			// scroll-settle — painting the highlight at the old inset for a frame, then
			// snapping (the "hover highlight loads to the side then snaps" flash). The
			// style branch in onMutations re-mirrors the indent cheaply, before paint.
			obs.observe(c, {
				childList: true,
				subtree: true,
				attributes: true,
				attributeFilter: ["data-path", "style"],
			});
			this.observers.push(obs);
			// Row recycling can reveal a row without any observable mutation (the
			// virtualizer repositions existing nodes) — a settled scroll re-runs
			// the decoration pass so every visible row paints correctly.
			if (!this.scrollBound.has(c)) {
				this.scrollBound.add(c);
				this.plugin.registerDomEvent(c, "scroll", () => this.scrollRedraw(), { passive: true });
			}
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
		// Row ORDER never runs from here anymore — rows are born sorted via the
		// patched getSortedFolderItems, so the observer only decorates. (The old
		// post-hoc move scheduling, including the synchronous root reorder, went
		// with the DOM-move mechanism in 0.4.65.)
		let childrenInserted = false;
		for (const m of muts) {
			if (m.type === "attributes" && m.target instanceof HTMLElement) {
				const tgt = m.target;
				if (tgt.matches(".nav-file-title, .nav-folder-title")) {
					if (m.attributeName === "style") {
						// Cheap, idempotent re-mirror of Obsidian's inline
						// padding-inline-start into --nkui-row-indent the instant it
						// changes — before paint (this runs in the observer microtask).
						// Closes the stale-indent window that flashed a recycled row's
						// band at the old inset, then snapped. NO full decorate here (it
						// stays on data-path + the debounced pass, so scroll churn pays
						// only this O(1) cost); the value-equal guard stops decorate's own
						// indent write from re-triggering an endless style-mutation loop.
						const indent = tgt.style.paddingInlineStart;
						const cur = tgt.style.getPropertyValue("--nkui-row-indent");
						if (indent) {
							if (indent !== cur) tgt.style.setProperty("--nkui-row-indent", indent);
						} else if (cur) {
							tgt.style.removeProperty("--nkui-row-indent");
						}
					} else {
						// data-path swap = a RECYCLED row — full re-decorate + re-stamp
						// the settled rows once the scroll stops.
						this.decorate(tgt, s);
						this.scrollRedraw();
					}
				}
				continue;
			}
			m.addedNodes.forEach((node) => {
				if (!(node instanceof HTMLElement)) return;
				if (node.matches(".nav-file-title, .nav-folder-title")) this.decorate(node, s);
				node
					.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title")
					.forEach((el) => this.decorate(el, s));
				if (node.matches(".nav-folder-children") || node.querySelector(".nav-folder-children")) {
					childrenInserted = true;
				}
			});
		}
		if (childrenInserted) {
			// A folder just expanded: its rows are ALREADY decorated synchronously
			// by the loop above (so they paint styled, never raw-then-snap), and
			// Obsidian reads their height to arm its JS-driven children-height
			// expand animation. The full decorateAll catch-up (re-stamps every
			// rendered row AND forces a container reflow via offsetHeight) must NOT
			// land in the middle of that height transition, or the reflow/re-stamp
			// can stomp the inline height Obsidian is animating — the lost
			// folder-expand animation. Defer the catch-up past the native animation
			// window (Obsidian's --anim-duration-moderate ≈ 300ms) so the expand
			// plays clean; the rows are already correct, so the wait costs nothing
			// visible. countsRefresh (600ms) is already well clear. One in-flight
			// timer at a time (a rapid second expand just resets it); cleared in
			// stop() so it never fires into a dead plugin's explorer.
			if (this.expandRedrawTimer) window.clearTimeout(this.expandRedrawTimer);
			this.expandRedrawTimer = window.setTimeout(() => {
				this.expandRedrawTimer = undefined;
				this.redraw();
			}, 360);
			// Counts refresh via the deferred redraw above (decorateAll → updateCounts)
			// AFTER the native expand animation — do NOT also fire countsRefresh now:
			// adding/removing a badge span mid-transition changes a row's height and
			// perturbs the heights Obsidian is animating (the expand "chop").
		} else {
			// Counts only — the targeted work above already decorated every touched
			// row. A full decorateAll per mutation batch made scrolling the
			// virtualized explorer (which churns childList constantly) drop frames.
			this.countsRefresh();
		}
	}

	/** Synchronous, targeted decoration of one row by its data-path. */
	private decorateByPath(path: string): void {
		const s = this.plugin.settings;
		const sel = `.nav-file-title[data-path="${cssEscape(path)}"], .nav-folder-title[data-path="${cssEscape(path)}"]`;
		for (const c of this.containers()) {
			const el = c.querySelector<HTMLElement>(sel);
			if (!el) continue;
			this.decorate(el, s);
			// A metadata change can change a sibling's weight heat — refresh the
			// folder's style pass. Order changes ride the rank-key check in the
			// 'changed' handler instead (the sort patch).
			const cc = el.closest<HTMLElement>(".nav-folder-children");
			if (cc) this.applyFolderStyles(cc);
		}
	}

	// ── decoration ─────────────────────────────────────────────────────────────

	/** redraw() with a max-wait. redraw's debounce coalesces a burst (resetTimer)
	 * so an indexing/resolved storm doesn't re-stamp + reflow per 150ms, but
	 * Obsidian's debounce has no max-wait — a sustained create/delete/resolved
	 * burst with sub-150ms gaps would push the deadline forever and never flush.
	 * Force a full pass once the burst has run past ~600ms so the explorer can't
	 * stay stale for a whole bulk filing/sync operation. Date.now() is fine in the
	 * plugin runtime (this is not the workflow sandbox). */
	private redrawBounded(): void {
		const now = Date.now();
		if (!this.redrawBurstStart) this.redrawBurstStart = now;
		if (now - this.redrawBurstStart >= 600) {
			this.redraw.cancel();
			this.decorateAll(); // resets redrawBurstStart at its top
			return;
		}
		this.redraw();
	}

	private decorateAll(): void {
		this.redrawBurstStart = 0; // a full pass ran — close the burst window
		this.decorateRendered();
		void this.updateCounts();
	}

	/** Decoration over every RENDERED row, counts excluded — the virtualized
	 * explorer only keeps the visible rows in the DOM, so this is cheap, and
	 * the scroll-settle pass (scrollRedraw) uses it to re-stamp recycled rows
	 * without re-reading the queue files. The per-folder style pass (weight
	 * heat) refreshes too: it is style-only, no node ever moves for it. */
	private decorateRendered(flush = true): void {
		const s = this.plugin.settings;
		for (const c of this.containers()) {
			const titles = c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title");
			titles.forEach((el) => this.decorate(el, s));
			// Weight heat everywhere — the ROOT items' container first (it is
			// NOT a .nav-folder-children: current Obsidian renders root tree-items
			// in an unclassed div directly under .nav-files-container), then every
			// folder's children.
			const root = this.rootItemsContainer(c);
			if (root) this.applyFolderStyles(root);
			c.querySelectorAll<HTMLElement>(".nav-folder-children").forEach((cc) =>
				this.applyFolderStyles(cc)
			);
			// Item 1 — force a style-flush after re-stamping all attributes so the
			// browser repaints the new border-radius immediately (e.g. when rounded
			// mode toggles). Reading offsetHeight is a minimal forced reflow. Gated
			// to flush=true (the decorateAll / settings-refresh path) — the
			// scroll-settle pass calls decorateRendered(false), so a routine settled
			// scroll no longer forces a synchronous layout on the virtualized list.
			if (flush) void c.offsetHeight; // eslint-disable-line @typescript-eslint/no-unused-expressions
		}
	}

	/** The element whose direct children are the vault root's tree-items.
	 * Older DOMs wrap them in `.nav-folder.mod-root > .nav-folder-children`;
	 * current ones put them in an unclassed div under .nav-files-container. */
	private rootItemsContainer(c: HTMLElement): HTMLElement | null {
		const legacy = c.querySelector<HTMLElement>(".nav-folder.mod-root > .nav-folder-children");
		if (legacy) return legacy;
		const div = c.firstElementChild;
		if (
			div instanceof HTMLElement &&
			div.querySelector(":scope > .nav-folder, :scope > .nav-file")
		) {
			return div;
		}
		return null;
	}

	/** Queue files aren't coloured (a coloured row reads as an open folder); each
	 * shows a plain NUMBER — its open-item count — with a brief tooltip, and those
	 * counts also feed the top-level inbox pill. Open is judged the way the For You
	 * Decide/Queue buckets judge it (a resolved decision keeps unchecked sibling
	 * options, so a naive "has a [ ]" test over-counts). Also refreshes the inbox
	 * folder pill = unreviewed drafts + both queues. */
	private async updateCounts(): Promise<void> {
		const s = this.plugin.settings;
		const userN = s.userQueuePath ? await this.queueOpenCount(s.userQueuePath, true) : 0;
		const machineN = s.machineQueuePath ? await this.queueOpenCount(s.machineQueuePath, false) : 0;
		this.setQueueBadge(s.userQueuePath, userN, "waiting on user");
		this.setQueueBadge(s.machineQueuePath, machineN, "waiting on agent");

		const show = s.enableReviewFlags && s.showInboxCount;
		for (const c of this.containers()) {
			for (const folderPath of s.inboxFolders) {
				const titleEl = c.querySelector<HTMLElement>(
					`.nav-folder-title[data-path="${cssEscape(folderPath)}"]`
				);
				if (!titleEl) continue;
				let badge = titleEl.querySelector<HTMLElement>(".nkui-inbox-count");
				// Total USER actions: unreviewed drafts + open user-queue decisions. The
				// machine queue (machineN) is the AGENT's to-do, not the user's, so it is
				// deliberately excluded from this roll-up pill.
				const n = show ? this.countNeedsAttention(folderPath, s) + userN : 0;
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

	/** Set (or clear) a queue file's plain number badge + brief tooltip. */
	private setQueueBadge(path: string, n: number, brief: string): void {
		if (!path) return;
		for (const c of this.containers()) {
			const el = c.querySelector<HTMLElement>(`.nav-file-title[data-path="${cssEscape(path)}"]`);
			if (!el) continue;
			el.removeAttribute("data-nkui-queue-active"); // never colour-emphasise a queue
			let badge = el.querySelector<HTMLElement>(".nkui-queue-count");
			if (n <= 0) {
				badge?.remove();
				// The tooltip lives on the ROW — without this an emptied queue
				// keeps advertising its last nonzero count.
				el.removeAttribute("aria-label");
				el.removeAttribute("title");
				continue;
			}
			if (!badge) badge = el.createSpan({ cls: "nkui-queue-count" });
			const text = String(n);
			if (badge.textContent !== text) badge.setText(text);
			const t = `${n} ${brief}`;
			el.setAttr("aria-label", t);
			el.setAttr("title", t);
		}
	}

	/** Count open items: every open machine task, or every open user-queue decision. */
	private async queueOpenCount(path: string, isUser: boolean): Promise<number> {
		const f = this.plugin.app.vault.getAbstractFileByPath(path);
		if (!(f instanceof TFile)) return 0;
		const content = await this.plugin.app.vault.cachedRead(f);
		if (!isUser) return (content.match(/^\s*[-*]\s+\[ \]/gm) ?? []).length;
		let count = 0;
		let hasOpen = false,
			hasApproved = false,
			hasCheckbox = false,
			hasProse = false,
			inBlock = false;
		const flush = (): void => {
			if (inBlock && (hasCheckbox ? hasOpen && !hasApproved : hasProse)) count++;
		};
		for (const ln of content.split("\n")) {
			if (/^#{2,}\s+\S/.test(ln)) {
				flush();
				inBlock = true;
				hasOpen = hasApproved = hasCheckbox = hasProse = false;
				continue;
			}
			const m = ln.match(/^\s*[-*]\s+\[(.)\]/);
			if (m) {
				if (!inBlock) {
					inBlock = true;
					hasOpen = hasApproved = hasProse = false;
				}
				hasCheckbox = true;
				if (m[1] === " ") hasOpen = true;
				else if (m[1] === "x" || m[1] === "X") hasApproved = true;
				continue;
			}
			const t = ln.trim();
			if (inBlock && t && !t.startsWith("---") && !t.startsWith("_")) hasProse = true;
		}
		flush();
		return count;
	}

	private decorate(el: HTMLElement, s = this.plugin.settings): void {
		const path = el.getAttribute("data-path");
		if (!path) return;
		// The band's left edge tracks Obsidian's OWN per-row indent by reflecting its
		// inline padding-inline-start into --nkui-row-indent below — a one-directional
		// read, never a write to a property the host owns. (An earlier JS-flatten that
		// OVERRODE the host's margin/padding raced its re-render and snapped on click;
		// it was deleted — see [[Overriding-the-Hosts-Layout-Races-Its-Re-Render]].)
		const indent = el.style.paddingInlineStart;
		if (indent) el.style.setProperty("--nkui-row-indent", indent);
		else el.style.removeProperty("--nkui-row-indent");
		// Inject the wash band as the row's first child, once. The explorer recycles
		// rows, so guard on an existing band; the band is path-independent (it paints
		// var(--nkui-row-wash), which the stylesheet sets per state), so a recycled row
		// keeps its band and only the wash variable changes. position:absolute +
		// z-index:0 + pointer-events:none (stylesheet) keep it behind the content and
		// click-through.
		if (!el.querySelector(":scope > .nkui-row-band")) {
			el.createDiv({ cls: "nkui-row-band", prepend: true });
		}
		// Stale marks exist only when the element was recycled onto a NEW path —
		// same path since the last pass means the hygiene strips below can be
		// skipped (2-3 selector queries + ~8 attribute removals per row per pass
		// add up at a few hundred rendered rows). Regular stamping still runs:
		// frontmatter can change without a path swap.
		const recycled = this.lastDecoratedPath.get(el) !== path;
		this.lastDecoratedPath.set(el, path);
		// Obsidian's own state classes ride a recycled element too — the active
		// file's grey wash surfacing on an unrelated row and appearing to follow
		// the scroll. Strip them whenever the recycled row is NOT the active
		// file; Obsidian re-applies its own marks on a real selection or focus.
		if (recycled && (el.classList.contains("is-active") || el.classList.contains("has-focus"))) {
			if (path !== this.plugin.app.workspace.getActiveFile()?.path) {
				el.classList.remove("is-active", "has-focus");
			}
		}
		const isFolder = el.classList.contains("nav-folder-title");
		const leaf = path.split("/").pop() ?? path;
		const name = isFolder ? leaf : leaf.replace(/\.md$/i, "");

		// (a) sink folder — a folder anywhere whose name (prefix-stripped,
		// case-insensitive) is declared in settings.sinkFolders. Both the title
		// row and the wrapping .nav-folder carry the marker, so the stylesheet
		// can style the row AND dim everything inside the expanded subtree.
		// The sink look itself (smaller + dimmer) is entirely the stylesheet's.
		let isSink = false;
		if (isFolder) {
			// Strip a legacy `NN-` token for ROLE matching only — no styling or
			// attribute is keyed on the prefix itself anymore.
			const bare = name.replace(/^\d{2,}[ _-]+/, "").toLowerCase();
			isSink = this.sinkTerms.some((term) => bare.includes(term));
			this.setAttr(el, "data-nkui-sink", isSink ? "" : null);
			const wrapper = el.closest<HTMLElement>(".nav-folder");
			if (wrapper) this.setAttr(wrapper, "data-nkui-sink", isSink ? "" : null);

			// (a1) workflow role — the inbox/outbox folders are the kit's working
			// mouths, not content; the stylesheet keys an identity wash on the role.
			// Configured paths first (both queue-folder settings), CONFIG literals
			// next, the bare name at the vault ROOT only as the CONFIG-less
			// fallback (mirroring rootRank).
			const facts = this.plugin.kitFacts;
			const atRoot = !path.includes("/");
			const role =
				s.inboxFolders.includes(path) || path === facts?.inboxLiteral || (!facts && atRoot && bare === "inbox")
					? "inbox"
					: s.nowQueueFolders.includes(path) || path === facts?.outboxLiteral || (!facts && atRoot && bare === "outbox")
						? "outbox"
						: null;
			this.setAttr(el, "data-nkui-role", role);

			// Type-named folder colouring: a folder whose name — with any numeric
			// prefix stripped — is a note type (02-Projects or plain Projects →
			// project) is tinted by that type, brighter than the files inside
			// (stylesheet). Singularise the plural folder name to match the singular
			// type. Works identically for the prefixed and plain naming schemes.
			const singular = bare.endsWith("s") ? bare.slice(0, -1) : bare;
			const ftype = this.typeColors.has(bare) ? bare : this.typeColors.has(singular) ? singular : null;
			let fcolor = ftype
				? this.plugin.typeStyles().find((t) => t.type === ftype)?.color ?? null
				: null;
			// Otherwise (the folder name isn't a type) an OPEN folder borrows the
			// colour of its top-most NON-INDEX typed child by visible order, falling
			// back to the cover/index note's own type only when nothing else is typed —
			// so a plain folder reads its kind from the content that leads it.
			if (!ftype) {
				const cc = this.topChildTypeColor(path);
				if (cc) fcolor = cc;
			}
			this.setAttr(el, "data-nkui-folder-type", ftype ?? (fcolor ? "child" : null));
			if (fcolor) el.style.setProperty("--nkui-folder-color", fcolor);
			else el.style.removeProperty("--nkui-folder-color");

			// Nested-container box (Note-Kit-UI-Nested-Containers-Plan): stamp the
			// WRAPPER .nav-folder so the stylesheet can draw a filled tinted box
			// around the folder's title + children when it is OPEN. The box is an
			// OPT-IN look — the stylesheet only paints it under body.nkui-nested-boxes
			// (settings.nestedContainers, OFF by default); these marks are inert and
			// cheap when the feature is off, so flipping the toggle is instant. The
			// open/closed gate stays in CSS (:not(.is-collapsed)) so JS never touches
			// the collapse-height animation (a prior regression source). --nkui-depth
			// (0 at a root folder, +1 per nesting level) drives the per-depth wash +
			// concentric radius.
			//
			// GATED on s.nestedContainers (the opt-in toggle, OFF by default): when the
			// feature is off the box paints nothing (CSS keys on body.nkui-nested-boxes),
			// so skip the per-row ancestor walk + boxFolderColor on every pass and just
			// clear any stamp left from when it was on. saveSettings() re-decorates on
			// toggle, so the stamps update the instant it flips. The wrapper has NO
			// recycled-row strip, so the ON branch runs every pass to overwrite/clear.
			// SKIP sink and role (inbox/outbox) folders — they keep their own recede /
			// mouth wash, never a competing box. Box only when a tint resolves — own
			// boxColor OR an already-boxed ancestor to inherit from — so a colourless
			// subtree never restructures its indent without a box to justify it.
			if (wrapper && s.nestedContainers) {
				let depth = 0;
				let ancestorBoxed = false;
				let anc = wrapper.parentElement?.closest<HTMLElement>(".nav-folder") ?? null;
				while (anc && !anc.classList.contains("mod-root")) {
					depth++;
					if (anc.hasAttribute("data-nkui-box")) ancestorBoxed = true;
					anc = anc.parentElement?.closest<HTMLElement>(".nav-folder") ?? null;
				}
				// Box tint: a typed folder uses its own type colour; a plain folder
				// uses ONLY a strict content colour (never the cover/index fallback),
				// so a cover-led hub inherits its parent box's tint instead of a
				// neutral index tone (the model: type-derived, never neutral). This
				// INTENTIONALLY differs from the title's lenient fcolor set above
				// (title = lenient, box = strict) — don't collapse the two writes.
				const boxColor = ftype ? fcolor : this.boxFolderColor(path);
				if (!isSink && !role && (boxColor || ancestorBoxed)) {
					wrapper.style.setProperty("--nkui-depth", String(depth));
					this.setAttr(wrapper, "data-nkui-box", "");
					if (boxColor) wrapper.style.setProperty("--nkui-folder-color", boxColor);
					else wrapper.style.removeProperty("--nkui-folder-color");
				} else {
					this.setAttr(wrapper, "data-nkui-box", null);
					wrapper.style.removeProperty("--nkui-depth");
					wrapper.style.removeProperty("--nkui-folder-color");
				}
			} else if (wrapper && wrapper.hasAttribute("data-nkui-box")) {
				// Feature off but a stale stamp remains (just toggled off) — clear it.
				this.setAttr(wrapper, "data-nkui-box", null);
				wrapper.style.removeProperty("--nkui-depth");
				wrapper.style.removeProperty("--nkui-folder-color");
			}

			// (f) session host — a folder matching the kit's session subfolder. The
			// literal comes from CONFIG (§ Subfolders `<sessions>`, via facts) so a
			// renamed sessions folder is tracked, not a hard-coded name; the bare
			// "sessions"/singular name is only the CONFIG-less / token-absent
			// fallback. Stamped so the lead stylesheet can give it and its children
			// the same size/indent as other entries (not the smaller subfolder look).
			const sessLit = facts?.sessionsLiteral ? facts.sessionsLiteral.toLowerCase() : "";
			const isSessionHost = sessLit
				? bare === sessLit
				: bare === "sessions" || (bare.endsWith("s") && bare.slice(0, -1) === "session");
			this.setAttr(el, "data-nkui-session-host", isSessionHost ? "" : null);

			// Recycled-row hygiene: the explorer reuses elements while scrolling,
			// so a folder title can surface still carrying a previous FILE row's
			// marks — clear everything a folder never owns, so stale decoration
			// can't appear to follow the scroll. Only on a path swap (see above).
			if (recycled) {
				for (const a of [
					"data-nkui-type",
					"data-nkui-reviewed",
					"data-nkui-queue",
					"data-nkui-queue-active",
					"data-nkui-float",
					"data-nkui-cover",
					"data-nkui-weight",
					"data-nkui-nonmd",
					"data-nkui-session-child",
				]) {
					el.removeAttribute(a);
				}
				el.querySelector(".nkui-weight-badge")?.remove();
				const staleQ = el.querySelector(".nkui-queue-count");
				if (staleQ) {
					staleQ.remove();
					// The queue badge carried its tooltip on the ROW (setQueueBadge).
					el.removeAttribute("aria-label");
					el.removeAttribute("title");
				}
				el.style.removeProperty("--nkui-weight-heat");
				// A stale inbox pill on a folder that is NOT an inbox (updateCounts
				// only ever touches rows matching the configured inbox paths).
				if (!s.inboxFolders.includes(path)) el.querySelector(".nkui-inbox-count")?.remove();
			}
		}

		// (a2) uncategorized — a vault-ROOT item the kit doesn't recognise: a root
		// folder outside the CONFIG root order (and not a sink), or a root file
		// other than the two the kit knows (the configured queue files). The
		// stylesheet gives these the mid-dim look; nested items are never tagged.
		let uncat = false;
		if (!path.includes("/")) {
			uncat = isFolder
				? !isSink && this.rootRank(path, true) === 600
				: path !== s.userQueuePath && path !== s.machineQueuePath;
		}
		this.setAttr(el, "data-nkui-uncat", uncat ? "" : null);

		// (b) empty = quiet — a 0-byte file or a childless folder is stamped so
		// the stylesheet can render it dimmer and smaller (the sink treatment,
		// keeping the type colour). Stamped fresh every pass, so the mark clears
		// the moment content or a child arrives — and recycling can't carry it.
		const af = this.plugin.app.vault.getAbstractFileByPath(path);
		const empty = isFolder
			? af instanceof TFolder && af.children.length === 0
			: af instanceof TFile && af.stat.size === 0;
		this.setAttr(el, "data-nkui-empty", empty ? "" : null);

		// (c) type + (d) reviewed + (e) queue surface + (w) weight — files only
		if (!isFolder) {
			// The configured queues (user/machine) are interaction surfaces, not
			// content: tag them so the stylesheet floats them to the top of their
			// folder and gives them the same accent as their For You buckets.
			const isQueue = path === s.userQueuePath || path === s.machineQueuePath;
			this.setAttr(el, "data-nkui-queue", isQueue ? "true" : null);

			// Recycled-row hygiene (the file-row mirror of the folder branch):
			// drop folder-only marks and another path's badges so a reused element
			// never shows the previous row's decoration. Only on a path swap.
			if (recycled) {
				this.setAttr(el, "data-nkui-folder-type", null);
				el.removeAttribute("data-nkui-role");
				el.removeAttribute("data-nkui-session-host");
				el.style.removeProperty("--nkui-folder-color");
				el.querySelector(".nkui-inbox-count")?.remove();
				if (!isQueue) {
					const staleQ = el.querySelector(".nkui-queue-count");
					if (staleQ) {
						staleQ.remove();
						// The queue badge carried its tooltip on the ROW (setQueueBadge).
						el.removeAttribute("aria-label");
						el.removeAttribute("title");
					}
				}
			}

			const fm = this.plugin.app.metadataCache.getCache(path)?.frontmatter;
			let typeHit: string | null = null;
			if (s.enableTypeStyling && fm) {
				const t = fm[s.typeField];
				if (t != null && this.typeColors.has(String(t))) typeHit = String(t);
			}
			this.setAttr(el, "data-nkui-type", typeHit);

			// (e4) non-markdown — an asset/binary file row renders diminutive
			// (sink-like dim + small; the stylesheet keys on the attribute). The
			// queue files are markdown, so they can never carry it. Stamped fresh
			// every pass, so recycling can't carry it onto an md row.
			this.setAttr(el, "data-nkui-nonmd", /\.md$/i.test(path) ? null : "");

			// (e2) float-to-top — a file whose type is in the configured set sorts to
			// the top of its folder (the sort patch; see patchExplorerSort). The
			// attribute stays for the stylesheet. Uses the raw type, colour or not.
			const rawType = fm?.[s.typeField] != null ? String(fm[s.typeField]) : null;
			this.setAttr(el, "data-nkui-float", rawType && this.floatSet.has(rawType) ? "true" : null);

			// (f2) session child — a file directly inside a sessions-host folder
			// inherits the unification look (same size/indent as any other entry).
			// Derive the parent folder path and check for a session-host marker.
			const parentPath = path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
			const parentBare = parentPath.includes("/")
				? parentPath.split("/").pop()?.replace(/^\d{2,}[ _-]+/, "").toLowerCase() ?? ""
				: parentPath.replace(/^\d{2,}[ _-]+/, "").toLowerCase();
			const sessLitC = this.plugin.kitFacts?.sessionsLiteral
				? this.plugin.kitFacts.sessionsLiteral.toLowerCase()
				: "";
			const isSessionChild = sessLitC
				? parentBare === sessLitC
				: parentBare === "sessions" || (parentBare.endsWith("s") && parentBare.slice(0, -1) === "session");
			this.setAttr(el, "data-nkui-session-child", isSessionChild ? "" : null);

			// (e3) cover note — BOTH conventions: a 00-/01- structural prefix (the
			// legacy numeric scheme) and the folder-note (basename equals its folder's
			// name, the plain scheme), plus any explicit index-typed file. Drives
			// float rank 150 — below the floated typed files; the queues sit above
			// both — and gives the stylesheet an emphasis hook. Queues are
			// interaction surfaces, never covers.
			const parentName = path.includes("/") ? path.split("/").slice(-2, -1)[0] : "";
			const isCover = !isQueue && (this.isCoverName(name, parentName) || rawType === "index");
			this.setAttr(el, "data-nkui-cover", isCover ? "true" : null);

			// Review state is tri-state: "false" on an unreviewed draft (needs you,
			// gets the draft marker — gated on showRowBadge); "true" on an APPROVED
			// draft still sitting in an inbox folder (reviewed, but the filing agent
			// hasn't moved it yet) so the stylesheet condenses it like a sink; null
			// once filed or for ordinary content.
			let reviewedAttr: string | null = null;
			if (s.enableReviewFlags && fm) {
				if (isUnreviewed(fm, s.reviewedField)) {
					reviewedAttr = s.showRowBadge ? "false" : null;
				} else if (
					isApproved(fm, s.reviewedField) &&
					s.inboxFolders.some((f) => path === f || path.startsWith(`${f}/`))
				) {
					reviewedAttr = "true";
				}
			}
			this.setAttr(el, "data-nkui-reviewed", reviewedAttr);

			// (w) standard weight — frontmatter `weight` > 0 stamps the row and
			// appends a small w<n> badge. The per-folder heat variable
			// (--nkui-weight-heat) is applied during reorder, where the whole
			// sibling set is in hand. Idempotent: text updates in place; the badge
			// and attribute go when the weight does.
			const w = this.weightValue(fm);
			this.setAttr(el, "data-nkui-weight", w > 0 ? String(w) : null);
			let badge = el.querySelector<HTMLElement>(".nkui-weight-badge");
			if (w > 0) {
				// Long/short twins (the waitnote pattern): "weight: N" where the
				// row has room, "wN" on mobile — the stylesheet picks; both
				// truncate with an ellipsis rather than hard-cutting.
				if (!badge || !badge.querySelector(".nkui-weight-badge-long")) {
					badge?.remove();
					badge = el.createSpan({ cls: "nkui-weight-badge" });
					badge.createSpan({ cls: "nkui-weight-badge-long" });
					badge.createSpan({ cls: "nkui-weight-badge-short" });
				}
				const long = badge.querySelector<HTMLElement>(".nkui-weight-badge-long");
				const short = badge.querySelector<HTMLElement>(".nkui-weight-badge-short");
				const text = `weight: ${w}`;
				if (long && long.textContent !== text) long.setText(text);
				if (short && short.textContent !== `w${w}`) short.setText(`w${w}`);
				const tip = "standard weight — how many times the kit has re-derived this rule; heavier sorts first";
				badge.setAttr("aria-label", tip);
				badge.setAttr("title", tip);
			} else {
				badge?.remove();
				// An unweighted row carries no heat — clear a recycled element's
				// leftover wash now; applyFolderStyles re-scales the weighted rows.
				el.style.removeProperty("--nkui-weight-heat");
			}
		}

		// Restore a display name a LEGACY build rewrote (prefix hiding is gone) —
		// harmless when the marker is absent.
		const content = el.querySelector<HTMLElement>(
			isFolder ? ".nav-folder-title-content" : ".nav-file-title-content"
		);
		if (content && content.dataset.nkuiOrig !== undefined) {
			content.textContent = content.dataset.nkuiOrig;
			delete content.dataset.nkuiOrig;
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
			const draft = fm ? isUnreviewed(fm, s.reviewedField) : false;
			if (segs.length === 1) {
				if (draft) looseDrafts++;
				return;
			}
			const container = segs[0];
			if (segs.length === 2) {
				const arr = roots.get(container) ?? [];
				arr.push({ name: segs[1], approved: fm ? isApproved(fm, s.reviewedField) : false });
				roots.set(container, arr);
			}
			if (draft) draftsByContainer.set(container, (draftsByContainer.get(container) ?? 0) + 1);
		});

		let n = looseDrafts;
		for (const [container, count] of draftsByContainer) {
			const members = roots.get(container) ?? [];
			const gate = pickGateName(members.map((m) => m.name), container);
			const awaitingFiling = !!gate && (members.find((m) => m.name === gate)?.approved ?? false);
			if (!awaitingFiling) n += count;
		}
		return n;
	}

	/**
	 * Colour an open plain folder borrows for its TITLE: the type colour of its
	 * TOP-MOST NON-INDEX typed direct child file, by visible order — the same rank
	 * the sort patch applies (queues, floated types, covers, then native order;
	 * plain name order when ordering is off). The folder reads its kind from the
	 * content that leads it; only when no other typed child carries a colour does
	 * it fall back to the cover/index note's own type. Both cover conventions are
	 * recognised (##-prefix and folder-note basename). Null when nothing typed
	 * carries a colour.
	 */
	private topChildTypeColor(folderPath: string): string | null {
		const { primary, cover } = this.folderTypeColors(folderPath);
		return primary ?? cover;
	}

	/**
	 * Colour an open plain folder borrows for its container BOX (the nested-folder
	 * highlight): the STRICT top-child content colour only — never the cover/index
	 * fallback. A hub folder led solely by its index note returns null here, so the
	 * box inherits its PARENT box's colour through CSS custom-property inheritance
	 * rather than painting a neutral index tone (the model: type-derived, never
	 * neutral). The TITLE still uses topChildTypeColor's lenient fallback.
	 */
	private boxFolderColor(folderPath: string): string | null {
		return this.folderTypeColors(folderPath).primary;
	}

	/** {primary, cover} colours a folder borrows, cached per pass. primary = the
	 * top-most NON-index typed child's colour by visible order; cover = the
	 * cover/index note's own type colour (the fallback). */
	private folderTypeColors(folderPath: string): { primary: string | null; cover: string | null } {
		const cached = this.folderColorCache.get(folderPath);
		if (cached !== undefined) return cached;
		const res = this.computeFolderTypeColors(folderPath);
		this.folderColorCache.set(folderPath, res);
		return res;
	}

	private computeFolderTypeColors(folderPath: string): { primary: string | null; cover: string | null } {
		const folder = this.plugin.app.vault.getAbstractFileByPath(folderPath);
		if (!(folder instanceof TFolder)) return { primary: null, cover: null };
		const files = folder.children
			.filter((c): c is TFile => c instanceof TFile && c.extension === "md")
			.sort((a, b) => a.name.localeCompare(b.name));
		const ordered = this.floatEnabled
			? [...files].sort(
					(a, b) =>
						this.fileRank(a.path, a.basename, folder.name) -
						this.fileRank(b.path, b.basename, folder.name)
				)
			: files;
		let cover: string | null = null;
		for (const f of ordered) {
			const t = this.typeOfPath(f.path);
			if (!t) continue;
			const color = this.plugin.typeStyles().find((x) => x.type === t)?.color;
			if (!color) continue;
			if (t === "index" || this.isCoverName(f.basename, folder.name)) {
				cover ??= color;
				continue;
			}
			return { primary: color, cover };
		}
		return { primary: null, cover };
	}

	// ── ordering ─────────────────────────────────────────────────────────────

	/**
	 * Wrap each file-explorer view's `getSortedFolderItems(folder)` (internal
	 * API — the method the explorer calls to lay out a folder's child items, the
	 * same hook the Custom File Explorer Sorting plugin patches) so rows are
	 * BORN in kit order: the wrapper calls the original, then stable-sorts the
	 * result with the rank rules (rootRank / subfolderRank / fileRank), ranking
	 * off the items' TFile/TFolder objects. Patched on the view INSTANCE (an own
	 * property shadowing the prototype), saved for exact restore on stop().
	 *
	 * Defensive: if the method is absent (a future Obsidian rebuild), warn once
	 * and leave the order native — never throw. The wrapper itself reads
	 * floatEnabled live, so switching ordering off needs no unpatch, just a
	 * re-sort.
	 *
	 * Returns true when a NEW view got wrapped (the caller then triggers one
	 * re-sort so the fresh leaf takes kit order immediately).
	 */
	private patchExplorerSort(): boolean {
		let patched = false;
		for (const leaf of this.plugin.app.workspace.getLeavesOfType("file-explorer")) {
			const view = leaf.view as unknown as Record<string, unknown>;
			if (this.sortPatched.has(view)) continue;
			const original = view["getSortedFolderItems"];
			if (typeof original !== "function") {
				if (!this.sortPatchWarned) {
					this.sortPatchWarned = true;
					console.warn(
						"note-kit-ui: file-explorer getSortedFolderItems not found — explorer keeps Obsidian's native order"
					);
				}
				continue;
			}
			const hadOwn = Object.prototype.hasOwnProperty.call(view, "getSortedFolderItems");
			this.sortPatched.set(view, { hadOwn, original });
			const dec = this; // eslint-disable-line @typescript-eslint/no-this-alias
			view["getSortedFolderItems"] = function (this: unknown, folder: unknown, ...rest: unknown[]): unknown {
				const items = (original as (...a: unknown[]) => unknown).call(this, folder, ...rest);
				try {
					if (!dec.floatEnabled || !Array.isArray(items) || !(folder instanceof TFolder)) {
						return items;
					}
					return dec.sortFolderItems(folder, items);
				} catch {
					return items; // never break the explorer for an ordering nicety
				}
			};
			patched = true;
		}
		return patched;
	}

	/** Restore every wrapped view's original getSortedFolderItems. */
	private unpatchExplorerSort(): void {
		for (const [view, p] of this.sortPatched) {
			if (p.hadOwn) view["getSortedFolderItems"] = p.original;
			else delete view["getSortedFolderItems"]; // the prototype's own shows again
		}
		this.sortPatched.clear();
	}

	/** Stable-sort one folder's child items (the explorer's tree items, each
	 * carrying a `file` TFile/TFolder) into kit order. Equal ranks keep
	 * Obsidian's native order — Array.sort is stable. */
	private sortFolderItems(folder: TFolder, items: unknown[]): unknown[] {
		const isRoot = folder.isRoot();
		const ranks = new Map<unknown, number>();
		for (const it of items) {
			const af = (it as { file?: unknown } | null)?.file;
			let r = isRoot ? 600 : 300;
			if (af instanceof TFolder) {
				r = isRoot ? this.rootRank(af.path, true) : this.subfolderRank(af.path);
			} else if (af instanceof TFile) {
				// Rank base = the name minus a .md extension ONLY (matching the old
				// data-path derivation): a non-md file keeps its extension, so e.g.
				// "Cover.png" can never read as the folder note "Cover".
				const base = af.extension.toLowerCase() === "md" ? af.basename : af.name;
				r = isRoot ? this.rootRank(af.path, false) : this.fileRank(af.path, base, folder.name);
			}
			ranks.set(it, r);
		}
		return [...items].sort((a, b) => (ranks.get(a) ?? 900) - (ranks.get(b) ?? 900));
	}

	/**
	 * Per-folder style pass over a children container: weight heat only. Row
	 * order is never touched here — rows are born sorted via the patched
	 * getSortedFolderItems (patchExplorerSort); the post-hoc insertBefore walk
	 * that used to live here made every explorer re-render visibly shuffle.
	 */
	private applyFolderStyles(children: HTMLElement): void {
		const isItem = (n: Node | null): n is HTMLElement =>
			n instanceof HTMLElement &&
			(n.classList.contains("nav-folder") || n.classList.contains("nav-file"));
		const items = Array.from(children.children).filter(isItem);
		if (!items.length) return;
		// Weight heat is per-folder by design — the sibling set is in hand here, so
		// scaling against the heaviest sibling costs one metadataCache pass per
		// folder instead of anything vault-wide. Runs regardless of float ordering.
		this.applyWeightHeat(items);
	}

	/** Float rank of a file within its folder: 50 queue file — the kit's two
	 * special docs lead everything, floated types and covers included ·
	 * 100-band floated types (ordered by CONFIG § Types row order, clamped to
	 * 100–149) · 150 cover/index — typed floats lead, so a project's
	 * project-typed file sits ABOVE the folder note — · 300 everything else.
	 * When sortByWeight is on, a positive frontmatter `weight` subtracts a
	 * fraction < 1, ordering weighted siblings by weight descending WITHIN
	 * their band (a fraction can never cross bands). Shared with
	 * topChildTypeColor so the borrowed folder colour follows the visible
	 * order exactly. */
	private fileRank(path: string, base: string, parentName: string): number {
		const s = this.plugin.settings;
		let rank: number;
		if (path === s.userQueuePath || path === s.machineQueuePath) rank = 50;
		else {
			const t = this.typeOfPath(path);
			if (t && this.floatSet.has(t) && t !== "index" && !this.isCoverName(base, parentName)) {
				rank = 100 + Math.min(this.typeOrderMap.get(t) ?? 99, 49);
			} else if (t === "index" || this.isCoverName(base, parentName)) {
				rank = 150;
			} else {
				rank = 300;
			}
		}
		if (s.sortByWeight) {
			const w = this.weightOfPath(path);
			// w/(w+1) is monotonic in w and < 1: heavier sorts earlier, same band.
			if (w > 0) rank -= w / (w + 1);
		}
		return rank;
	}

	/** Subfolders order by CONFIG § Subfolders row order (a typed subfolder —
	 * Notes, Sessions, Plans … — sits where its table row sits); folders the
	 * table doesn't name keep native order after them, in the same band as
	 * plain files. */
	private subfolderRank(path: string): number {
		const name = (path.split("/").pop() ?? path).toLowerCase();
		const idx =
			this.subOrderMap.get(name) ??
			this.subOrderMap.get(name.replace(/^\d{2,}[-_ ]+/, ""));
		return idx == null ? 300 : 250 + Math.min(idx, 49);
	}

	/** Rank for the vault root's direct children: CONFIG § Folders table-row
	 * order when CONFIG is read (the table IS the order — Inbox first, Archive
	 * last, because that's how the rows are written); unrecognised folders and
	 * root-level files keep native order after the configured set. Falls back
	 * to the fixed semantic order for CONFIG-less installs. */
	private rootRank(path: string, isFolder: boolean): number {
		if (!isFolder) return 600; // root-level files keep native order
		const f = this.plugin.kitFacts;
		if (f) {
			if (path === f.inboxLiteral) return 0;
			if (path === f.outboxLiteral) return 1;
		}
		const lower = path.toLowerCase();
		const fromConfig = this.rootOrderMap.get(lower) ?? this.rootOrderMap.get(lower.replace(/^\d{2,}[-_ ]+/, ""));
		if (fromConfig != null) return fromConfig;
		if (this.rootOrderMap.size) return 600; // configured order exists; unknown → native band
		const key = lower.replace(/^\d{2,}[-_ ]+/, "");
		const k = key.endsWith("s") ? key.slice(0, -1) : key;
		switch (k) {
			case "inbox":
				return 0;
			case "outbox":
				return 1;
			case "project":
				return 2;
			case "area":
				return 3;
			case "reference":
				return 4;
			case "snippet":
				return 5;
			case "archive":
				return 800;
			default:
				return 600;
		}
	}

	/** A folder's cover note, by either convention: a 00-/01- structural prefix
	 * (the kit's numeric scheme), or a basename equal to its parent folder's name
	 * with the folder's own prefix ignored — so "Areas.md" covers both "Areas"
	 * and "02-Areas" (the folder-note convention of the plain scheme). */
	private isCoverName(base: string, parentName: string): boolean {
		if (/^0[01][-_ ]/.test(base)) return true;
		if (!parentName) return false;
		if (base === parentName) return true;
		return base === parentName.replace(/^\d{2,}[-_ ]+/, "");
	}

	/** The frontmatter type of a file path, as a string, or null. */
	private typeOfPath(path: string): string | null {
		const t = this.plugin.app.metadataCache.getCache(path)?.frontmatter?.[
			this.plugin.settings.typeField
		];
		return t == null ? null : String(t);
	}

	/** A frontmatter block's numeric `weight`, normalised: a finite number (or
	 * numeric string) > 0, else 0. */
	private weightValue(fm: Record<string, unknown> | undefined): number {
		const v = fm?.["weight"];
		const n = typeof v === "number" ? v : typeof v === "string" ? Number(v) : NaN;
		return Number.isFinite(n) && n > 0 ? n : 0;
	}

	/** The `weight` of an md file path via the metadataCache, or 0. */
	private weightOfPath(path: string): number {
		if (!/\.md$/i.test(path)) return 0;
		return this.weightValue(this.plugin.app.metadataCache.getCache(path)?.frontmatter);
	}

	/** Per-folder weight heat: each weighted FILE row gets
	 * `--nkui-weight-heat` scaled linearly against the heaviest sibling present
	 * (max = 1), so the stylesheet can tint most-to-least like the Active list.
	 * Unweighted rows lose the variable. Folder rows never carry heat. */
	private applyWeightHeat(items: HTMLElement[]): void {
		const rows: { title: HTMLElement; w: number }[] = [];
		let max = 0;
		for (const it of items) {
			const title = it.querySelector<HTMLElement>(
				":scope > .nav-file-title, :scope > .nav-folder-title"
			);
			if (!title) continue;
			const w = it.classList.contains("nav-file")
				? this.weightOfPath(title.getAttribute("data-path") ?? "")
				: 0;
			rows.push({ title, w });
			if (w > max) max = w;
		}
		for (const { title, w } of rows) {
			if (w > 0 && max > 0) {
				const heat = String(Math.round((w / max) * 1000) / 1000);
				if (title.style.getPropertyValue("--nkui-weight-heat") !== heat) {
					title.style.setProperty("--nkui-weight-heat", heat);
				}
			} else {
				title.style.removeProperty("--nkui-weight-heat");
			}
		}
	}

	/** Ask the file explorer to re-sort itself (best-effort, internal API) —
	 * the rebuild routes through the (patched) getSortedFolderItems, so this is
	 * both "apply kit order now" (after patching, after a rank change) and
	 * "return to native order" (ordering off, plugin unloading). The view's
	 * `requestSort` is the modern trigger; `sort` the older one. */
	private resortExplorer(): void {
		for (const leaf of this.plugin.app.workspace.getLeavesOfType("file-explorer")) {
			const v = leaf.view as unknown as { requestSort?: () => void; sort?: () => void };
			try {
				if (typeof v.requestSort === "function") v.requestSort();
				else v.sort?.();
			} catch {
				// internal API absent — order settles on the next vault event
			}
		}
	}

	// ── teardown ─────────────────────────────────────────────────────────────

	private clearAll(): void {
		for (const c of this.containers()) {
			c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title").forEach((el) => {
				el.removeAttribute("data-nkui-prefix"); // legacy builds stamped it
				el.removeAttribute("data-nkui-type");
				el.removeAttribute("data-nkui-reviewed");
				el.removeAttribute("data-nkui-queue");
				el.removeAttribute("data-nkui-queue-active");
				el.removeAttribute("data-nkui-float");
				el.removeAttribute("data-nkui-cover");
				el.removeAttribute("data-nkui-sink");
				el.removeAttribute("data-nkui-role");
				el.removeAttribute("data-nkui-uncat");
				el.removeAttribute("data-nkui-empty");
				el.removeAttribute("data-nkui-nonmd");
				el.removeAttribute("data-nkui-weight");
				el.removeAttribute("data-nkui-session-host");
				el.removeAttribute("data-nkui-session-child");
				el.style.removeProperty("--nkui-weight-heat");
				el.style.removeProperty("--nkui-row-indent");
				el.querySelector(":scope > .nkui-row-band")?.remove();
				const content = el.querySelector<HTMLElement>(
					".nav-folder-title-content, .nav-file-title-content"
				);
				// Legacy prefix-hiding rewrote names; restore any marker still present.
				if (content && content.dataset.nkuiOrig !== undefined) {
					content.textContent = content.dataset.nkuiOrig;
					delete content.dataset.nkuiOrig;
				}
			});
			c.querySelectorAll<HTMLElement>(".nav-folder[data-nkui-sink]").forEach((el) =>
				el.removeAttribute("data-nkui-sink")
			);
			c.querySelectorAll<HTMLElement>(".nav-folder[data-nkui-box]").forEach((el) => {
				el.removeAttribute("data-nkui-box");
				el.style.removeProperty("--nkui-depth");
				el.style.removeProperty("--nkui-folder-color");
			});
			c.querySelectorAll<HTMLElement>(".nav-folder-title[data-nkui-folder-type]").forEach((el) => {
				el.removeAttribute("data-nkui-folder-type");
				el.style.removeProperty("--nkui-folder-color");
			});
			c.querySelectorAll<HTMLElement>(
				".nkui-inbox-count, .nkui-queue-count, .nkui-weight-badge"
			).forEach((b) => b.remove());
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
