import { App, Notice, PluginSettingTab, Setting, TextComponent } from "obsidian";
import type NoteKitUiPlugin from "./main";

/** One note-type rule (feature c). `type` matches the frontmatter `type` value. */
export interface TypeStyle {
	type: string;
	color: string;
}

/** What the right-sidebar leaf hosts (see main.applySidebarNow): the For You
 * page (the shipped default), the user queue, or the machine queue. */
export type SidebarContent = "for-you" | "user-queue" | "machine-queue";

export interface NoteKitUiSettings {
	/** Derive vault structure (inbox/outbox, queue paths, field names, type
	 * vocabulary) from the kit's .claude/CONFIG.md at load, so the plugin cannot
	 * drift from the kit. Off (or no CONFIG present) = the manual fields below
	 * apply. */
	useKitConfig: boolean;

	// Feature toggles
	enableTypeStyling: boolean; // c
	enableReviewFlags: boolean; // d
	hideFolderArrows: boolean; // explorer minimalism — hide collapse chevrons
	foldChildrenWithParent: boolean; // collapsing a folder also collapses its subfolders
	minimalChrome: boolean; // quieten Obsidian's tab bar + nav toolbar (main area only)
	calmReading: boolean; // reading-mode overrides to reduce note noise
	dedupeTabs: boolean; // one tab per document — focus the existing tab instead of opening twins
	animations: boolean; // enable the view's animations (fold grow/shrink, hold fills, check pulse)

	/** Folders that render small and dim with all their contents — cold storage
	 * (the decorator marks them; the static stylesheet carries the look). */
	sinkFolders: string[];
	/** Files carrying a `weight` frontmatter value sort and tint heaviest-first
	 * among their siblings. */
	sortByWeight: boolean;

	// c — per-type styling
	typeField: string;
	typeStyles: TypeStyle[];
	applyTypeBodyClass: boolean; // accent the open note by type
	typeTint: boolean; // tint the open note's title/headings/frontmatter/background by type
	/** Derive type colours from the active theme's palette (--color-red … pink)
	 * at load and on every theme change, instead of the manual list. */
	themePalette: boolean;
	/** Mirror the type colours into the graph view's colour groups (graph.json)
	 * so graph/bubble nodes match the explorer dots. */
	syncGraphColors: boolean;
	/** Explorer: note types whose files sort to the top of their folder, so
	 * folder contents read semantically (cover first, then plans, queues). */
	floatTopTypes: string[];

	// d — unreviewed drafts
	reviewedField: string;
	inboxFolders: string[];
	showRowBadge: boolean; // per-row draft dot
	showInboxCount: boolean; // live "N unreviewed" pill on the inbox folder row

	// "Now" view
	nowOpenOnStartup: boolean;
	nowReplaceNewTab: boolean; // a new empty tab opens For You instead of the blank screen
	nowActiveTypes: string[];
	nowQueueFolders: string[];
	nowCollapsedSections: string[]; // section titles currently folded
	nowExpandedGroups: string[]; // "Section/groupKey" ids the user has opened (groups fold by default)
	userQueuePath: string; // AI → user proposals to check off
	machineQueuePath: string; // user → AI checklist (addable from the view)
	/** Open queue files in the clean queue view instead of the raw note. */
	queueCleanView: boolean;
	/** Keep a leaf in the right sidebar (on mobile the swipe-left drawer). */
	sidebarNow: boolean;
	/** What that right-sidebar leaf hosts — the For You page (default), the
	 * user queue, or the machine queue. */
	sidebarContent: SidebarContent;
	/** One-time default-palette migration has run (see main.migratePalette). */
	paletteMigrated: boolean;
	/** Press-and-hold commit duration in ms (approve / undo holds). */
	holdMs: number;
	/** Replace Obsidian's outline control icons with minimal solid shapes. */
	solidIcons: boolean;
	/** Explorer: render the inbox/outbox rows — the kit's working mouths, where
	 * draft and queue types flow — larger than ordinary rows (body class
	 * nkui-large-mouths; the stylesheet carries the look). */
	largeMouths: boolean;
	/** Round the kit's corners — pills, badges, washes, buttons — to match
	 * iOS/native menus (body class nkui-rounded). Off = the kit's sharp default. */
	roundedCorners: boolean;
	/** Hide the app chrome — tab bar, ribbons, nav buttons, version stamps,
	 * explainer text — leaving the notes (body class nkui-minimal). The settings
	 * path stays visible, so the mode is always exitable. */
	minimalistMode: boolean;
}

/** Defaults seeded from the kit's CONFIG vocabulary (types, inbox path). */
export const DEFAULT_SETTINGS: NoteKitUiSettings = {
	useKitConfig: true,
	enableTypeStyling: true,
	enableReviewFlags: true,
	hideFolderArrows: true,
	foldChildrenWithParent: true,
	minimalChrome: true,
	calmReading: true,
	dedupeTabs: true,
	animations: true,

	sinkFolders: ["Archive"],
	sortByWeight: true,

	typeField: "type",
	// Curated per-type palette tuned for a near-black background (applies when
	// Theme palette is off). One hue per type; soft/operational types use lighter
	// or greyer variants so the primary content types read brightest.
	typeStyles: [
		{ type: "project", color: "#ff2a55" },
		{ type: "area", color: "#ff6a14" },
		{ type: "reference", color: "#00e62e" },
		{ type: "research", color: "#3b82f6" },
		{ type: "plan", color: "#a855f7" },
		{ type: "session", color: "#4d61ec" },
		{ type: "journal", color: "#ff2ea6" },
		{ type: "idea", color: "#ffec80" },
		{ type: "snippet", color: "#5bd8e1" },
		{ type: "source", color: "#5c6bc0" },
		{ type: "index", color: "#ffffff" },
		// note sits brighter than the old #3b4261 — a near-background indigo read
		// as "a little dark" at h1/full-colour weight on the default theme.
		{ type: "note", color: "#8e9bd8" },
		{ type: "voice", color: "#ffea00" },
		{ type: "design", color: "#ff8d0a" },
		{ type: "format", color: "#ffae00" },
		{ type: "addendum", color: "#ff9955" },
		{ type: "log", color: "#3b3b3b" },
		{ type: "revision", color: "#709eff" },
	],
	applyTypeBodyClass: true,
	typeTint: true,
	themePalette: true,
	syncGraphColors: true,
	floatTopTypes: ["project", "index", "plan"],

	reviewedField: "reviewed",
	inboxFolders: ["Inbox"],
	showRowBadge: true,
	showInboxCount: true,

	nowOpenOnStartup: true,
	nowReplaceNewTab: true,
	nowActiveTypes: ["project", "area"],
	nowQueueFolders: ["Outbox"],
	nowCollapsedSections: [],
	nowExpandedGroups: [],
	userQueuePath: "Inbox/User-Queue.md",
	machineQueuePath: "Outbox/Machine-Queue.md",
	queueCleanView: true,
	sidebarNow: true,
	sidebarContent: "for-you",
	paletteMigrated: false,
	holdMs: 395,
	solidIcons: true,
	largeMouths: false,
	roundedCorners: false,
	minimalistMode: false,
};

/** Sanitize a type value into a CSS class suffix. Shared by css + noteClass. */
export function typeClass(type: string): string {
	return "nkui-type-" + type.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

/** Normalize a colour string to a #rrggbb hex the native colour input accepts. */
function toHexColor(c: string): string {
	const v = (c ?? "").trim();
	if (/^#[0-9a-f]{6}$/i.test(v)) return v;
	const short = /^#([0-9a-f])([0-9a-f])([0-9a-f])$/i.exec(v);
	if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`;
	return "#888888";
}

export class NoteKitUiSettingTab extends PluginSettingTab {
	plugin: NoteKitUiPlugin;

	constructor(app: App, plugin: NoteKitUiPlugin) {
		super(app, plugin);
		this.plugin = plugin;
	}

	display(): void {
		const { containerEl } = this;
		containerEl.empty();
		const s = this.plugin.settings;
		const synced = s.useKitConfig && this.plugin.kitFacts !== null;

		const save = async () => {
			await this.plugin.saveSettings();
		};

		/** A kit-fact row: shown read-only while CONFIG drives it, to avoid drift. */
		const kitFact = (parent: HTMLElement, name: string, value: string, manual: () => void) => {
			if (synced) {
				new Setting(parent)
					.setName(name)
					.setDesc(`${value} — derived from the kit's .claude/CONFIG.md; edit CONFIG to change it.`);
			} else {
				manual();
			}
		};

		/** A native <details> collapsible for the long tail; collapsed by default. */
		const advancedGroup = (parent: HTMLElement, label: string): HTMLElement => {
			const details = parent.createEl("details", { cls: "nkui-settings-advanced" });
			const summary = details.createEl("summary", {
				text: label,
				cls: "nkui-settings-advanced-summary",
			});
			summary.style.cursor = "pointer";
			const body = details.createDiv({ cls: "nkui-settings-advanced-body" });
			return body;
		};

		// ── Header: name + running version, so a stale build is spottable ─────
		const header = new Setting(containerEl)
			.setName("Note Kit UI")
			.setDesc(`Version ${this.plugin.manifest.version}`)
			.setHeading();
		header.descEl.addClass("nkui-settings-version");

		// ── Essentials — the controls a user actually reaches for ────────────
		const kitSync = new Setting(containerEl)
			.setName("Follow the kit's CONFIG.md")
			.setDesc(
				synced
					? "On — vault structure (inbox, queues, field names, type vocabulary) is read from .claude/CONFIG.md at load, so the plugin can't drift from the kit. Derived values appear read-only in the advanced groups below."
					: s.useKitConfig
						? "On, but no readable .claude/CONFIG.md was found — the manual fields in the advanced groups apply."
						: "Off — the manual fields in the advanced groups apply."
			)
			.addToggle((t) =>
				t.setValue(s.useKitConfig).onChange(async (v) => {
					s.useKitConfig = v;
					await this.plugin.applyKitFacts();
					await save();
					this.display();
				})
			);
		if (s.useKitConfig) {
			kitSync.addButton((b) =>
				b
					.setButtonText("Re-sync")
					.setTooltip("Re-read CONFIG.md (after editing it) without reloading the plugin.")
					.onClick(async () => {
						await this.plugin.applyKitFacts();
						await save();
						this.display();
					})
			);
		}

		new Setting(containerEl)
			.setName("Theme palette")
			.setDesc(
				"Derive the type colours from the active theme's own palette so they always complement it. Primary types get the full hue, satellite types a softened variant, operational types stay gray. Re-derives on theme change. Off = the manual colour list below applies."
			)
			.addToggle((t) =>
				t.setValue(s.themePalette).onChange(async (v) => {
					s.themePalette = v;
					await save();
					this.display();
				})
			);

		new Setting(containerEl)
			.setName("Tint by type")
			.setDesc(
				"Wash the open note in its type colour: the title and h1 take the full colour, sub-headings and frontmatter are tinted, the background gets a faint tint. Explorer file names are tinted too."
			)
			.addToggle((t) =>
				t.setValue(s.typeTint).onChange(async (v) => {
					s.typeTint = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Calm reading")
			.setDesc(
				"In reading mode: collapse the properties block to a hover strip, hide edit-only tag buttons, and ease the reading measure. Theme colours (headings, emphasis, code) are kept — they aid skimming."
			)
			.addToggle((t) =>
				t.setValue(s.calmReading).onChange(async (v) => {
					s.calmReading = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Rounded corners")
			.setDesc(
				"Round the kit's corners — pills, badges, washes, buttons — to match iOS and native menus. Off keeps the kit's sharp default."
			)
			.addToggle((t) =>
				t.setValue(s.roundedCorners).onChange(async (v) => {
					s.roundedCorners = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Minimalist mode")
			.setDesc(
				"Hide the app chrome — tab bar, ribbons, nav buttons, version stamps, explainer text — leaving the notes. The settings gear stays, so this screen is always reachable to exit."
			)
			.addToggle((t) =>
				t.setValue(s.minimalistMode).onChange(async (v) => {
					s.minimalistMode = v;
					await save();
					if (v) new Notice("Minimalist mode on — Settings → Note-Kit UI to exit");
				})
			);

		// ── For You view ──────────────────────────────────────────────────────
		new Setting(containerEl).setName("For You").setHeading();

		new Setting(containerEl)
			.setName("Open on startup")
			.setDesc("Open the For You front page automatically when this vault loads.")
			.addToggle((t) =>
				t.setValue(s.nowOpenOnStartup).onChange(async (v) => {
					s.nowOpenOnStartup = v;
					await save();
				})
			);
		new Setting(containerEl)
			.setName("New tab opens For You")
			.setDesc("Turn a new empty tab (Ctrl/Cmd-T or +) into the For You page — a Home button.")
			.addToggle((t) =>
				t.setValue(s.nowReplaceNewTab).onChange(async (v) => {
					s.nowReplaceNewTab = v;
					await save();
				})
			);
		new Setting(containerEl)
			.setName("Show in sidebar")
			.setDesc("Keep a leaf in the right sidebar (on mobile, the swipe-left drawer) hosting the content chosen below.")
			.addToggle((t) =>
				t.setValue(s.sidebarNow).onChange(async (v) => {
					s.sidebarNow = v;
					await save();
				})
			);
		new Setting(containerEl)
			.setName("Sidebar content")
			.setDesc(
				"What the right sidebar hosts: the For You page, the user queue (decisions for you), or the machine queue (your checklist for the agents). On mobile a queue here sits behind a hold-to-unlock, so a stray swipe never lands a tap on it."
			)
			.addDropdown((d) =>
				d
					.addOption("for-you", "For You")
					.addOption("user-queue", "User queue")
					.addOption("machine-queue", "Machine queue")
					.setValue(s.sidebarContent)
					.onChange(async (v) => {
						s.sidebarContent = v as SidebarContent;
						await save();
					})
			);
		new Setting(containerEl)
			.setName("Clean queue view")
			.setDesc("Open queue files in the clean queue view.")
			.addToggle((t) =>
				t.setValue(s.queueCleanView).onChange(async (v) => {
					s.queueCleanView = v;
					await save();
				})
			);
		new Setting(containerEl)
			.setName("Hold duration")
			.setDesc("How long a press-and-hold (approve, undo) takes to commit, in milliseconds.")
			.addSlider((sl) =>
				sl
					.setLimits(150, 800, 10)
					.setValue(s.holdMs)
					.setDynamicTooltip()
					.onChange(async (v) => {
						s.holdMs = v;
						await save();
					})
			);
		const forYouAdv = advancedGroup(containerEl, "Advanced — For You sources");
		new Setting(forYouAdv)
			.setName("Active types")
			.setDesc("Comma-separated note types treated as active work in the Active section.")
			.addText((t) =>
				t.setValue(s.nowActiveTypes.join(", ")).onChange(async (v) => {
					s.nowActiveTypes = v
						.split(",")
						.map((x) => x.trim())
						.filter(Boolean);
					await save();
				})
			);
		kitFact(forYouAdv, "Queue folders", s.nowQueueFolders.join(", "), () => {
			new Setting(forYouAdv)
				.setName("Queue folders")
				.setDesc("One folder path per line. Items here join the Needs-you section.")
				.addTextArea((ta) => {
					ta.setValue(s.nowQueueFolders.join("\n")).onChange(async (v) => {
						s.nowQueueFolders = v
							.split("\n")
							.map((l) => l.trim())
							.filter(Boolean);
						await save();
					});
					ta.inputEl.rows = 2;
					ta.inputEl.style.width = "100%";
				});
		});
		kitFact(forYouAdv, "User queue", s.userQueuePath, () => {
			new Setting(forYouAdv)
				.setName("User queue")
				.setDesc("Path to the AI→you checklist; its open items appear in the Decide bucket.")
				.addText((t) =>
					t.setValue(s.userQueuePath).onChange(async (v) => {
						s.userQueuePath = v.trim();
						await save();
					})
				);
		});
		kitFact(forYouAdv, "Machine queue", s.machineQueuePath, () => {
			new Setting(forYouAdv)
				.setName("Machine queue")
				.setDesc("Path to the you→AI checklist; open items appear in the Queue bucket, which can add to it.")
				.addText((t) =>
					t.setValue(s.machineQueuePath).onChange(async (v) => {
						s.machineQueuePath = v.trim();
						await save();
					})
				);
		});
		new Setting(forYouAdv)
			.setName("Animations")
			.setDesc(
				"Animate the For You view — section grow/shrink on fold, the press-and-hold fills, and a pulse when a queue item is checked or submitted."
			)
			.addToggle((t) =>
				t.setValue(s.animations).onChange(async (v) => {
					s.animations = v;
					await save();
				})
			);

		// ── Explorer & notes ──────────────────────────────────────────────────
		new Setting(containerEl).setName("Explorer & notes").setHeading();

		new Setting(containerEl)
			.setName("Per-type styling")
			.setDesc("Colour explorer rows by note type and accent the open note.")
			.addToggle((t) =>
				t.setValue(s.enableTypeStyling).onChange(async (v) => {
					s.enableTypeStyling = v;
					await save();
				})
			);

		// Type colours: compact swatch grid + guarded reset to the shipped palette.
		const paletteOn = s.themePalette && this.plugin.palette !== null;
		let resetArmed = false;
		new Setting(containerEl)
			.setName("Type colours")
			.setDesc(
				paletteOn
					? "Derived from the theme (read-only while Theme palette is on). Reset restores the kit's shipped manual palette."
					: "Click a swatch to change a type's colour. Only listed types get a dot/accent."
			)
			.addButton((b) =>
				b
					.setButtonText("Reset to kit defaults")
					.setTooltip("Overwrite the colour list with the palette shipped with the kit.")
					.onClick(async () => {
						if (!resetArmed) {
							resetArmed = true;
							b.setWarning().setButtonText("Click again to confirm");
							window.setTimeout(() => {
								resetArmed = false;
								b.setButtonText("Reset to kit defaults");
								b.buttonEl.removeClass("mod-warning");
							}, 4000);
							return;
						}
						s.typeStyles = DEFAULT_SETTINGS.typeStyles.map((r) => ({ ...r }));
						await save();
						this.display();
					})
			);

		const typeGrid = containerEl.createDiv({ cls: "nkui-type-grid" });
		const shownTypes = paletteOn ? this.plugin.typeStyles() : s.typeStyles;
		shownTypes.forEach((rule, i) => {
			const row = typeGrid.createDiv({ cls: "nkui-type-grid-cell" });
			if (paletteOn) {
				const swatch = row.createSpan({ cls: "nkui-type-grid-swatch" });
				swatch.style.backgroundColor = rule.color;
			} else {
				const input = row.createEl("input", { type: "color", cls: "nkui-type-grid-swatch" });
				input.value = toHexColor(rule.color);
				input.setAttr("aria-label", `Colour for type "${rule.type}"`);
				input.addEventListener("change", async () => {
					s.typeStyles[i].color = input.value;
					await save();
				});
			}
			row.createSpan({ text: rule.type, cls: "nkui-type-grid-name" });
			if (!paletteOn) {
				const remove = row.createSpan({ text: "×", cls: "nkui-type-grid-remove" });
				remove.style.cursor = "pointer";
				remove.style.opacity = "0.5";
				remove.style.marginLeft = "auto";
				remove.setAttr("role", "button");
				remove.setAttr("aria-label", `Remove type "${rule.type}"`);
				remove.addEventListener("click", async () => {
					s.typeStyles.splice(i, 1);
					await save();
					this.display();
				});
			}
		});
		if (!paletteOn) {
			let addTypeInput: TextComponent | undefined;
			new Setting(containerEl)
				.setName("Add type")
				.setClass("nkui-compact-row")
				.addText((t) => {
					t.setPlaceholder("type name");
					addTypeInput = t;
				})
				.addButton((b) =>
					b.setButtonText("Add").onClick(async () => {
						const name = addTypeInput?.getValue().trim();
						if (!name || s.typeStyles.some((r) => r.type === name)) return;
						s.typeStyles.push({ type: name, color: "#888888" });
						await save();
						this.display();
					})
				);
		}

		new Setting(containerEl)
			.setName("Unreviewed draft flags")
			.setDesc("Mark drafts (reviewed: false) and show a live count on the inbox folder.")
			.addToggle((t) =>
				t.setValue(s.enableReviewFlags).onChange(async (v) => {
					s.enableReviewFlags = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Float types to top")
			.setDesc(
				"Order folder contents semantically — comma-separated note types whose files sort first in their folder (cover index first, then floated types, then queues). With CONFIG sync on, display order follows the kit's .claude/CONFIG.md table rows — § Folders for the root folders, § Subfolders for subfolders, § Types for floated files — so reordering the table rows reorders the explorer. Empty disables all ordering."
			)
			.addText((t) =>
				t.setValue(s.floatTopTypes.join(", ")).onChange(async (v) => {
					s.floatTopTypes = v
						.split(",")
						.map((x) => x.trim())
						.filter(Boolean);
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Sink folders")
			.setDesc("Folders that render small and dim with all their contents — cold storage.")
			.addText((t) =>
				t.setValue(s.sinkFolders.join(", ")).onChange(async (v) => {
					s.sinkFolders = v
						.split(",")
						.map((x) => x.trim())
						.filter(Boolean);
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Sort by weight")
			.setDesc("Files carrying a `weight` frontmatter value sort and tint heaviest-first among their siblings.")
			.addToggle((t) =>
				t.setValue(s.sortByWeight).onChange(async (v) => {
					s.sortByWeight = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Large inbox/outbox rows")
			.setDesc(
				"Render the inbox and outbox — the working mouths where draft and queue types flow — as larger explorer rows, so the folders you act on lead the eye."
			)
			.addToggle((t) =>
				t.setValue(s.largeMouths).onChange(async (v) => {
					s.largeMouths = v;
					await save();
				})
			);

		const explorerAdv = advancedGroup(containerEl, "Advanced — explorer & note options");
		new Setting(explorerAdv)
			.setName("Accent the open note")
			.setDesc("Add a coloured edge to the active note matching its type.")
			.addToggle((t) =>
				t.setValue(s.applyTypeBodyClass).onChange(async (v) => {
					s.applyTypeBodyClass = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("Match the graph view")
			.setDesc(
				"Write the type colours into the graph view's colour groups, so graph and bubble nodes carry the same colour as the explorer dots. Updates whenever the colours change; your other graph options are untouched."
			)
			.addToggle((t) =>
				t.setValue(s.syncGraphColors).onChange(async (v) => {
					s.syncGraphColors = v;
					await save();
				})
			)
			.addButton((b) =>
				b.setButtonText("Apply now").onClick(async () => {
					await this.plugin.applyGraphColors();
				})
			);
		new Setting(explorerAdv)
			.setName("Hide folder arrows")
			.setDesc("Remove the collapse chevrons from explorer folders. Folders still toggle when you click their name.")
			.addToggle((t) =>
				t.setValue(s.hideFolderArrows).onChange(async (v) => {
					s.hideFolderArrows = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("Fold children with parent")
			.setDesc("Collapsing a folder also collapses its subfolders, so re-opening it shows them folded.")
			.addToggle((t) =>
				t.setValue(s.foldChildrenWithParent).onChange(async (v) => {
					s.foldChildrenWithParent = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("Minimal chrome")
			.setDesc("Quieten Obsidian's tab bar and the file-explorer toolbar icons to match the minimalist styling.")
			.addToggle((t) =>
				t.setValue(s.minimalChrome).onChange(async (v) => {
					s.minimalChrome = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("Solid icons")
			.setDesc("Replace the outline control icons (folder, search, sort, close …) with minimal solid shapes. A glyph the replacement doesn't cover keeps its original icon.")
			.addToggle((t) =>
				t.setValue(s.solidIcons).onChange(async (v) => {
					s.solidIcons = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("One tab per document")
			.setDesc(
				"Opening a file that is already open focuses its existing tab instead of creating a duplicate. Also keeps a single For You tab — a new tab focuses the open one rather than spawning a second."
			)
			.addToggle((t) =>
				t.setValue(s.dedupeTabs).onChange(async (v) => {
					s.dedupeTabs = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("Per-row draft dot")
			.setDesc("Show a dot on each unreviewed note in the explorer.")
			.addToggle((t) =>
				t.setValue(s.showRowBadge).onChange(async (v) => {
					s.showRowBadge = v;
					await save();
				})
			);
		new Setting(explorerAdv)
			.setName("Inbox count badge")
			.setDesc("Show a live 'N unreviewed' pill on the inbox folder row.")
			.addToggle((t) =>
				t.setValue(s.showInboxCount).onChange(async (v) => {
					s.showInboxCount = v;
					await save();
				})
			);
		kitFact(explorerAdv, "Type field", s.typeField, () => {
			new Setting(explorerAdv)
				.setName("Type field")
				.setDesc("Frontmatter key that holds the note type.")
				.addText((t) =>
					t.setValue(s.typeField).onChange(async (v) => {
						s.typeField = v.trim() || "type";
						await save();
					})
				);
		});
		kitFact(explorerAdv, "Reviewed field", s.reviewedField, () => {
			new Setting(explorerAdv)
				.setName("Reviewed field")
				.setDesc("Frontmatter key whose false value marks an unreviewed draft.")
				.addText((t) =>
					t.setValue(s.reviewedField).onChange(async (v) => {
						s.reviewedField = v.trim() || "reviewed";
						await save();
					})
				);
		});
		kitFact(explorerAdv, "Inbox folders", s.inboxFolders.join(", "), () => {
			new Setting(explorerAdv)
				.setName("Inbox folders")
				.setDesc("One folder path per line. These get the count badge.")
				.addTextArea((ta) => {
					ta.setValue(s.inboxFolders.join("\n")).onChange(async (v) => {
						s.inboxFolders = v
							.split("\n")
							.map((l) => l.trim())
							.filter(Boolean);
						await save();
					});
					ta.inputEl.rows = 3;
					ta.inputEl.style.width = "100%";
				});
		});

	}
}
