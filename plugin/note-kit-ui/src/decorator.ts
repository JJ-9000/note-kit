import { TFile, TFolder, Vault, debounce } from "obsidian";
import type NoteKitUiPlugin from "./main";

/**
 * Decorates file-explorer rows with data attributes the stylesheet keys off:
 *   data-nkui-prefix   (a) numeric structural prefix
 *   data-nkui-type     (c) note type (only for types with a configured colour)
 *   data-nkui-reviewed (d) "false" on unreviewed drafts
 * Also rewrites displayed names to hide the prefix (b) and injects the live
 * "needs you" pill on inbox folder rows (d) — drafts awaiting a decision, not
 * sets already approved and waiting on the filing-agent.
 *
 * Also ORDERS the explorer when float-to-top is on (settings.floatTopTypes):
 * inside each folder the cover/index note leads, then floated types, then the
 * queues; at the vault root the kit folders take their semantic order (inbox →
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
	private redraw: () => void;

	private prefixSet = new Set<string>();
	private sinkSet = new Set<string>();
	private typeColors = new Set<string>();
	private floatSet = new Set<string>();
	private floatEnabled = false;
	private hideRe: RegExp | null = null;

	constructor(plugin: NoteKitUiPlugin) {
		this.plugin = plugin;
		this.redraw = debounce(() => this.decorateAll(), 150, false);
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
				this.decorateByPath(file.path);
				this.redraw();
			})
		);
		this.plugin.registerEvent(app.metadataCache.on("resolved", this.redraw));
		this.plugin.registerEvent(
			app.vault.on("rename", (file) => {
				this.decorateByPath(file.path);
				this.redraw();
			})
		);
		this.plugin.registerEvent(app.vault.on("create", this.redraw));
		this.plugin.registerEvent(app.vault.on("delete", this.redraw));
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
		this.prefixSet = new Set(s.prefixStyles.map((p) => p.prefix));
		// A "sink" is a folder whose prefix rule fades it (opacity < 1, e.g. 99-).
		// Tagging the wrapper lets the stylesheet dim its contents to match.
		this.sinkSet = new Set(
			s.dimSinkContents
				? s.prefixStyles
						.filter((p) => Number.isFinite(p.opacity) && p.opacity < 1)
						.map((p) => p.prefix)
				: []
		);
		this.typeColors = new Set(this.plugin.typeStyles().filter((t) => t.color).map((t) => t.type));
		this.floatSet = new Set(s.floatTopTypes ?? []);
		// One gate for ALL explorer ordering (per-folder float AND the root's
		// semantic folder order): the "Float types to top" setting, off when empty.
		this.floatEnabled = this.floatSet.size > 0;
		if (s.enableHidePrefix && this.prefixSet.size) {
			const alt = [...this.prefixSet].map((p) => p.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|");
			this.hideRe = new RegExp(`^(?:${alt})[ _-]+`);
		} else {
			this.hideRe = null;
		}
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
				if (m.target.matches(".nav-file-title, .nav-folder-title")) this.decorate(m.target, s);
				continue;
			}
			if (m.target instanceof HTMLElement) {
				const cc = m.target.closest<HTMLElement>(".nav-folder-children");
				if (cc) toOrder.add(cc);
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
		this.redraw();
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
		const s = this.plugin.settings;
		for (const c of this.containers()) {
			const titles = c.querySelectorAll<HTMLElement>(".nav-file-title, .nav-folder-title");
			titles.forEach((el) => this.decorate(el, s));
			// Settle ordering everywhere (root included — the mod-root wrapper has a
			// .nav-folder-children like any other folder). Idempotent: an ordered
			// container moves nothing.
			c.querySelectorAll<HTMLElement>(".nav-folder-children").forEach((cc) =>
				this.reorderChildren(cc)
			);
		}
		void this.updateCounts();
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
		const isFolder = el.classList.contains("nav-folder-title");
		const leaf = path.split("/").pop() ?? path;
		const name = isFolder ? leaf : leaf.replace(/\.md$/i, "");

		// (a) prefix — only configured prefixes; never matches YYYY dates
		const m = name.match(/^(\d{2,})[ _-]/);
		const matched = m && this.prefixSet.has(m[1]) ? m[1] : null;
		this.setAttr(el, "data-nkui-prefix", s.enablePrefixStyling ? matched : null);

		// (a2) sink folder — tag the wrapping .nav-folder so the stylesheet can
		// fade everything *inside* an expanded sink (e.g. 99-Archive). The folder's
		// own row is still styled by (a); this only reaches its descendants.
		if (isFolder) {
			const wrapper = el.closest<HTMLElement>(".nav-folder");
			const sinkHit = matched && this.sinkSet.has(matched) ? matched : null;
			if (wrapper) this.setAttr(wrapper, "data-nkui-sink", sinkHit);

			// Type-named folder colouring: a folder whose name — with any numeric
			// prefix stripped — is a note type (02-Projects or plain Projects →
			// project) is tinted by that type, brighter than the files inside
			// (stylesheet). Singularise the plural folder name to match the singular
			// type. Works identically for the prefixed and plain naming schemes.
			const fm = name.match(/^\d{2,}[ _-]+(.+)$/);
			const bare = (fm ? fm[1] : name).toLowerCase();
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
		}

		// (c) type + (d) reviewed + (e) queue surface — files only
		if (!isFolder) {
			// The configured queues (user/machine) are interaction surfaces, not
			// content: tag them so the stylesheet floats them to the top of their
			// folder and gives them the same accent as their For You buckets.
			const isQueue = path === s.userQueuePath || path === s.machineQueuePath;
			this.setAttr(el, "data-nkui-queue", isQueue ? "true" : null);

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
			// numeric scheme) and the folder-note (basename equals its folder's name,
			// the plain scheme), plus any explicit index-typed file. Drives float
			// rank 0 and gives the stylesheet a hook so a plain-scheme cover can carry
			// the same emphasis prefix styling gives a 00- row. Queues are interaction
			// surfaces, never covers.
			const parentName = path.includes("/") ? path.split("/").slice(-2, -1)[0] : "";
			const isCover = !isQueue && (this.isCoverName(name, parentName) || rawType === "index");
			this.setAttr(el, "data-nkui-cover", isCover ? "true" : null);

			const draft =
				s.enableReviewFlags && s.showRowBadge && fm ? this.isUnreviewed(fm, s) : false;
			this.setAttr(el, "data-nkui-reviewed", draft ? "false" : null);
		}

		// (b) hide prefix — rewrite display text, keep filename on disk
		const content = el.querySelector<HTMLElement>(
			isFolder ? ".nav-folder-title-content" : ".nav-file-title-content"
		);
		if (content) {
			if (s.enableHidePrefix && this.hideRe) {
				const current = content.textContent ?? "";
				const stripped = current.replace(this.hideRe, "");
				if (stripped !== current) {
					if (content.dataset.nkuiOrig === undefined) content.dataset.nkuiOrig = current;
					content.textContent = stripped;
				}
			} else if (content.dataset.nkuiOrig !== undefined) {
				content.textContent = content.dataset.nkuiOrig;
				delete content.dataset.nkuiOrig;
			}
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
	 * reorder applies (covers, floated types, queues, then native order; plain
	 * name order when ordering is off). The folder reads its kind from the
	 * content that leads it; only when no other typed child carries a colour does
	 * it fall back to the cover/index note's own type. Both cover conventions are
	 * recognised (##-prefix and folder-note basename). Null when nothing typed
	 * carries a colour.
	 */
	private topChildTypeColor(folderPath: string): string | null {
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
	 * Inside a folder: cover/index note, floated types, the queues, then
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
		if (!this.floatEnabled) return;
		const isItem = (n: Node | null): n is HTMLElement =>
			n instanceof HTMLElement &&
			(n.classList.contains("nav-folder") || n.classList.contains("nav-file"));
		const items = Array.from(children.children).filter(isItem);
		if (items.length < 2) return;
		const isRoot = children.parentElement?.classList.contains("mod-root") ?? false;
		const ranks = new Map(items.map((el) => [el, this.itemRank(el, isRoot)]));
		// Array.sort is stable: equal ranks keep Obsidian's native order.
		const desired = [...items].sort((a, b) => (ranks.get(a) ?? 9) - (ranks.get(b) ?? 9));
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
		if (!path) return isRoot ? 6 : 3;
		if (isRoot) return this.rootRank(path, el.classList.contains("nav-folder"));
		if (el.classList.contains("nav-folder")) return 3; // subfolders keep native order
		const leaf = path.split("/").pop() ?? path;
		const base = leaf.replace(/\.md$/i, "");
		const parentName = path.includes("/") ? path.split("/").slice(-2, -1)[0] : "";
		return this.fileRank(path, base, parentName);
	}

	/** Float rank of a file within its folder: 0 cover/index · 1 floated type ·
	 * 2 queue file · 3 everything else. Shared with topChildTypeColor so the
	 * borrowed folder colour follows the visible order exactly. */
	private fileRank(path: string, base: string, parentName: string): number {
		const s = this.plugin.settings;
		if (path === s.userQueuePath || path === s.machineQueuePath) return 2;
		const t = this.typeOfPath(path);
		if (t === "index" || this.isCoverName(base, parentName)) return 0;
		if (t && this.floatSet.has(t)) return 1;
		return 3;
	}

	/** Semantic rank for the vault root's direct children: Inbox 0 · Outbox 1 ·
	 * Projects 2 · Areas 3 · References 4 · Snippets 5 · anything unrecognised 6
	 * (kept in native order) · Archive 8, always last. Names resolve through the
	 * kit's CONFIG literals first (so 00-Inbox and plain Inbox both rank), then
	 * by the prefix-stripped, singularised folder name. */
	private rootRank(path: string, isFolder: boolean): number {
		if (!isFolder) return 6; // root-level files keep native order
		const f = this.plugin.kitFacts;
		if (f) {
			if (path === f.inboxLiteral) return 0;
			if (path === f.outboxLiteral) return 1;
		}
		const key = path.replace(/^\d{2,}[-_ ]+/, "").toLowerCase();
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
				return 8;
			default:
				return 6;
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
				el.removeAttribute("data-nkui-prefix");
				el.removeAttribute("data-nkui-type");
				el.removeAttribute("data-nkui-reviewed");
				el.removeAttribute("data-nkui-queue");
				el.removeAttribute("data-nkui-queue-active");
				el.removeAttribute("data-nkui-float");
				el.removeAttribute("data-nkui-cover");
				const content = el.querySelector<HTMLElement>(
					".nav-folder-title-content, .nav-file-title-content"
				);
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
			c.querySelectorAll<HTMLElement>(".nkui-inbox-count, .nkui-queue-count").forEach((b) => b.remove());
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
