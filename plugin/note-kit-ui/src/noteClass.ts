import { MarkdownView } from "obsidian";
import { typeClass } from "./settings";
import type NoteKitUiPlugin from "./main";

/**
 * Applies an `nkui-type-<type>` class to each open markdown view's container,
 * read from the note's frontmatter `type`. This is the type → cssclasses bridge
 * the kit lacks — done at render time, so no file is ever modified.
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
		this.forEachView((view) => this.clear(view.containerEl));
	}

	refresh(): void {
		this.apply();
	}

	private apply(): void {
		const s = this.plugin.settings;
		const enabled = s.enableTypeStyling && s.applyTypeBodyClass;
		this.forEachView((view) => {
			const el = view.containerEl;
			this.clear(el);
			if (!enabled || !view.file) return;
			const fm = this.plugin.app.metadataCache.getFileCache(view.file)?.frontmatter;
			const t = fm?.[s.typeField];
			if (t != null) el.classList.add(typeClass(String(t)));
		});
	}

	private clear(el: HTMLElement): void {
		const stale = Array.from(el.classList).filter((c) => c.startsWith("nkui-type-"));
		for (const c of stale) el.classList.remove(c);
	}

	private forEachView(fn: (view: MarkdownView) => void): void {
		for (const leaf of this.plugin.app.workspace.getLeavesOfType("markdown")) {
			if (leaf.view instanceof MarkdownView) fn(leaf.view);
		}
	}
}
