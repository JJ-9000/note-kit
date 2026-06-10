# Note-Kit UI

An optional Obsidian plugin that makes the note-kit's structure visible in the app — desktop and mobile. It replaces what would otherwise be four third-party plugins (Supercharged Links, FileName Styler / Front Matter Title, Draft Indicator) with one package that speaks the kit's exact vocabulary.

## Features

| | Feature | How |
| - | ------- | --- |
| **a** | Folder-prefix styling | Weights/fades explorer rows by their `00-`/`01-`/`02-`/`99-` prefix. |
| **b** | Hide numeric prefix | Strips the structural prefix from displayed names (`00-Inbox` → `Inbox`). The on-disk filename keeps its prefix, so wikilinks still resolve. Date prefixes (`2026-…`) are deliberately never touched. |
| **c** | Per-type styling | A coloured dot on each explorer row by `type`, and a coloured edge on the open note. Applied at render time — **no `cssclasses` written to any file**, so it needs no kit normalizer script. |
| **d** | Unreviewed flags | A dot on each `reviewed: false` note, plus a live "N unreviewed" pill on the inbox folder row — the one thing no off-the-shelf plugin does. |

All four are individually toggleable. **Vault structure is read from the kit itself:** at load the plugin parses `.claude/CONFIG.md` (one-way, read-only) and derives the inbox/outbox folders, both queue paths, the frontmatter field names, the prefix tokens, and the type vocabulary — those rows show as read-only in **Settings → Note-Kit UI** ("derived from CONFIG"), so the plugin cannot drift from the kit. Presentation (colors, weights, sizes, toggles) stays the plugin's own. In a vault with no kit, the same fields fall back to editable manual settings.

Explorer decoration is applied inside the file tree's `MutationObserver` callback — a microtask that runs before the browser paints — so rows in a freshly-expanded folder appear already-styled rather than flashing their raw form and snapping into place.

## "For You" front page

A dedicated view (the **sun** ribbon icon; opens in a new tab via the Home action) that surfaces what needs attention without the file tree:

- the two kit queues as live checklists — **Decide** (items the assistant raised for you to tick off) and **Queue** (tasks you type in for the assistant);
- inbox **drafts** (`reviewed: false`) grouped by `type`;
- **Active** projects and areas, each with a relative-age column and a `draft` marker where one applies.

It reads `metadataCache` and file stats for the lists and writes only the two queue files (ticking a checkbox or appending an item). Groups are foldable and the collapsed/expanded state persists.

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
