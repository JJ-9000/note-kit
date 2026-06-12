import { TFile, TFolder, Vault, debounce, type Debouncer } from "obsidian";
import type NoteKitUiPlugin from "./main";

/**
 * Decorates file-explorer rows with data attributes the stylesheet keys off:
 *   data-nkui-sink     (a) declared sink folder (settings.sinkFolders) — the
 *                      title AND its wrapping .nav-folder carry it, so the
 *                      stylesheet can dim the whole subtree
 *   data-nkui-uncat    (a2) vault-root item the kit doesn't recognise
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
 * Also ORDERS the explorer when float-to-top is on (settings.floatTopTypes):
 * inside each folder the floated types lead (weighted siblings sort by weight
 * desc within their band when settings.sortByWeight is on), then the
 * cover/index note, then the queues; at the vault root the kit folders take
 * their semantic order (inbox →
 * outbox → projects → areas → references → snippets, archive last). Ordering is
 * a physical DOM reorder (insertBefore inside .nav-folder-children) — NEVER a
 * flex/order CSS override on the container, which broke Obsidian's collapse
 * height animation and clipped rows (removed in 0.4.52).
 *
 * All DOM writes are idempotent: re-running on an already-decorated row produces
 * no mutation, and re-running the reorder on an ordered container moves nothing,
 * so the MutationObserver that drives re-decoration cannot loop.
 */
export class ExplorerDecorator {
	private plugin: NoteKitUiPlugin;
	private observers: MutationObserver[] = [];
	private redraw: Debouncer<[], void>;

	/** Declared sink folder names (settings.sinkFolders), prefix-stripped and
	 * lower-cased — a folder anywhere whose own name matches is a sink. */
	private sinkSet = new Set<string>();
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
	 * scrolled away and back). Decoration-only (no counts), debounced trailing
	 * so a fling costs ONE pass after the scroll settles, not work per frame. */
	private scrollRedraw: Debouncer<[], void>;
	/** Containers whose scroll listener is already attached — attachObservers
	 * re-runs on every layout-change and must not stack duplicate listeners. */
	private scrollBound = new WeakSet<HTMLElement>();
	/** The path each element was last decorated as — recycling implies a
	 * data-path swap, so the stale-mark hygiene only needs to run when the
	 * stored path differs; same path means nothing can be stale. */
	private lastDecoratedPath = new WeakMap<HTMLElement, string>();
	/** folderPath → borrowed colour. topChildTypeColor sorts + reads metadata
	 * per call — the heaviest per-row cost in decorate — so each pass pays it
	 * once per unique folder; vault/metadata events invalidate. */
	private folderColorCache = new Map<string, string | null>();

	constructor(plugin: NoteKitUiPlugin) {
		this.plugin = plugin;
		this.redraw = debounce(() => this.decorateAll(), 150, false);
		this.countsRefresh = debounce(() => void this.updateCounts(), 600, false);
		// resetTimer: TRUE — each scroll event pushes the deadline back, so the
		// pass runs once on settle; with false this is a 10Hz throttle that runs
		// the full pass DURING the fling, the exact churn the counts-only fix
		// removed.
		this.scrollRedraw = debounce(() => this.decorateRendered(), 100, true);
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
			})
		);
		this.plugin.registerEvent(app.metadataCache.on("resolved", this.redraw));
		this.plugin.registerEvent(
			app.vault.on("rename", (file) => {
				this.folderColorCache.clear();
				this.decorateByPath(file.path);
				this.redraw();
			})
		);
		this.plugin.registerEvent(
			app.vault.on("create", () => {
				this.folderColorCache.clear();
				this.redraw();
			})
		);
		this.plugin.registerEvent(
			app.vault.on("delete", () => {
				this.folderColorCache.clear();
				this.redraw();
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
		this.clearAll();
		// Hand the row order back to Obsidian — observers are gone, so this can't loop.
		this.resortExplorer();
	}

	refresh(): void {
		this.compile();
		this.attachObservers();
		this.clearAll();
		// Ordering just switched off → restore native order; reorderChildren alone
		// can't, because it only ever moves rows toward the float layout.
		if (!this.floatEnabled) this.resortExplorer();
		this.decorateAll();
	}

	// ── setup ────────────────────────────────────────────────────────────────

	private compile(): void {
		const s = this.plugin.settings;
		this.folderColorCache.clear(); // type colours / settings may have changed
		// A "sink" is a declared low-attention folder (settings.sinkFolders, e.g.
		// Archive): a folder anywhere whose name — prefix-stripped, case-insensitive
		// — equals an entry. The stylesheet dims its whole subtree.
		this.sinkSet = new Set(
			(s.sinkFolders ?? [])
				.map((n) => n.replace(/^\d{2,}[-_ ]+/, "").trim().toLowerCase())
				.filter(Boolean)
		);
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
		// Containers whose rows changed this batch — reordered once, after every row
		// is decorated. Our own insertBefore moves re-enter here on the next callback,
		// where the already-ordered container produces zero mutations (fixed point),
		// so observing our own reorder converges instead of looping.
		const toOrder = new Set<HTMLElement>();
		for (const m of muts) {
			if (m.type === "attributes" && m.target instanceof HTMLElement) {
				if (m.target.matches(".nav-file-title, .nav-folder-title")) {
					this.decorate(m.target, s);
					// A data-path swap is a RECYCLED row — its folder's weight heat
					// and float order were computed for the old path. Recycling is
					// continuous during a fling, so ordering here per batch would
					// re-attach reorder work to the hottest event stream in the
					// plugin; the settled pass reorders every container instead (a
					// genuine rename settles ≤100ms later, imperceptible).
					this.scrollRedraw();
				}
				continue;
			}
			if (m.target instanceof HTMLElement) {
				const cc = m.target.closest<HTMLElement>(".nav-folder-children");
				if (cc) toOrder.add(cc);
				else {
					// A root-level change (the root items live in an unclassed div,
					// not a .nav-folder-children) — settle the root container.
					const host = m.target.closest<HTMLElement>(".nav-files-container");
					const root = host && this.rootItemsContainer(host);
					if (root && (m.target === root || root.contains(m.target))) toOrder.add(root);
				}
			}
			m.addedNodes.forEach((node) => {
				if (!(node instanceof HTMLElement)) return;
				if (node.matches(".nav-file-title, .nav-folder-title")) this.decorate(node, s);
				node
					.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title")
					.forEach((el) => this.decorate(el, s));
				if (node.matches(".nav-folder-children")) toOrder.add(node);
				node.querySelectorAll<HTMLElement>(".nav-folder-children").forEach((el) => toOrder.add(el));
			});
		}
		for (const cc of toOrder) this.reorderChildren(cc);
		// Counts only — the targeted work above already decorated every touched
		// row. A full decorateAll per mutation batch made scrolling the
		// virtualized explorer (which churns childList constantly) drop frames.
		this.countsRefresh();
	}

	/** Synchronous, targeted decoration of one row by its data-path. */
	private decorateByPath(path: string): void {
		const s = this.plugin.settings;
		const sel = `.nav-file-title[data-path="${cssEscape(path)}"], .nav-folder-title[data-path="${cssEscape(path)}"]`;
		for (const c of this.containers()) {
			const el = c.querySelector<HTMLElement>(sel);
			if (!el) continue;
			this.decorate(el, s);
			// A metadata change can change the row's float rank (type edited) —
			// re-settle its own folder. No-op when the order already holds.
			const cc = el.closest<HTMLElement>(".nav-folder-children");
			if (cc) this.reorderChildren(cc);
		}
	}

	// ── decoration ─────────────────────────────────────────────────────────────

	private decorateAll(): void {
		this.decorateRendered();
		void this.updateCounts();
	}

	/** Decoration + ordering over every RENDERED row, counts excluded — the
	 * virtualized explorer only keeps the visible rows in the DOM, so this is
	 * cheap, and the scroll-settle pass (scrollRedraw) uses it to re-stamp
	 * recycled rows without re-reading the queue files. */
	private decorateRendered(): void {
		const s = this.plugin.settings;
		for (const c of this.containers()) {
			const titles = c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title");
			titles.forEach((el) => this.decorate(el, s));
			// Settle ordering everywhere — the ROOT items' container first (it is
			// NOT a .nav-folder-children: current Obsidian renders root tree-items
			// in an unclassed div directly under .nav-files-container), then every
			// folder's children. Idempotent: an ordered container moves nothing.
			const root = this.rootItemsContainer(c);
			if (root) this.reorderChildren(root);
			c.querySelectorAll<HTMLElement>(".nav-folder-children").forEach((cc) =>
				this.reorderChildren(cc)
			);
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
		// Stale marks exist only when the element was recycled onto a NEW path —
		// same path since the last pass means the hygiene strips below can be
		// skipped (2-3 selector queries + ~8 attribute removals per row per pass
		// add up at a few hundred rendered rows). Regular stamping still runs:
		// frontmatter can change without a path swap.
		const recycled = this.lastDecoratedPath.get(el) !== path;
		this.lastDecoratedPath.set(el, path);
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
			isSink = this.sinkSet.has(bare);
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

			// (e2) float-to-top — a file whose type is in the configured set sorts to
			// the top of its folder (DOM reorder; see reorderChildren). The attribute
			// stays for the stylesheet. Uses the raw type, colour or not.
			const rawType = fm?.[s.typeField] != null ? String(fm[s.typeField]) : null;
			this.setAttr(el, "data-nkui-float", rawType && this.floatSet.has(rawType) ? "true" : null);

			// (e3) cover note — BOTH conventions: a 00-/01- structural prefix (the
			// legacy numeric scheme) and the folder-note (basename equals its folder's
			// name, the plain scheme), plus any explicit index-typed file. Drives
			// float rank 150 — below the floated typed files, above the queues — and
			// gives the stylesheet an emphasis hook. Queues are interaction surfaces,
			// never covers.
			const parentName = path.includes("/") ? path.split("/").slice(-2, -1)[0] : "";
			const isCover = !isQueue && (this.isCoverName(name, parentName) || rawType === "index");
			this.setAttr(el, "data-nkui-cover", isCover ? "true" : null);

			const draft =
				s.enableReviewFlags && s.showRowBadge && fm ? this.isUnreviewed(fm, s) : false;
			this.setAttr(el, "data-nkui-reviewed", draft ? "false" : null);

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
				// leftover wash now; reorderChildren re-scales the weighted rows.
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
			const draft = fm ? this.isUnreviewed(fm, s) : false;
			if (segs.length === 1) {
				if (draft) looseDrafts++;
				return;
			}
			const container = segs[0];
			if (segs.length === 2) {
				const arr = roots.get(container) ?? [];
				arr.push({ name: segs[1], approved: fm ? this.isApproved(fm, s) : false });
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
	 * Colour an open plain folder borrows: the type colour of its TOP-MOST
	 * NON-INDEX typed direct child file, by visible order — the same rank the DOM
	 * reorder applies (floated types, covers, queues, then native order; plain
	 * name order when ordering is off). The folder reads its kind from the
	 * content that leads it; only when no other typed child carries a colour does
	 * it fall back to the cover/index note's own type. Both cover conventions are
	 * recognised (##-prefix and folder-note basename). Null when nothing typed
	 * carries a colour.
	 */
	private topChildTypeColor(folderPath: string): string | null {
		const cached = this.folderColorCache.get(folderPath);
		if (cached !== undefined) return cached;
		const color = this.computeTopChildTypeColor(folderPath);
		this.folderColorCache.set(folderPath, color);
		return color;
	}

	private computeTopChildTypeColor(folderPath: string): string | null {
		const folder = this.plugin.app.vault.getAbstractFileByPath(folderPath);
		if (!(folder instanceof TFolder)) return null;
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
		let coverFallback: string | null = null;
		for (const f of ordered) {
			const t = this.typeOfPath(f.path);
			if (!t) continue;
			const color = this.plugin.typeStyles().find((x) => x.type === t)?.color;
			if (!color) continue;
			if (t === "index" || this.isCoverName(f.basename, folder.name)) {
				coverFallback ??= color;
				continue;
			}
			return color;
		}
		return coverFallback;
	}

	// ── ordering ─────────────────────────────────────────────────────────────

	/**
	 * Float rows to the top by PHYSICALLY reordering the explorer DOM —
	 * insertBefore within the .nav-folder-children container, never CSS. (The
	 * 0.4.52 lesson: display:flex + order on this container fought Obsidian's own
	 * row layout, broke the collapse/expand height animation, and clipped rows.
	 * Moving nodes leaves the container's layout — and so the animation — alone.)
	 *
	 * Inside a folder: floated types, the cover/index note, the queues, then
	 * everything else in native order. At the vault root (the mod-root wrapper's
	 * children): the kit's semantic folder order (rootRank). Only .nav-folder /
	 * .nav-file children ever move — Obsidian's spacer and animation helper nodes
	 * are never touched, and the container itself is never wrapped or restyled.
	 *
	 * Idempotent by fixed point: the desired sequence is computed first and a
	 * node is moved only when out of place, so re-running on an ordered container
	 * performs zero mutations and the MutationObserver converges.
	 */
	private reorderChildren(children: HTMLElement): void {
		const isItem = (n: Node | null): n is HTMLElement =>
			n instanceof HTMLElement &&
			(n.classList.contains("nav-folder") || n.classList.contains("nav-file"));
		const items = Array.from(children.children).filter(isItem);
		if (!items.length) return;
		// Weight heat is per-folder by design — the sibling set is in hand here, so
		// scaling against the heaviest sibling costs one metadataCache pass per
		// folder instead of anything vault-wide. Runs regardless of float ordering.
		this.applyWeightHeat(items);
		if (!this.floatEnabled || items.length < 2) return;
		// Root = any items container that is NOT a folder's .nav-folder-children
		// (the unclassed root div), or the legacy mod-root wrapper's children.
		const isRoot =
			!children.classList.contains("nav-folder-children") ||
			(children.parentElement?.classList.contains("mod-root") ?? false);
		const ranks = new Map(items.map((el) => [el, this.itemRank(el, isRoot)]));
		// Array.sort is stable: equal ranks keep Obsidian's native order.
		const desired = [...items].sort((a, b) => (ranks.get(a) ?? 900) - (ranks.get(b) ?? 900));
		// Minimal-mutation walk: advance a cursor down the live item sequence and
		// pull forward only the nodes that are out of place.
		let cursor: HTMLElement | null = items[0];
		for (const want of desired) {
			if (want === cursor) {
				let n: Node | null = cursor.nextSibling;
				while (n && !isItem(n)) n = n.nextSibling;
				cursor = n;
			} else {
				children.insertBefore(want, cursor);
			}
		}
	}

	/** Rank of one explorer row element for reorderChildren. */
	private itemRank(el: HTMLElement, isRoot: boolean): number {
		const title = el.querySelector<HTMLElement>(
			":scope > .nav-folder-title, :scope > .nav-file-title"
		);
		const path = title?.getAttribute("data-path");
		if (!path) return isRoot ? 600 : 300;
		if (isRoot) return this.rootRank(path, el.classList.contains("nav-folder"));
		if (el.classList.contains("nav-folder")) return this.subfolderRank(path);
		const leaf = path.split("/").pop() ?? path;
		const base = leaf.replace(/\.md$/i, "");
		const parentName = path.includes("/") ? path.split("/").slice(-2, -1)[0] : "";
		return this.fileRank(path, base, parentName);
	}

	/** Float rank of a file within its folder: 100-band floated types (ordered by
	 * CONFIG § Types row order, clamped to 100–149) · 150 cover/index — typed
	 * floats lead, so a project's project-typed file sits ABOVE the folder note —
	 * · 200 queue file · 300 everything else. When sortByWeight is on, a positive
	 * frontmatter `weight` subtracts a fraction < 1, ordering weighted siblings
	 * by weight descending WITHIN their band (a fraction can never cross bands).
	 * Shared with topChildTypeColor so the borrowed folder colour follows the
	 * visible order exactly. */
	private fileRank(path: string, base: string, parentName: string): number {
		const s = this.plugin.settings;
		let rank: number;
		if (path === s.userQueuePath || path === s.machineQueuePath) rank = 200;
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
	 * used when ordering is switched off, so the DOM returns to Obsidian's native
	 * order instead of keeping the last float layout until the next vault event. */
	private resortExplorer(): void {
		for (const leaf of this.plugin.app.workspace.getLeavesOfType("file-explorer")) {
			const v = leaf.view as unknown as { sort?: () => void };
			try {
				v.sort?.();
			} catch {
				// internal API absent — native order returns on the next vault event
			}
		}
	}

	private isUnreviewed(fm: Record<string, unknown>, s = this.plugin.settings): boolean {
		const v = fm[s.reviewedField];
		return v === false || v === "false";
	}

	private isApproved(fm: Record<string, unknown>, s = this.plugin.settings): boolean {
		const v = fm[s.reviewedField];
		return v === true || v === "true";
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
				el.removeAttribute("data-nkui-weight");
				el.style.removeProperty("--nkui-weight-heat");
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

/**
 * Resolve a working set's gate by the names of its root-level members — the
 * name-only twin of nowView's pickGate (the decorator only has names + approval
 * to work with). A lone root file IS the gate; among several, a single
 * 00-prefixed cover wins, else a single folder-note (basename equal to the
 * container, the plain scheme's cover convention), else a single date-named
 * file. Ambiguity resolves to none — no guessing.
 */
function pickGateName(names: string[], containerName: string): string | null {
	const one = (xs: string[]): string | null => (xs.length === 1 ? xs[0] : null);
	return (
		one(names) ??
		one(names.filter((n) => /^00[-_ ]/.test(n))) ??
		one(names.filter((n) => n.replace(/\.md$/i, "") === containerName)) ??
		one(names.filter((n) => /^\d{4}-\d{2}-\d{2}/.test(n)))
	);
}

/** CSS.escape is unavailable in some mobile webviews; fall back to a minimal escape. */
function cssEscape(v: string): string {
	const g = globalThis as unknown as { CSS?: { escape?: (s: string) => string } };
	if (g.CSS?.escape) return g.CSS.escape(v);
	return v.replace(/["\\]/g, "\\$&");
}
