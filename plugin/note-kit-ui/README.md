# Note-Kit UI

An optional Obsidian plugin that makes the note-kit's structure visible in the app — desktop and mobile. It folds together what would otherwise be a stack of single-purpose community plugins — type-aware link styling, a draft indicator, a home dashboard, a reading declutter — into one package that speaks the kit's exact vocabulary.

## Features

| | Feature | How |
| - | ------- | --- |
| **a** | Per-type styling | A coloured dot on each explorer row by `type`, and a coloured edge — optionally a full tint of title, headings, and frontmatter — on the open note. Applied at render time — **no `cssclasses` written to any file**, so it needs no kit normalizer script. |
| **b** | Theme palette | Derives the per-type colours from the active theme's own palette and re-derives on every theme change; off, a curated manual colour list applies. |
| **c** | Graph colour match | Mirrors the type colours into the graph view's colour groups, so graph and bubble nodes carry the same colour as the explorer dots. |
| **d** | Unreviewed draft flags | A dot on each `reviewed: false` note, plus a live "N unreviewed" pill on the inbox folder row — the one thing no off-the-shelf plugin does. |
| **e** | List ordering | Floats chosen types to the top of their folder, sinks cold-storage folders small and dim, and sorts siblings heaviest-first by a `weight` field. |
| **f** | For You front page | A dedicated view (the **sun** ribbon icon) that surfaces the kit's queues, inbox drafts, and active work without the file tree — see below. |
| **g** | Clean queue views | Opens either kit queue file as a clean checklist instead of raw markdown — tick a decision, type in a task — with one tap back to the raw editor. |
| **h** | CONFIG editor | A schema-driven editable grid of `.claude/CONFIG.md`'s tables. Opens read-only; a hold-to-unlock arms editing, and every save archives the prior CONFIG, refuses any change that would break the table shape, and prompts you to re-run sync_config. Opt-in — see below. |
| **i** | Calm reading | In reading mode, collapses the properties block to a hover strip, hides edit-only tag buttons, and eases the reading measure; theme colours are kept as skim aids. |
| **j** | Skim mode | A reading-view declutter: condense every non-heading block to a dim single-line outline (click a heading to restore its section), minimize completed `- [x]` items, or fold sections by keyword or to first-and-last only. Condense carries three intensity tiers — readable, faint, and outline. |
| **k** | Tag → graph | Clicking a tag — a frontmatter pill or an inline `#tag` — opens the graph view filtered to that tag (`tag:#…`) instead of the default tag search. |
| **l** | Replaceable icons | Swaps Obsidian's outline control icons for minimal solid shapes, and takes a pasted SVG for any single control. |
| **m** | Quiet chrome | Minimalist mode strips the app chrome down to the notes; lighter touches quieten the tab bar and explorer toolbar, hide folder arrows, fold children with their parent, dedupe tabs to one per document, enlarge the inbox/outbox rows, and round the kit's corners. |
| **n** | Motion & holds | Subtle motion on the For-You and queue views — sections grow and shrink on fold, a press-and-hold fill on every approve / undo / unlock, a pulse when a queue item is checked. A speed multiplier tunes it, a hold-duration setting tunes the commit, and a single toggle turns all motion off. |
| **o** | Dockable views | The For-You page and either queue open as a main-area tab or dock in a side panel (the *Open …* commands). The same surface renders identically wherever it sits — tab or sidebar — and once placed the plugin leaves the arrangement alone. |

Every feature is individually toggleable in **Settings → Note-Kit UI**. **Vault structure is read from the kit itself:** at load the plugin parses `.claude/CONFIG.md` (one-way, read-only) and derives the inbox/outbox folders, both queue paths, the frontmatter field names, and the type vocabulary — those rows show as read-only in settings ("derived from CONFIG"), so the plugin cannot drift from the kit. Presentation (colors, weights, sizes, toggles) stays the plugin's own. In a vault with no kit, the same fields fall back to editable manual settings.

Explorer decoration is applied inside the file tree's `MutationObserver` callback — a microtask that runs before the browser paints — so rows in a freshly-expanded folder appear already-styled rather than flashing their raw form and snapping into place.

## "For You" front page

A dedicated view (the **sun** ribbon icon; opens in a new tab via the Home action) that surfaces what needs attention without the file tree:

- the two kit queues as live checklists — **Decide** (items the assistant raised for you to tick off) and **Queue** (tasks you type in for the assistant);
- inbox **drafts** (`reviewed: false`) grouped by `type`;
- **Active** projects and areas, each with a relative-age column and a `draft` marker where one applies.

It reads `metadataCache` and file stats for the lists and writes only the two queue files (ticking a checkbox or appending an item). A checkbox tick or an approve commits on a short press-and-hold rather than a single click, so a stray tap is not a decision. Groups are foldable and the collapsed/expanded state persists.

It can open automatically when the vault loads and stand in for a new empty tab (a Home button), and it can sit in a side panel as readily as a main tab. Its vertical placement is adjustable — the content sits a little above true centre by default.

## CONFIG editor

An opt-in view (the **sliders** ribbon icon and the *Open CONFIG editor* command, both added only when the feature is enabled in settings) that renders every table in the kit's `.claude/CONFIG.md` as a clean editable grid — one grid per `##` section. The shape is parsed, never assumed: a section that gains a column, or one the editor can't parse, still renders rather than being dropped.

Because this writes the canonical CONFIG (outside the inbox), every save is guarded. The grid opens read-only behind a hold-to-unlock; a save archives the prior CONFIG before overwriting, refuses any edit that would change a table's shape, and — since the plugin can't run it headlessly — prompts you to re-run sync_config to propagate the change.

## Mobile

`isDesktopOnly: false`. The plugin uses no Node/Electron APIs — only the documented Obsidian DOM/metadata APIs — so it runs on iOS and Android. `CSS.escape` has a fallback for older mobile webviews.

## Build

```
npm install
npm run build      # tsc type-check + esbuild production bundle → main.js
npm run dev        # esbuild watch (inline sourcemap) for development
```

Source lives in `src/`. The three files Obsidian actually loads are `main.js`, `manifest.json`, and `styles.css`.

## Packaging into the kit install

Ship only the runtime artifacts — drop these into `<vault>/.obsidian/plugins/note-kit-ui/`:

- `main.js`
- `manifest.json`
- `styles.css`

Then enable it: add `"note-kit-ui"` to `.obsidian/community-plugins.json` (the installer should merge, not overwrite). `src/`, `node_modules/`, and the build config are dev-only and need not ship.
