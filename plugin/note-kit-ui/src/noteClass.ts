import { MarkdownView } from "obsidian";
import { typeClass } from "./settings";
import { toneVars } from "./palette";
import type NoteKitUiPlugin from "./main";

/**
 * Applies an `nkui-type-<type>` class to each open markdown view's container
 * AND to that leaf's tab header, read from the note's frontmatter `type`. This
 * is the type → cssclasses bridge the kit lacks — done at render time, so no
 * file is ever modified. The tab header is a sibling of the leaf content (out of
 * CSS reach from the container), so it must be stamped directly to tint the tab.
 */
export class NoteClassApplier {
	private plugin: NoteKitUiPlugin;

	constructor(plugin: NoteKitUiPlugin) {
		this.plugin = plugin;
	}

	start(): void {
		const app = this.plugin.app;
		this.plugin.registerEvent(app.workspace.on("active-leaf-change", () => this.apply()));
		this.plugin.registerEvent(app.workspace.on("file-open", () => this.apply()));
		this.plugin.registerEvent(app.workspace.on("layout-change", () => this.apply()));
		this.plugin.registerEvent(
			app.metadataCache.on("changed", () => this.apply())
		);
		this.apply();
	}

	stop(): void {
		this.forEachLeaf((view, tab) => {
			this.clear(view.containerEl);
			if (tab) this.clear(tab);
		});
	}

	refresh(): void {
		this.apply();
	}

	private apply(): void {
		const s = this.plugin.settings;
		const enabled = s.enableTypeStyling && s.applyTypeBodyClass;
		this.forEachLeaf((view, tab) => {
			const el = view.containerEl;
			this.clear(el);
			if (tab) this.clear(tab);
			if (!enabled || !view.file) return;
			const fm = this.plugin.app.metadataCache.getFileCache(view.file)?.frontmatter;
			const t = fm?.[s.typeField];
			if (t != null) {
				const cls = typeClass(String(t));
				el.classList.add(cls);
				if (tab) {
					tab.classList.add(cls);
					// Set the colour + its sub-tones inline on the tab too: the dynamic
					// stylesheet keys them off the class, but the inline values guarantee
					// the tab tint resolves regardless, since the tab lives outside the
					// leaf content where the class rule applies.
					const color = this.plugin.typeStyles().find((x) => x.type === String(t))?.color;
					if (color) {
						tab.style.setProperty("--nkui-type-color", color);
						for (const [k, v] of Object.entries(toneVars(color))) tab.style.setProperty(k, v);
					}
				}
			}
		});
	}

	private clear(el: HTMLElement): void {
		const stale = Array.from(el.classList).filter((c) => c.startsWith("nkui-type-"));
		for (const c of stale) el.classList.remove(c);
		for (const v of ["--nkui-type-color", "--nkui-ts", "--nkui-tm", "--nkui-tk"]) {
			el.style.removeProperty(v);
		}
	}

	private forEachLeaf(fn: (view: MarkdownView, tab: HTMLElement | undefined) => void): void {
		for (const leaf of this.plugin.app.workspace.getLeavesOfType("markdown")) {
			if (leaf.view instanceof MarkdownView) {
				// tabHeaderEl is an undocumented internal handle on WorkspaceLeaf.
				const tab = (leaf as unknown as { tabHeaderEl?: HTMLElement }).tabHeaderEl;
				fn(leaf.view, tab);
			}
		}
	}
}
