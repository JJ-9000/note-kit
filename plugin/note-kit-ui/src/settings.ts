import { App, PluginSettingTab, Setting } from "obsidian";
import type NoteKitUiPlugin from "./main";

/** One numeric-prefix rule (feature a). `prefix` is the literal leading token, e.g. "00". */
export interface PrefixStyle {
	prefix: string;
	weight: number;
	size: number; // font-size multiplier in em; 1 = inherit explorer size
	opacity: number;
	color: string; // "" = inherit
}

/** One note-type rule (feature c). `type` matches the frontmatter `type` value. */
export interface TypeStyle {
	type: string;
	color: string;
}

export interface NoteKitUiSettings {
	/** Derive vault structure (inbox/outbox, queue paths, field names, prefix
	 * tokens, type vocabulary) from the kit's .claude/CONFIG.md at load, so the
	 * plugin cannot drift from the kit. Off (or no CONFIG present) = the manual
	 * fields below apply. */
	useKitConfig: boolean;

	// Feature toggles
	enablePrefixStyling: boolean; // a
	enableHidePrefix: boolean; // b
	enableTypeStyling: boolean; // c
	enableReviewFlags: boolean; // d
	hideFolderArrows: boolean; // explorer minimalism — hide collapse chevrons
	minimalChrome: boolean; // quieten Obsidian's tab bar + nav toolbar (main area only)
	calmReading: boolean; // reading-mode overrides to reduce note noise
	dedupeTabs: boolean; // one tab per document — focus the existing tab instead of opening twins

	// a / b — numeric prefix scheme (also drives which prefixes get hidden)
	prefixStyles: PrefixStyle[];

	// c — per-type styling
	typeField: string;
	typeStyles: TypeStyle[];
	applyTypeBodyClass: boolean; // accent the open note by type
	/** Derive type colours from the active theme's palette (--color-red … pink)
	 * at load and on every theme change, instead of the manual list. */
	themePalette: boolean;
	/** Mirror the type colours into the graph view's colour groups (graph.json)
	 * so graph/bubble nodes match the explorer dots. */
	syncGraphColors: boolean;

	// d — unreviewed drafts
	reviewedField: string;
	inboxFolders: string[];
	showRowBadge: boolean; // per-row draft dot
	showInboxCount: boolean; // live "N unreviewed" pill on the inbox folder row

	// "Now" view
	nowOpenOnStartup: boolean;
	nowReplaceNewTab: boolean; // a new empty tab opens For You instead of the blank screen
	nowRecentCount: number;
	nowActiveTypes: string[];
	nowQueueFolders: string[];
	nowCollapsedSections: string[]; // section titles currently folded
	nowExpandedGroups: string[]; // "Section/groupKey" ids the user has opened (groups fold by default)
	userQueuePath: string; // AI → user proposals to check off
	machineQueuePath: string; // user → AI checklist (addable from the view)
}

/** Defaults seeded from the kit's CONFIG vocabulary (prefixes, type enum, inbox path). */
export const DEFAULT_SETTINGS: NoteKitUiSettings = {
	useKitConfig: true,
	enablePrefixStyling: true,
	enableHidePrefix: true,
	enableTypeStyling: true,
	enableReviewFlags: true,
	hideFolderArrows: true,
	minimalChrome: true,
	calmReading: true,
	dedupeTabs: true,

	prefixStyles: [
		{ prefix: "00", weight: 800, size: 1.2, opacity: 1, color: "" },
		{ prefix: "01", weight: 600, size: 1, opacity: 1, color: "" },
		{ prefix: "02", weight: 500, size: 1, opacity: 1, color: "" },
		{ prefix: "03", weight: 500, size: 1, opacity: 1, color: "" },
		{ prefix: "04", weight: 500, size: 1, opacity: 1, color: "" },
		{ prefix: "99", weight: 400, size: 0.95, opacity: 0.55, color: "" },
	],

	typeField: "type",
	typeStyles: [
		{ type: "project", color: "#e5534b" },
		{ type: "area", color: "#d29922" },
		{ type: "reference", color: "#3fb950" },
		{ type: "research", color: "#58a6ff" },
		{ type: "plan", color: "#bc8cff" },
		{ type: "session", color: "#8b949e" },
		{ type: "journal", color: "#db61a2" },
		{ type: "idea", color: "#f0883e" },
		{ type: "snippet", color: "#39c5cf" },
		{ type: "source", color: "#a5a5a5" },
		{ type: "index", color: "#ffd33d" },
		{ type: "note", color: "#768390" },
		{ type: "voice", color: "#e275ad" },
		{ type: "design", color: "#9e86ff" },
		{ type: "format", color: "#4cc9c0" },
		{ type: "addendum", color: "#c69026" },
		{ type: "log", color: "#6e7681" },
		{ type: "revision", color: "#7a8dd8" },
	],
	applyTypeBodyClass: true,
	themePalette: true,
	syncGraphColors: true,

	reviewedField: "reviewed",
	inboxFolders: ["00-Inbox"],
	showRowBadge: true,
	showInboxCount: true,

	nowOpenOnStartup: true,
	nowReplaceNewTab: true,
	nowRecentCount: 15,
	nowActiveTypes: ["project", "area"],
	nowQueueFolders: ["00-Outbox"],
	nowCollapsedSections: ["Recent"],
	nowExpandedGroups: [],
	userQueuePath: "00-Inbox/00-User-Queue.md",
	machineQueuePath: "00-Outbox/00-Machine-Queue.md",
};

/** Sanitize a type value into a CSS class suffix. Shared by css + noteClass. */
export function typeClass(type: string): string {
	return "nkui-type-" + type.toLowerCase().replace(/[^a-z0-9_-]+/g, "-");
}

// ── textarea (de)serialization for the settings tab ──────────────────────────

export function typesToText(rows: TypeStyle[]): string {
	return rows.map((r) => `${r.type} | ${r.color}`).join("\n");
}

export function parseTypes(text: string): TypeStyle[] {
	const out: TypeStyle[] = [];
	for (const raw of text.split("\n")) {
		const line = raw.trim();
		if (!line) continue;
		const parts = line.split("|").map((p) => p.trim());
		if (!parts[0]) continue;
		out.push({ type: parts[0], color: parts[1] ?? "" });
	}
	return out;
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
		const kitFact = (name: string, value: string, manual: () => void) => {
			if (synced) {
				new Setting(containerEl)
					.setName(name)
					.setDesc(`${value} — derived from the kit's .claude/CONFIG.md; edit CONFIG to change it.`);
			} else {
				manual();
			}
		};

		// ── Kit CONFIG sync ───────────────────────────────────────
		new Setting(containerEl).setName("Kit CONFIG sync").setHeading();
		new Setting(containerEl)
			.setName("Follow the kit's CONFIG.md")
			.setDesc(
				synced
					? "On — vault structure (inbox, queues, field names, prefixes) is read from .claude/CONFIG.md at load, so the plugin can't drift from the kit. The derived values are shown read-only below."
					: s.useKitConfig
						? "On, but no readable .claude/CONFIG.md was found — the manual fields below apply."
						: "Off — the manual fields below apply."
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
			new Setting(containerEl)
				.setName("Re-sync now")
				.setDesc("Re-read CONFIG.md (after editing it) without reloading the plugin.")
				.addButton((b) =>
					b.setButtonText("Re-sync").onClick(async () => {
						await this.plugin.applyKitFacts();
						await save();
						this.display();
					})
				);
		}

		// ── Features ──────────────────────────────────────────────
		new Setting(containerEl).setName("Features").setHeading();

		new Setting(containerEl)
			.setName("Folder prefix styling")
			.setDesc("Weight/fade file-explorer rows by their numeric prefix (00-/01-/02-/99-).")
			.addToggle((t) =>
				t.setValue(s.enablePrefixStyling).onChange(async (v) => {
					s.enablePrefixStyling = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Hide numeric prefix")
			.setDesc(
				"Strip the structural prefix from displayed names (e.g. 00-Inbox → Inbox). The file on disk keeps its prefix, so wikilinks still resolve. Date prefixes (2026-…) are never touched."
			)
			.addToggle((t) =>
				t.setValue(s.enableHidePrefix).onChange(async (v) => {
					s.enableHidePrefix = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Per-type styling")
			.setDesc("Colour explorer rows by note type and accent the open note.")
			.addToggle((t) =>
				t.setValue(s.enableTypeStyling).onChange(async (v) => {
					s.enableTypeStyling = v;
					await save();
				})
			);

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
			.setName("Hide folder arrows")
			.setDesc("Remove the collapse chevrons from explorer folders. Folders still toggle when you click their name.")
			.addToggle((t) =>
				t.setValue(s.hideFolderArrows).onChange(async (v) => {
					s.hideFolderArrows = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Minimal chrome")
			.setDesc("Quieten Obsidian's tab bar and the file-explorer toolbar icons to match the minimalist styling.")
			.addToggle((t) =>
				t.setValue(s.minimalChrome).onChange(async (v) => {
					s.minimalChrome = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("One tab per document")
			.setDesc("Opening a file that is already open focuses its existing tab instead of creating a duplicate.")
			.addToggle((t) =>
				t.setValue(s.dedupeTabs).onChange(async (v) => {
					s.dedupeTabs = v;
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Calm reading")
			.setDesc("In reading mode: collapse the properties block to a hover strip, hide edit-only tag buttons, and ease the reading measure. Theme colours (headings, emphasis, code) are kept — they aid skimming.")
			.addToggle((t) =>
				t.setValue(s.calmReading).onChange(async (v) => {
					s.calmReading = v;
					await save();
				})
			);

		// ── Prefix scheme ─────────────────────────────────────────
		new Setting(containerEl).setName("Prefix scheme").setHeading();
		containerEl.createEl("p", {
			text: "Style file-explorer rows by their leading prefix. Each row's controls are, left to right: weight, text size, and opacity. Only the prefixes listed here are styled or hidden — date-named notes (2026-…) are never touched.",
			cls: "setting-item-description",
		});

		const WEIGHTS: Record<string, string> = {
			"300": "Light",
			"400": "Normal",
			"500": "Medium",
			"600": "Semibold",
			"700": "Bold",
			"800": "Extra-bold",
			"900": "Black",
		};

		s.prefixStyles.forEach((rule, i) => {
			const row = new Setting(containerEl);
			row.setName(`Prefix "${rule.prefix || "?"}"`);

			row.addText((t) => {
				t.setValue(rule.prefix)
					.setPlaceholder("00")
					.onChange(async (v) => {
						rule.prefix = v.trim();
						row.setName(`Prefix "${rule.prefix || "?"}"`);
						await save();
					});
				t.inputEl.size = 4;
				t.inputEl.setAttr("aria-label", "Prefix token");
			});

			row.addDropdown((d) => {
				d.addOptions(WEIGHTS)
					.setValue(String(rule.weight))
					.onChange(async (v) => {
						rule.weight = Number(v);
						await save();
					});
				d.selectEl.setAttr("aria-label", "Font weight");
			});

			row.addSlider((sl) => {
				sl.setLimits(0.8, 1.6, 0.05)
					.setValue(rule.size ?? 1)
					.setDynamicTooltip()
					.onChange(async (v) => {
						rule.size = v;
						await save();
					});
				sl.sliderEl.setAttr("aria-label", "Text size (×)");
			});

			row.addSlider((sl) => {
				sl.setLimits(0.3, 1, 0.05)
					.setValue(rule.opacity ?? 1)
					.setDynamicTooltip()
					.onChange(async (v) => {
						rule.opacity = v;
						await save();
					});
				sl.sliderEl.setAttr("aria-label", "Opacity");
			});

			row.addExtraButton((b) => {
				b.setIcon("trash-2")
					.setTooltip("Remove this prefix")
					.onClick(async () => {
						s.prefixStyles.splice(i, 1);
						await save();
						this.display();
					});
			});
		});

		new Setting(containerEl).addButton((b) => {
			b.setButtonText("Add prefix")
				.setCta()
				.onClick(async () => {
					s.prefixStyles.push({ prefix: "", weight: 400, size: 1, opacity: 1, color: "" });
					await save();
					this.display();
				});
		});

		// ── Types ─────────────────────────────────────────────────
		new Setting(containerEl).setName("Note types").setHeading();
		kitFact("Type field", s.typeField, () => {
			new Setting(containerEl)
				.setName("Type field")
				.setDesc("Frontmatter key that holds the note type.")
				.addText((t) =>
					t.setValue(s.typeField).onChange(async (v) => {
						s.typeField = v.trim() || "type";
						await save();
					})
				);
		});
		new Setting(containerEl)
			.setName("Accent the open note")
			.setDesc("Add a coloured edge to the active note matching its type.")
			.addToggle((t) =>
				t.setValue(s.applyTypeBodyClass).onChange(async (v) => {
					s.applyTypeBodyClass = v;
					await save();
				})
			);
		new Setting(containerEl)
			.setName("Theme palette")
			.setDesc(
				"Derive the type colours from the active theme's own palette (its red, orange, yellow, green, cyan, blue, purple and pink), so they always complement the theme. Primary types get the full hue, their satellite types a softened variant, operational types stay gray. Re-derives automatically when the theme changes. Off = the manual list below applies."
			)
			.addToggle((t) =>
				t.setValue(s.themePalette).onChange(async (v) => {
					s.themePalette = v;
					await save();
					this.display();
				})
			);
		new Setting(containerEl)
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
		const paletteOn = s.themePalette && this.plugin.palette !== null;
		new Setting(containerEl)
			.setName("Type colours")
			.setDesc(
				paletteOn
					? "Derived from the theme (read-only while Theme palette is on)."
					: "One per line: type | colour. Only listed types get a dot/accent."
			)
			.addTextArea((ta) => {
				ta.setValue(typesToText(paletteOn ? this.plugin.typeStyles() : s.typeStyles)).onChange(
					async (v) => {
						s.typeStyles = parseTypes(v);
						await save();
					}
				);
				ta.inputEl.rows = 8;
				ta.inputEl.style.width = "100%";
				ta.inputEl.style.fontFamily = "var(--font-monospace)";
				ta.inputEl.disabled = paletteOn;
			});

		// ── Review flags ──────────────────────────────────────────
		new Setting(containerEl).setName("Drafts & review").setHeading();
		kitFact("Reviewed field", s.reviewedField, () => {
			new Setting(containerEl)
				.setName("Reviewed field")
				.setDesc("Frontmatter key whose false value marks an unreviewed draft.")
				.addText((t) =>
					t.setValue(s.reviewedField).onChange(async (v) => {
						s.reviewedField = v.trim() || "reviewed";
						await save();
					})
				);
		});
		new Setting(containerEl)
			.setName("Per-row draft dot")
			.setDesc("Show a dot on each unreviewed note in the explorer.")
			.addToggle((t) =>
				t.setValue(s.showRowBadge).onChange(async (v) => {
					s.showRowBadge = v;
					await save();
				})
			);
		new Setting(containerEl)
			.setName("Inbox count badge")
			.setDesc("Show a live 'N unreviewed' pill on the inbox folder row.")
			.addToggle((t) =>
				t.setValue(s.showInboxCount).onChange(async (v) => {
					s.showInboxCount = v;
					await save();
				})
			);
		kitFact("Inbox folders", s.inboxFolders.join(", "), () => {
			new Setting(containerEl)
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

		// ── "Now" view ────────────────────────────────────────────
		new Setting(containerEl).setName("“Now” view").setHeading();
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
			.setName("Recent items")
			.setDesc("How many recently-changed notes the Recent section shows.")
			.addSlider((sl) =>
				sl
					.setLimits(5, 40, 1)
					.setValue(s.nowRecentCount)
					.setDynamicTooltip()
					.onChange(async (v) => {
						s.nowRecentCount = v;
						await save();
					})
			);
		new Setting(containerEl)
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
		kitFact("Queue folders", s.nowQueueFolders.join(", "), () => {
			new Setting(containerEl)
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
		kitFact("User queue", s.userQueuePath, () => {
			new Setting(containerEl)
				.setName("User queue")
				.setDesc("Path to the AI→you checklist; its open items appear in the Decide bucket.")
				.addText((t) =>
					t.setValue(s.userQueuePath).onChange(async (v) => {
						s.userQueuePath = v.trim();
						await save();
					})
				);
		});
		kitFact("Machine queue", s.machineQueuePath, () => {
			new Setting(containerEl)
				.setName("Machine queue")
				.setDesc("Path to the you→AI checklist; open items appear in the Queue bucket, which can add to it.")
				.addText((t) =>
					t.setValue(s.machineQueuePath).onChange(async (v) => {
						s.machineQueuePath = v.trim();
						await save();
					})
				);
		});
	}
}
