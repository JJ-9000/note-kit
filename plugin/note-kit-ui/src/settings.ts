import { App, Notice, PluginSettingTab, Setting, TextComponent } from "obsidian";
import type NoteKitUiPlugin from "./main";
import { HOLD_MS } from "./holds";
import { ICON_CONTROLS, encodeSvgDataUri, normalizeOverrideSvg } from "./icons";

/** One note-type rule (feature c). `type` matches the frontmatter `type` value. */
export interface TypeStyle {
	type: string;
	color: string;
}

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
	 * (the decorator marks them; the static stylesheet carries the look).
	 * Matches any folder whose name CONTAINS any entry (case-insensitive). */
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
	/** Allow the plugin's views in the side docks. OFF detaches them; ON means
	 * hands off — the panes behave like normal Obsidian panes and the user's
	 * arrangement stands (see main.applySidebarNow). */
	sidebarNow: boolean;
	/** One-time default layout has been installed (see main.seedLayout) —
	 * after it the plugin never forces a pane again. */
	layoutSeeded: boolean;
	/** For You vertical placement: a percent-ish shift of the centred position
	 * (-50..+50 of the screen height; negative = higher). The view adds
	 * (value / 100) × window.innerHeight to its --nkui-screen-shift target. */
	nowVerticalBias: number;
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
	/** Explorer: draw open folders as nested, filled, type-tinted CONTAINER boxes
	 * — each sub-folder a concentric box inside its parent (body class
	 * nkui-nested-boxes; an iOS grouped-cards look). OFF by default: the explorer
	 * keeps its flat highlight bands (the start-of-session look). */
	nestedContainers: boolean;
	/** Use the bundled Inter face (the public San-Francisco alternative) as the
	 * app-wide interface + text font, keeping the monospace for code (body class
	 * nkui-font). Off = the app's normal font; a theme can still override. */
	interfaceFont: boolean;
	/** Hide the app chrome — tab bar, ribbons, nav buttons, version stamps,
	 * explainer text — leaving the notes (body class nkui-minimal). The settings
	 * path stays visible, so the mode is always exitable. */
	minimalistMode: boolean;
	/** Animation speed multiplier. The live CSS token --nkui-speed-user divides
	 * animation durations by this value (shipped default 1.1; 1.0 = real-time;
	 * higher = faster). Range 0.5–2.0. Persisted and re-applied on load. */
	animSpeed: number;

	/** Inter-element margin multiplier. The live CSS token --nkui-margin-scale
	 * multiplies every margin BETWEEN rows / boxes / sections (NOT the content
	 * padding/inset inside them); default 1.0 (no change), lower = denser. Persisted
	 * and re-applied on load (set at :root, where the margin tokens compute). */
	marginScale: number;

	/** Skim mode — a reading-view declutter (reading view only; live preview is a
	 * CM6 surface a post-processor never sees). One of:
	 *  · "off"               — no skim treatment.
	 *  · "condense"          — the primary skim: every non-heading block (paragraphs,
	 *                          blockquotes, lists, tables, completed `- [x]` items)
	 *                          shrinks to a dim single-line outline row; headings stay
	 *                          legible as scan anchors. Click a heading to restore its
	 *                          section to full size; the rest stays condensed.
	 *  · "minimize-completed"— `- [x]` checked items render dim + reduced.
	 *  · "fold-keywords"     — sections whose header matches skimFoldKeywords start folded.
	 *  · "first-last"        — fold every section except the first and last header. */
	skimMode: "off" | "condense" | "minimize-completed" | "fold-keywords" | "first-last";
	/** The last non-off skim mode chosen, so the "Toggle skim" command can restore
	 * it when turning skim back on (toggle off <-> last active mode). */
	lastSkimMode: "condense" | "minimize-completed" | "fold-keywords" | "first-last";
	/** Comma-separated header keywords that start folded under skimMode
	 * "fold-keywords" (case-insensitive substring match on the header text). */
	skimFoldKeywords: string;
	/** Condense intensity — three tiers layered on the base condense look (only in
	 * effect when skimMode is "condense"; drives body.nkui-skim-tier-<tier>):
	 *  · "readable" — condensed content lifted to read clearly + heading margins
	 *                 tightened to a dense stack; headings keep full size.
	 *  · "faint"    — condensed content at the dimmest recede + sparse default
	 *                 heading margins (pure scroll-past; today's original look).
	 *  · "outline"  — readable's lift + tight margins PLUS shrunk heading fonts,
	 *                 so the whole note reads as one uniform compact outline. */
	condenseTier: "readable" | "faint" | "outline";

	/** User-replaceable control icons: control-key → raw SVG (a full `<svg>…</svg>`
	 * or a bare path-d). Feeds the nkui-solid-icons mask machinery via css.ts; an
	 * empty/blank entry falls through to the shipped glyph. See icons.ts for the
	 * control set and the encode/normalize helpers. */
	iconOverrides: Record<string, string>;

	/** Clicking a tag (frontmatter pill or inline `a.tag`) opens the graph view
	 * filtered to that tag (tag:#<tag>) instead of the default tag search. */
	tagClickOpensGraph: boolean;

	/** Enable the schema-driven CONFIG-editor view (a command + ribbon to open an
	 * editable grid of CONFIG.md's tables). Off hides the command and ribbon. The
	 * view itself opens read-only and gates every save behind a hold-to-unlock,
	 * an archive-prior copy, and a shape check — but the editor is consequential
	 * (it writes the canonical CONFIG), so it stays opt-in. */
	configEditor: boolean;
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
		// log moved up from #3b3b3b — near-background grey was illegible at row
		// weight; loadSettings migrates a saved value still on the old default.
		{ type: "log", color: "#9a9a9a" },
		{ type: "revision", color: "#709eff" },
	],
	applyTypeBodyClass: true,
	typeTint: true,
	// The curated palette ships as the default look; theme-derivation is the
	// opt-in (the author's settled preference, promoted 2026-06-12).
	themePalette: false,
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
	layoutSeeded: false,
	// The author's settled placement — content sits noticeably above true centre.
	nowVerticalBias: -30,
	paletteMigrated: false,
	// The single hold source (holds.ts HOLD_MS = 610ms, the author's tuned commit).
	holdMs: HOLD_MS,
	solidIcons: true,
	largeMouths: true,
	// Sharp stays the kit default — rounded is the opt-in iOS look (the author
	// tried it on, then settled back to sharp; current-settings sync 2026-06-12).
	roundedCorners: false,
	// Nested folder-container boxes are OFF by default — the explorer keeps its
	// flat-band highlights (the previous, hardened look); the boxes are an opt-in
	// look (added 2026-06-15, user-gated after the prototype).
	nestedContainers: false,
	// On by default — the iPhone-style interface font is part of the kit look.
	interfaceFont: true,
	minimalistMode: false,
	// 1.1 = the shipped default (a touch faster than 1.0 = real-time); the CSS token
	// --nkui-speed-user divides animation durations by this value.
	animSpeed: 1.1,
	// 1.0 = default inter-element margins; --nkui-margin-scale multiplies them (0 = touching).
	marginScale: 1,

	// Skim off by default — it's an opt-in reading treatment, not a baseline look.
	skimMode: "off",
	// The mode Toggle skim restores; seeded to condense (the primary skim).
	lastSkimMode: "condense",
	// A sensible starter set of "boilerplate" headers worth folding away.
	skimFoldKeywords: "appendix, references, notes, changelog, metadata",
	// Readable is the kit default — the lifted dim + dense heading stack reads as a
	// usable little list; faint (the original ultra-recede) and outline (uniform
	// shrunk-heading outline) are the opt-in alternatives.
	condenseTier: "readable",

	// No icon overrides ship — every control uses its solid-icon default until the
	// user pastes their own SVG in the Icons settings group.
	iconOverrides: {},

	// The feature's whole point — on by default; a tag click opens the graph.
	tagClickOpensGraph: true,

	// Opt-in: the CONFIG editor writes the canonical CONFIG, so it ships off and
	// the user enables it deliberately (every save is still gated by the unlock,
	// the archive-prior copy, and the shape check).
	configEditor: false,
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

		// ── Essentials ─────────────────────────────────────────────────────────
		// Most-reached controls first: hold, corners, minimalist, large mouths,
		// vertical alignment, animation speed.
		new Setting(containerEl).setName("Essentials").setHeading();

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
			.setName("Interface font (Inter)")
			.setDesc(
				"Use the bundled Inter typeface — the public San-Francisco alternative — as the interface and text font across the app, keeping your monospace for code. Off uses the app's normal font; a theme can still override."
			)
			.addToggle((t) =>
				t.setValue(s.interfaceFont).onChange(async (v) => {
					s.interfaceFont = v;
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

		new Setting(containerEl)
			.setName("Large inbox/outbox rows")
			.setDesc(
				"Render the inbox and outbox — the working mouths where draft and queue types flow — as larger explorer rows, so the folders you act on lead the eye."
			)
			.addToggle((t) =>
				t.setValue(s.largeMouths).onChange(async (v) => {
					s.largeMouths = v;
					document.body.classList.toggle("nkui-large-mouths", v);
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Nested folder containers")
			.setDesc(
				"Draw open folders as nested, filled, type-tinted boxes — each sub-folder a concentric box inside its parent (an iOS grouped-cards look). Off (default) keeps the flat highlight bands."
			)
			.addToggle((t) =>
				t.setValue(s.nestedContainers).onChange(async (v) => {
					s.nestedContainers = v;
					document.body.classList.toggle("nkui-nested-boxes", v);
					await save();
				})
			);

		new Setting(containerEl)
			.setName("Vertical alignment")
			.setDesc(
				"Shift the For You page's centred position, as a percent-ish share of the screen height — negative sits the content higher, positive lower. 0 is true centre. Also governs inbox and queue view vertical placement."
			)
			.addSlider((sl) =>
				sl
					.setLimits(-50, 50, 1)
					.setValue(s.nowVerticalBias)
					.setDynamicTooltip()
					.onChange(async (v) => {
						s.nowVerticalBias = v;
						await save();
					})
			);

		new Setting(containerEl)
			.setName("Animation speed")
			.setDesc(
				"Multiply the speed of all kit animations (fold, hold fills, check pulse). 1× is real-time; 2× is twice as fast; 0.5× is half speed."
			)
			.addSlider((sl) =>
				sl
					.setLimits(0.5, 2.0, 0.1)
					.setValue(s.animSpeed)
					.setDynamicTooltip()
					.onChange(async (v) => {
						s.animSpeed = v;
						document.documentElement.style.setProperty("--nkui-speed-user", String(v));
						await save();
					})
			);

		new Setting(containerEl)
			.setName("Margin scale")
			.setDesc(
				"Scale the space BETWEEN rows, boxes and sections — not the content inside them. 1× is the default; 0.5× halves the gaps for a denser tree; 0 makes them touch; higher spreads them out. (Drives --nkui-margin-scale.)"
			)
			.addSlider((sl) =>
				sl
					.setLimits(0, 1.5, 0.05)
					.setValue(s.marginScale ?? 1)
					.setDynamicTooltip()
					.onChange(async (v) => {
						s.marginScale = v;
						document.documentElement.style.setProperty("--nkui-margin-scale", String(v));
						await save();
					})
			);

		// ── Kit CONFIG sync ────────────────────────────────────────────────────
		new Setting(containerEl).setName("Kit sync").setHeading();

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
			.setName("CONFIG editor")
			.setDesc(
				"Add a command and ribbon icon that open an editable grid of the kit's .claude/CONFIG.md tables. The view opens read-only; a hold-to-unlock arms editing, and every save archives the prior CONFIG, refuses any change that would break the table shape, and asks you to run sync_config to propagate. The icon and command appear after the next reload."
			)
			.addToggle((t) =>
				t.setValue(s.configEditor).onChange(async (v) => {
					s.configEditor = v;
					await save();
				})
			);

		// ── Visual controls ────────────────────────────────────────────────────
		new Setting(containerEl).setName("Visual").setHeading();

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

		// ── Skim (reading view) ─────────────────────────────────────────────────
		// A reading-view declutter for long notes. Reading view only — live preview
		// is a CM6 editor surface a post-processor never reaches.
		new Setting(containerEl).setName("Skim").setHeading();

		new Setting(containerEl)
			.setName("Skim mode")
			.setDesc(
				"Declutter long notes in READING VIEW (live preview is unaffected — on mobile, switch the note to reading view, since notes open in live preview by default). Condense shrinks every non-heading block — paragraphs, lists, completed `- [x]` items — to a dim single-line outline you scroll right past, keeping the headings legible as anchors; click a heading to restore its section to full size. Minimize completed dims only `- [x]` checked items. Fold by keyword starts matching-header sections folded — click a header to expand. First/last folds every section except each note's first and last header."
			)
			.addDropdown((d) =>
				d
					.addOption("off", "Off")
					.addOption("condense", "Condense to outline (recommended)")
					.addOption("minimize-completed", "Minimize completed items")
					.addOption("fold-keywords", "Fold sections by keyword")
					.addOption("first-last", "First/last header only")
					.setValue(s.skimMode)
					.onChange(async (v) => {
						s.skimMode = v as NoteKitUiSettings["skimMode"];
						if (s.skimMode !== "off") s.lastSkimMode = s.skimMode;
						await save();
						// Re-render open reading views so the skim post-processor marks
						// headings (and draws chevrons) for the new mode — a body-class
						// toggle alone leaves an open view unprocessed.
						this.plugin.rerenderReadingViews();
						this.display(); // show/hide the keyword field
					})
			);

		// Condense intensity — three tiers layered on the base condense look. Shown
		// always (it only bites when Skim mode is Condense, noted in the description),
		// so the user can set their preferred tier without first flipping to condense.
		new Setting(containerEl)
			.setName("Condense intensity")
			.setDesc(
				"Only when Skim mode is Condense. Readable lifts the dim so the outline reads clearly and tightens heading spacing into a dense stack. Faint is the dimmest scroll-past recede with sparse heading spacing. Outline does what Readable does and also shrinks the headings, so the whole note reads as one uniform compact outline."
			)
			.addDropdown((d) =>
				d
					.addOption("readable", "Readable (recommended)")
					.addOption("faint", "Faint")
					.addOption("outline", "Outline")
					.setValue(s.condenseTier)
					.onChange(async (v) => {
						s.condenseTier = v as NoteKitUiSettings["condenseTier"];
						await save();
					})
			);

		if (s.skimMode === "fold-keywords") {
			new Setting(containerEl)
				.setName("Fold keywords")
				.setDesc(
					"Comma-separated words; a reading-view header whose text contains any of them starts folded. Case-insensitive substring match."
				)
				.addText((t) =>
					t.setValue(s.skimFoldKeywords).onChange(async (v) => {
						s.skimFoldKeywords = v;
						await save();
					})
				);
		}

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
					// Keep the text input from overflowing the pane
					t.inputEl.style.flex = "1 1 0";
					t.inputEl.style.minWidth = "0";
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

		// ── List ordering ─────────────────────────────────────────────────────
		new Setting(containerEl).setName("List ordering").setHeading();

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
			.setDesc(
				"Folders that render small and dim with all their contents — cold storage. Matches any folder whose name contains any of these words (comma-separated, case-insensitive)."
			)
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
			.setName("Allow sidebar views")
			.setDesc(
				"Allow the plugin's views in the side docks. The panes behave like normal Obsidian panes: place For You or a queue wherever you like (the Open … commands) and your arrangement stands. Off removes the plugin's views from the sidebars."
			)
			.addToggle((t) =>
				t.setValue(s.sidebarNow).onChange(async (v) => {
					s.sidebarNow = v;
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

		// ── Advanced — explorer & note options ────────────────────────────────
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
			.setName("Tag click opens graph")
			.setDesc(
				"Clicking a tag — a frontmatter pill or an inline #tag — opens the graph view filtered to that tag (tag:#…), so its notes light up. Off keeps Obsidian's default tag-search behaviour."
			)
			.addToggle((t) =>
				t.setValue(s.tagClickOpensGraph).onChange(async (v) => {
					s.tagClickOpensGraph = v;
					await save();
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

		// ── Icons ────────────────────────────────────────────────────────────
		// User-replaceable control glyphs, feeding the solid-icon mask machinery.
		// One row per known control: a live preview of the current glyph, an SVG
		// paste field, and a per-row reset. Only meaningful while Solid icons is on.
		const iconsAdv = advancedGroup(
			containerEl,
			"Advanced — replaceable icons" + (s.solidIcons ? "" : " (turn on Solid icons to use)")
		);
		iconsAdv.createDiv({
			cls: "nkui-icon-help setting-item-description",
			text: "Paste a full <svg>…</svg> or a bare path-d for any control. The shape masks in the icon's colour, so its own fills/strokes are ignored. Blank uses the shipped glyph.",
		});
		ICON_CONTROLS.forEach((ctrl) => {
			const raw = s.iconOverrides[ctrl.key] ?? "";
			// Preview: mask a swatch with either the override (if usable) or the
			// shipped default — exactly what css.ts will emit live.
			const previewSvg = normalizeOverrideSvg(raw) ?? ctrl.defaultSvg;
			const setting = new Setting(iconsAdv)
				.setClass("nkui-icon-row")
				.setName(ctrl.label)
				.setDesc(raw ? "Custom" : "Default");
			// Live glyph preview to the left of the controls.
			const preview = createSpan({ cls: "nkui-icon-preview" });
			preview.style.setProperty(
				"--nkui-icon-mask",
				`url("${encodeSvgDataUri(previewSvg)}")`
			);
			setting.nameEl.prepend(preview);
			setting.addText((t) => {
				t.setPlaceholder("<svg …>…</svg> or path-d");
				t.setValue(raw);
				t.inputEl.style.flex = "1 1 0";
				t.inputEl.style.minWidth = "0";
				t.onChange(async (v) => {
					const trimmed = v.trim();
					if (trimmed) s.iconOverrides[ctrl.key] = trimmed;
					else delete s.iconOverrides[ctrl.key];
					await save();
				});
			});
			setting.addExtraButton((b) =>
				b
					.setIcon("rotate-ccw")
					.setTooltip("Reset to the shipped glyph")
					.onClick(async () => {
						delete s.iconOverrides[ctrl.key];
						await save();
						this.display();
					})
			);
		});

	}
}
