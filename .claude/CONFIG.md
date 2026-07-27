# Note-Kit CONFIG

The kit's only user-editable definitions, and the one source of truth its scripts read: every literal path, prefix, type, tag, and action resolves here; everywhere else it appears as a wildcard token. The tables are human-editable and machine-legible — `scripts/config_variables` reads them and `scripts/sync_config` mirrors scope-limited copies into `CLAUDE.md` and `AGENTS.md`.

**One home per rule.** This file holds every cross-agent protocol and all vocabulary; an agent's SKILL holds only that agent's own procedure; `RULES.md` stays the small always-on behavioral layer. Every other mention of a rule is a citation by section name ("per CONFIG § Asset folders"), never a restatement of its content — a rule stated twice is a rule that drifts. Before any judgment call, an agent grounds itself in the relevant section here.

## Editing convention

- A parser finds a table by its H2 heading and column header, never by line number; columns are fixed, and changing one updates `scripts/config_variables` and every consuming SKILL in the same change.
- **A table is the transposition signal.** Everything user-configurable in this file sits in a table; a table — or a `note-kit:sync` marker block — is the sign that the content is machine-parsed or stamped verbatim into the related skills and agents by `sync_config`. Prose outside tables and markers is read in place, never transposed.
- A token sits left, its literal right: a top-level folder is its `<wildcard>` everywhere but `## Folders`, a subfolder is named by the type-role `## Subfolders` resolves, and a machine-specific absolute path is a token too (`<user-home>/…`) — so no literal path or bare numeric prefix appears outside this file.
- After any edit, run `python scripts/sync_config.py`.

## Folders

The **six top-level roots** the kit creates and routes to. Everything else is a subfolder under one of these (§ Subfolders) or a named path (the token table below). The `script-skip` column lists the globs the deterministic scripts and linters never touch (`*` = the whole tree, `<name>` matches one segment); agent access is stated in § Agent responsibilities, not here. The inbox `script-skip` globs are the configurable knob for inbox linting — loose drafts are normalized, the listed queue and skill containers are left alone.

| wildcard      | literal       | type-defaults | script-skip                                                  | description                                                                                                                          |
| ------------- | ------------- | ------------- | ------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------- |
| `<inbox>`     | `Inbox`    |               | `<user-queue>`, `<type>-`, `<agent>-`, `<skill>-` subfolders | AI drafts land for the user to review — the human review gate; holds `<user-queue>`. Loose drafts are linted; queue + containers skipped |
| `<projects>`  | `Projects` | project       |                                                              | active projects                                                                                                                    |
| `<areas>`     | `Areas`    | area          |                                                              | maintained roles and systems; holds the vault-global per-type catch-alls and `<catchall>` (the asset sink)                          |
| `<reference>` | `References`| reference     |                                                              | evergreen knowledge                                                                                                                |
| `<snippets>`  | `Snippets` | snippet       |                                                              | code snippets                                                                                                                      |
| `<archive>`   | `Archive`  |               | `*`                                                          | archived documents and logs; not in active use                                                                                    |

A kit folder manually renamed off its `literal` is reverted by `audit.py` and the attempt logged (`folder-reverted`); the analyst proposes adopting a repeated rename here rather than fighting it on disk.

**Explorer order is presentation, never load-bearing.** With the optional UI plugin installed (§ Optional UI plugin), the file explorer displays things in **table-row order**: the folders table above sets the root order (Inbox first, Archive last, because that is how the rows are written), § Subfolders sets the subfolder order, and § Types sets the order of floated files inside a folder — reorder the rows to reorder the explorer. Without the plugin everything sorts plain-alphabetically. Nothing reads, routes, or files by position — only the literals above carry meaning.

| token             | resolves to                                                                                                                            |
| ----------------- | -------------------------------------------------------------------------------------------------------------------------------------- |
| `<vault-root>`    | the vault root, parent of `.claude/`                                                                                                   |
| `<user-home>`     | the OS home directory of the current user                                                                                              |
| `<kit-root>`      | `<vault-root>/.claude/` — holds `CONFIG.md`, `CLAUDE.md`, `AGENTS.md`, `RULES.md`, `settings.json`, `settings.local.json` (generated per install), `skills/`, `scheduled-tasks/`, `scripts/`, `hooks/`, `vault-search/` |
| `<skills>`        | `<kit-root>/skills/`                                                                                                                   |
| `<inbox-assets>`  | `<inbox>/Assets/` — the inbox asset-staging folder; a loose asset found in `<inbox>` moves under an `<asset>-`-named parent here (§ Asset folders) |
| `<user-queue>`    | `<inbox>/User-Queue.md` — the AI writes proposals; the user checks them off                                                         |
| `<machine-queue>` | `<inbox>/Machine-Queue.md` — the **file** the user writes instructions into, one checkable line per action, which the action-agent executes. Material — anything larger than a line — is a frontmatter-less file left at the `<inbox>` root instead                                                        |
| `<logs>`          | `<archive>/Logs/`                                                                                                                   |
| `<history>`       | `<vault-root>/.history/` — cold storage                                                                                                |
| `<sandbox-vault>` | `<user-home>/Documents/Notes-Sandbox` — the expendable red-team sandbox vault: structure-altering tests and adversarial rounds against vault mechanisms run **only** there, never in the canon vault |
| `<catchall>`      | `<areas>/Assets` — the universal catch-all (sink): unclaimed assets, untyped/unrecognized content, items gone stale in the inbox |

The vault root itself is the user's draft space (§ File handling); only the folders above are kit-managed.

## Types

The `type:` vocabulary. `additional-frontmatter` is the keys required beyond the global `type, tags, date`; `default-home` is where a member files when nothing more specific resolves — read top-down: a parent's matching subfolder, else the type's vault-global catch-all under `<areas>`, else `<catchall>`. A `parent` naming a skill resolves to that skill's project; an untyped or unrecognized file outside the root draft space routes to `<catchall>` with `review-flag`. **A declared type survives routing:** a drop whose title or frontmatter declares its type keeps that type through every downstream skill — classification fills gaps, never overrides a declaration. **A mixed-type document splits:** user-authored content that genuinely reads as two types splits into one note per type — content preserved, each piece keeping the original's `reviewed` value plus an `inferred` tag, the original archived; a fragment duplicating canon merges into the canon note. An external artifact stays whole — type it `source` and file it. The filing-agent applies this at file time, the janitor on its daily pass; neither queues it. Each drift-prone type's canonical shape lives in its `Format-<Type>` note (§ Format notes).

| type      | pattern               | additional-frontmatter | default-home                                                                              | description                                                                                                                                                                                                  |
| --------- | --------------------- | ---------------------- | ----------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| project   | Title-Case-Hyphens    | parent                 | `<projects>/`                                                                             | active work with a defined goal, start, and end; its file tracks live state and coordinates the plan, assets, and sessions |
| area      | Title-Case-Hyphens    | parent                 | `<areas>/`                                                                                | active role or system maintained without start or end                                                                                                                                                        |
| reference | Title-Case-Hyphens    | parent                 | `<reference>/`                                                                            | one piece of canonical knowledge, valuable with or without context. Used to form larger ideas by linking together references in active sessions.                                                                           |
| voice     | Title-Case-Hyphens    | parent                 | parent's `<voice>/` subfolder, else `<areas>/<voice>/`                                    | resolute prose-voice principle for human-facing text                                                                                                                                                         |
| design    | Title-Case-Hyphens    | parent                 | parent's `<design>/` subfolder, else `<areas>/<design>/`                                  | resolute design principle for skills, agents, and specs                                                                                                                                                      |
| format    | Title-Case-Hyphens    | parent                 | parent's `<format>/` subfolder, else `<areas>/<format>/`                                  | resolute visual-format principle, and the canonical shape of a note type                                                                                                                                     |
| research  | Title-Case-Hyphens    | project parent         | parent's `<research>/` subfolder, else `<areas>/<research>/`                              | investigation, experimentation; unproven — its open questions and the evidence gathered against them, held under its parent until a finding settles it. |
| session   | YYYY-MM-DD-kebab-slug | project                | parent's `<sessions>/` subfolder, else `<archive>/<sessions>/`                            | system-authored work log — what a session did, decided, and learned, under its project; the newest feeds the project file's state-of-play. |
| plan      | Title-Case-Hyphens    | parent                 | parent's `<plans>` subfolder, else `<areas>/<plans>/`                                     | forward-looking mission and execution that tracks the actual state of its in-development work against commits. One canonical plan per scope; a subordinate plan is legal when it declares `parent` at the scope's canonical plan and carries a lane label in its mission line — the multiplicity enforcement reads a missing declaration, never the mere existence of a second file. |
| note      | Title-Case-Hyphens    | parent                 | parent's `<notes>/` subfolder, else `<areas>/<notes>/`                                    | default plain-language markdown; the type a draft takes when no more specific one fits. |
| journal   | YYYY-MM-DD-kebab-slug | parent                 | `<areas>/<journal>/`                                                                      | self-authored personal reflection                                                                                                                                                                            |
| idea      | Title-Case-Hyphens    | parent                 | parent's `<idea>/` subfolder, else `<areas>/<ideas>/`                                     | single-shot capture of a concept or goal, carried forward into a more specific type (project, area, etc.). |
| snippet   | kebab-case            | parent                 | `<snippets>/`                                                                             | functional code, ready to paste                                                                                                                                                                              |
| source    | Title-Case-Hyphens    | parent                 | parent's `<sources>/` subfolder, else `<areas>/<sources>/`                                | external artifact supporting another document, kept whole and preserved as captured. |
| index     | Title-Case-Hyphens    |                        | the folder it covers (its folder-note cover — the file named after the folder, § Numbering)                         | a folder's cover note linking every child file and folder for scoped navigation, chaptered under clear labels that track these subdivisions and their purpose; a root — a project, area, reference domain, or snippet group (CONFIG § Types) — carries one, and so does each `<areas>` type-home fallback folder (`<ideas>`, `<notes>`, `<plans>`, `<research>`, `<sources>`), because § Types sends parentless content there by design and a cover is what keeps that content reachable; the root's children index their files and folders in this master, with project-specific links preserved in their source. |
| addendum  | Title-Case-Hyphens    | target                 | `<inbox>/` if unreviewed, `<archive>/<inbox>/<addendum>/` when completed                  | transient edit merging into its target. A merge synthesizes: the target's rule text rewritten, `weight` bumped where carried, the addendum archived, and the merged file passing the merge-residue lint before the merge logs — appending the addendum verbatim is not a legal merge. |
| log       | kebab-case            |                        | parent's `<logs>/` subfolder, else `<logs>/<agent> or <skill>/`                           | append-only operational log scoped to an agent or session run, one line per action. |
| revision  | Title-Case-Hyphens    | target                 | target's `<notes>/<revision>-<version>` subfolder, else `<archive>/<target>/<revisions>/` | edited working copy produced by the review skill                                                                                                                                                             |

## Subfolders

Patterns accepted under a parent folder, so a project's design or format notes live under that project. The `type-role` is the lookup key; the `subfolder` is its on-disk literal — a plain type-derived name (a legacy install's prefixed names are still recognised, § Numbering). Agents consult this list against their produced content for inference. **Subfolders are optional and on-demand** — created only when content needs one; an empty one is pruned automatically (the housekeeping script). No rigid pre-built tree, no hard boundary beyond what organizes the content.

| type-role | subfolder              | description                                 |
| --------- | ---------------------- | ------------------------------------------- |
| session   | `Sessions`          | session work logs scoped to the parent      |
| research  | `Research`          | research notes scoped to the parent         |
| note      | `Notes`             | plain notes scoped to the parent            |
| voice     | `Voice`             | prose-voice notes scoped to the parent      |
| design    | `Design`            | design notes scoped to the parent           |
| format    | `Format`            | format notes scoped to the parent           |
| source    | `Sources`           | source material scoped to the parent        |
| idea      | `Ideas`             | quick ideas                                 |
| journal   | `Journal`           | journal entries (always the Areas home; never parent-scoped — § Types one-rung) |
| plan      | `Plans`             | plans for future work                       |
| revision  | `Review`            | review working copies filed under a project |
| (asset)   | `Assets`            | non-markdown assets scoped to the parent    |
| log       | `Logs`              | operational logs scoped to the parent       |
| log       | `Logs/<agent-name>` | one log subfolder per agent                 |

## File handling

Required frontmatter on every vault `.md`: `type`, `tags`, `date`, plus the per-type keys in `## Types`; inbox drafts also carry `reviewed: false`, `status: draft`. The vault root is the user's draft space — a root file is left untouched, never typed, normalized, auto-filed, or flagged.

| pattern | required-frontmatter | reason |
|---|---|---|
| `<vault-root>/*` (root files) | none | the user's draft space |
| `*/<skill-name>/SKILL.md` | name, description | skill definition |
| `*/<skill-name>/*.md` | none | skill-internal docs |
| `<archive>/**`, `<history>/**` | preserved as filed | archive and cold storage are never normalized |
| `<user-queue>`, `<machine-queue>` | none | user-interaction files |
| `Assets/*` | none | assets carry no metadata (the `<catchall>` sink, `<inbox-assets>` staging, and any parent asset subfolder) |

The kit-root files (`CONFIG.md`, `CLAUDE.md`, `AGENTS.md`, `RULES.md`) and `<vault-root>/README.md` are exempt by § Scan exclusions and § Operational documents. **Version token:** `vNNN`, zero-padded — `-vNNN` for hyphen/kebab names, `_vNNN` for snake_case. **A revision increments to the next single `vNNN`** (`v005` → `v006`); stacked suffixes (`v005_v002`) are malformed and corrected on sight. Title case is enforced by `audit.py` (the format note holds the title-case rule); legacy numbering prefixes stay recognised but are never added (§ Numbering). The idea lifecycle (handoff stamps, the janitor archives) and orphaned-asset routing are defined in those agents' SKILLs.

**A queue-approved frontmatter repair keeps `reviewed: true`, and so does a mechanical old→new path rewrite that changes no prose; a content edit resets it.** The path-rewrite exemption is bounded: it covers a rewrite whose every change is one known old path to one known new path, it runs only under an approved migration lane, and each pass records the pairs it applied and the files it touched in `<archive>` — an edit that changes meaning, however small, is a content edit and resets the flag.

## Status

The `status:` vocabulary — plain words, set by the document's own lifecycle, never by the act of filing:

| status     | meaning                                                              | set by                                                        |
| ---------- | -------------------------------------------------------------------- | -------------------------------------------------------------- |
| `draft`    | unreviewed, in `<inbox>`                                             | the producer, at authoring                                     |
| `active`   | in use, still being iterated — plans, project covers, live research, and a filed standard still gaining weight | the producer or session                                        |
| `complete` | done; no longer iterated                                             | the filing-agent for finished knowledge content; a session when work closes |
| `open`     | idle ≥ 30 days, abandoned-but-resumable                              | the analyst (the idle-project transition)                      |
| `superseded` | replaced by a named successor; archived                            | the janitor, on the § Content lifecycle supersession trigger   |

A **living document** — a project or area cover, a canonical plan — is **maintained in place**: sessions edit it where it lives, no inbox round-trip, and it keeps its own `status`; `reviewed` governs knowledge content (references, standards, filed notes), not living state documents. Filing never stamps `complete` onto a living document. **The maintained-in-place exception carries a currency condition:** a living document stays current — its state within the currency threshold (default 4 days against the newest session under an active root); past the threshold the `cover-stale` finding fires, and the janitor's state-line refresh or the owning session restores currency.

## Numbering

**The folder-note cover convention is the default.** A folder's cover note is the file named exactly after the folder — `Note-Kit/Note-Kit.md`, `<projects>/Glass-Fracture/Glass-Fracture.md` — and folders carry plain type-derived names with no depth markers. Authored content (notes, entries, references, sources, snippets) is never prefixed; nothing in the default scheme carries a numeric marker, and a fresh scaffold creates none.

An index is the folder's folder-note cover and keeps a current link to **every** child, so a scoped lookup never needs a whole-vault search (`audit` checks the links, the janitor corrects them, an empty index is pruned). An index entry always points at a **filed** home, never an `<inbox>` path; an entry made early is corrected in the pass that files its member.

**Legacy numeric markers.** Existing installs may still run the prefixed scheme below. The tokens stay recognised by the scripts and the UI plugin — a legacy cover (`00-X.md` / `01-X.md`) still reads as the folder's index, and a prefixed folder still resolves to its role — but they are no longer scaffolded and never added to new content.

| marker | depth  | role (legacy)                                                                                                                                                                |
| ------ | ------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `00-`  | top    | intake or operational file, a project/area cover note, a singular plan when it is the root's top note; reserved first for operational files                                  |
| `01-`  | sub    | a sub-element below the top: a canonical plan when it is not the top note, supporting content, or a root's cover index (`01-Reference`, `01-Design`)                            |
| `02-`  | detail | a detail of a sub-element: a plan's changelog, sources and one-off notes                                                                                                     |
| `99-`  | sink   | cold material that sorts out of sight: logs, archived content                                                                                                                |

## Loop budget

`loop_count` starts at 0 and increments per iteration; the default cap is 2 attempts per finding (a tunable knob). After the cap, classify and stop: research and verify-claims share the verdict vocabulary **VERIFIED / DISPUTED / UNRESOLVED**, review uses **open item**, and every non-positive verdict names a concrete resolution path.

## Source-outcome categories

The closed vocabulary for **why a claim went unresolved** — the limit an UNRESOLVED verdict (§ Loop budget) names. verify-claims stamps one per unresolved claim and the analyst tallies them across cited notes, so the set lives here rather than in either consumer. The split between `no-authority` and the merely-missing categories (`no-url`, `paywalled`) separates a claim that *cannot* be sourced from one that simply *was not* this run.

| category           | meaning                                                              |
| ------------------ | -------------------------------------------------------------------- |
| `no-authority`     | editorial or derived; no source can exist — **terminal**, no re-verify |
| `source-silent`    | an authority exists but does not address the point                   |
| `no-url`           | a source exists but its address was not found this run               |
| `paywalled`        | a source exists but sits behind access this run could not clear      |
| `version-specific` | true only for a specific version, unverifiable in general            |
| `private-api`      | depends on a private or internal interface no public source covers   |
| `local-env`        | holds only in a particular local environment — a property of the claim itself |
| `env-unavailable`  | the claim needs an environment, app, licence tier, or interactive UI this run could not reach — a property of the run's access, where `local-env` scopes the claim |

## Scan exclusions

The vault walk never descends into a directory whose name begins with `.` — tooling, config, or cold storage, never active vault content. `scripts/config_variables` exposes the named instances plus the dot-directory test; scripts do not hardcode directory names.

| excluded-dir | reason |
|---|---|
| `.claude` | the kit's own install dir |
| `.history` | cold storage |
| `.git` | version-control metadata |
| `.obsidian` | Obsidian app config and workspace state |
| `.trash` | Obsidian's local trash |

## Asset folders

A folder classified as an **asset** is an opaque, hands-off unit — a repo, a structured export, a captured file tree. The deterministic walk never descends into it, the linters never touch its interior, and the filing-agent moves it **whole** to its home (a project's `Assets/<folder>/`, else `<catchall>`) rather than scattering its members by type. The classification holds **wherever the folder lives — in the inbox and after filing** — so a filed asset is never re-flagged or over-corrected: like `script-skip`, but earned by classification rather than fixed to a root.

A folder is an asset when **any** trigger below fires. `config_variables` parses this table and exposes `is_asset_folder(path)`; `audit.py` and `build_state_index` prune an asset folder from the walk (one outer record, interior never enumerated); the filing, action, and janitor agents read the same predicate **as the first check, before any inference, stamp, or in-folder action** — a file inside an asset folder is never typed, parented, or normalized — and a destructive op is refused inside one.

**A live-process tree is immovable.** A tree a running process is writing into stays where it is until the process ends — no filing, no relocation, no linting.

**Where a new tree starts life.** A new **work** tree — code, builds, datasets, a runtime sandbox — starts in `<user-home>/repos/<project-slug>/`, never in the vault. Handoff and report assets — figures, note-linked media, review bundles — still deposit in-kit, in a project's `Assets/` or an inbox bundle, exactly as before; an asset a note explicitly embeds or links keeps its vault home. The split is by function: the vault holds what a reader opens, the repos root holds what a program runs.

**The retirement store.** A lifecycle-routed retirement of 100 MB or more ([[Vault-Content-Lifecycle-Plan]] § Content lifecycle) lands at `<user-home>/repos/<project-slug>/retired/<date>-<name>/` under the same copy → verify → delete recipe, with its manifest line in `<archive>` — so a retirement with no migration lane still has a derivable destination. Bulk **data** retirements go to the **work-root store** — the out-of-vault bulk-data root named in [[Asset-Repo-Migration-Plan]] D5 — instead of beside a git tree; it is a sanctioned write root on the same depositor terms as the repos root (§ Holds and approvals guardrail floor), and a retirement there records its manifest line in `<archive>` exactly as one to the repos root does.

**Nothing lands ambiguous.** A code-classified tree arriving in `<user-home>/repos/<project-slug>/` is either initialized as a local git repository — `git init`, one initial commit, `.gitignore` seeded with the junk classes (`__pycache__/`, `*.pyc`, caches, large generated outputs), and the git author verified against the tree's intended identity before that first commit — or explicitly labelled `data-store` in the migration log. No remote is added until a push is separately approved (§ Holds and approvals guardrail floor). This keeps the repos root a repos root rather than a second assets folder.

**A plan gates its linked assets.** The inbox draft gate (`reviewed`) governs notes; an asset's review is its plan's approval. When a plan files — or is already filed — each workspace it names lands whole in the plan's project `Assets/`, the plan's path references repaired in the same pass. **The source decides the verb:** a workspace inside `<vault-root>` (the inbox, a root-level folder) **moves** (copy → verify → delete); a workspace outside `<vault-root>` — a repo, a tool directory, any external path — is **copied, never moved or deleted**. The external original stays untouched, and the copy notes its source path and date.

**A loose asset in `<inbox>`** (an image, PDF, binary, or export not already under an asset folder) moves under an `<asset>-`-named parent in `<inbox-assets>`; the filing-agent creates the parent when none exists. Never stamped, never sent to a new top-level folder, never left at the bare inbox root.

| trigger    | matches (configurable)                                                            |
| ---------- | --------------------------------------------------------------------------------- |
| `keyword`  | the folder name contains `-repo`, `-codebase`, `-src`, `-export`, `-dataset`, `-bundle`, `-site`, `-vault` |
| `marker`   | the folder holds a `.keep-whole` sentinel file                                    |
| `vcs`      | the folder holds `.git`, `.hg`, or `.svn`                                          |
| `manifest` | the folder holds `package.json`, `pyproject.toml`, `Cargo.toml`, `go.mod`, or `Makefile` |
| `home`     | the folder sits under an asset home — `Assets` (legacy `02-Assets`, `99-Assets`)                   |

Beyond this table, the filing-agent also treats a container whose members carry **no kit frontmatter at all** as an asset (it has nothing to file by type); mining a folder *into* notes instead is the deliberate note-kit-processor path, not the default.

**Exempt paths.** An owner's working folder named here is excluded from asset classification and every sweep — never typed, filed, or moved to `<catchall>`; it is neither a drop nor a container. An install names its own exempt owner folders here.

## Cold storage

`<history>` (`.history/`, scan-excluded) holds material aged out of active use — auto-populated, hand-managed: a script moves archived content past the retention window into it; no agent prunes inside it.

| setting             | value | meaning                                                                  |
| ------------------- | ----- | ------------------------------------------------------------------------ |
| `archive-retention` | 30d   | archived material older than this ages from `<archive>` into `<history>` |

**Retained ledgers** never age out, whatever their stamp — `age_to_cold_storage.py` skips each row permanently:

| retained ledger | reason |
| --------------- | ------ |
| `<archive>/Inbox/User-Queue/**` | the queue's decision history — the record of every user decision, never re-aged |

## Content lifecycle

Retirement rules for transient content, one row per class, parsed by `config_variables.py`. Destinations follow the § Actions archive shape; everything archived ages onward to `<history>` at 30 d per § Cold storage, and § Reintegration is the road back — nothing retired is unreachable. In-vault transients retire under this table; a repo-bound or 100 MB-and-over tree's physical move executes under [[Asset-Repo-Migration-Plan]], this table supplying only the eligibility trigger and the manifest line.

| class | trigger | destination | actor | cadence |
| ----- | ------- | ----------- | ----- | ------- |
| superseded plan | its successor files canonical with `reviewed: true`, or a dated SUPERSEDED banner sits unedited 7 days | `<archive>/<source-path>/<date>-<filename>` with `status: superseded`; one forward link stays in the successor | janitor-agent | daily |
| completed plan element | a `[x]` item checked 14+ days on an `active` plan | the plan's `<Plan>-Changelog` note, created on first migration; the changelog follows its plan's disposition | janitor-agent | daily |
| event log | the live `<logs>/<agent>/<agent>.md` exceeds 100 KB, or its oldest entry passes 30 days | rotate: close the segment as `<agent>-<YYYY-MM>.md` beside the head; a closed segment ages to `<history>` per `archive-retention` | `rotate_logs.py` at agent run start rotates; `age_to_cold_storage.py` flags the closed segment; the analyst judges the move | each run flag, weekly move |
| session scratch | the owning session log files plus a 7-day grace; or 14 days with no process writes when no session claims it | under 100 MB: `<archive>/<source-path>/…`; 100 MB and over: the retirement store per [[Asset-Repo-Migration-Plan]]'s Kit Rule (copy→verify→delete, manifest line in `<archive>`) — bulk data to the work-root store, code-adjacent to `repos/<project-slug>/retired/`, never bulk data beside a git tree | filing-agent flags; action-agent executes | daily flag, hourly execute |
| transient comms | the carrying mechanism gains a `## Retired tokens` row, or the message is 7 days past its consuming run's log entry | `<archive>/<source-path>/<date>-<filename>` | janitor-agent | daily |
| duplicate asset | a `duplicate-asset` (byte-identical) finding persists across two state-index runs; a `diverged-asset` finding queues the canon choice instead | the canonical copy stays at its filed path; each redundant copy archives (100 MB and over queues for delete-on-approval) | janitor-agent consumes the finding | daily |
| review bundle | the bundle's note is `reviewed: true` and its verdict is recorded on the plan or session, plus a 7-day grace | figures 5 MB and under stay beside the note (the mobile-review workflow keeps its figures); heavier binaries to the project `Assets/`; 100 MB and over to the retirement store | filing-agent | daily |

## Operational documents (never renamed, fuzzy-matched, or normalized)

A script or agent reads each of these at an exact name or path, so the hygiene helpers never touch them.

| operational document                                                                                                    | protected via                  |
| ----------------------------------------------------------------------------------------------------------------------- | ------------------------------ |
| the kit itself — `CONFIG.md`, `CLAUDE.md`, `AGENTS.md`, `RULES.md`, `scripts/`, `skills/`, `scheduled-tasks/`, `hooks/` | § Scan exclusions (`.claude/`) |
| `<user-queue>`                                                                                                          | `<inbox>` script-skip          |
| `<machine-queue>`                                                                                                       | user-authored; agents read and check lines, never rewrite the file |
| `<logs>/<agent>/<agent>.md` heads, `<logs>/Vault-State-Index.md`, `<logs>/Sync-Log.md`, `<logs>/run-lease.md`, `<logs>/fix-verification.md` — `<sessions>` | `<archive>` script-skip        |
| `<vault-root>/README.md`                                                                                                | § File handling                |

**Merge and write targets are confined to corpus content.** An `addendum` (or any merge-onto-target operation) whose `target` resolves into `.claude/`, onto any operational document above, or to any path outside `<vault-root>` is **refused and surfaced to `<user-queue>`** — addendums merge only onto in-corpus content documents. The same confinement binds every **merge**: a merge-onto-target never leaves the corpus, whatever the write rules permit elsewhere. Agent writes outside the vault are confined by the § Holds and approvals guardrail floor, which admits exactly two further roots — `<user-home>/repos/` and the work-root store — and only as a **depositor** under § Asset folders' whole-tree verbs.

**A merge runs only on a `reviewed: true` addendum.** The addendum's own `reviewed` field is the merge gate, exactly as a corpus file's own `reviewed` field is its filing gate (§ File handling). A `reviewed: false` addendum stays in `<inbox>`, untouched — never merged, never archived, never stamped `reviewed: true`. Only the user's review sets `reviewed: true` on a standalone addendum; no agent writes that flag itself. The one exception is a working-set member, auto-reviewed only when its gate file is approved (§ Group approval) — a resolving `target` is never on its own a licence to merge.

**A casing change to a link never dangles it.** A wikilink (or a file rename that changes only casing) is rewritten only to make a link **resolve**, targets the destination file's exact on-disk basename (Title-Case-Hyphens keeps short conjunctions, articles, and prepositions lowercase — `and`, `or`, `the`, `of`, `in` — so a real basename can carry a lower-cased word mid-name), and never turns a resolving link into a dangling one. A rename that moves a link target runs through `rename_with_link_integrity.py` (inbound links rewritten in one pass, straggler-abort), never a hand-edit.

## Log files

Scheduled agents file artifacts under `<logs>`, appending the present file with their information. Never overwrite existing content in the log, continue it with the appropriate information and formatting. Every entry is one line of fixed fields, parsed not read: `timestamp | actor | code | target | value` — `code` a closed token, `target` a path or value, `value` a result or count.

**Log economy.** A log is read by the analyst and future sessions; a log nobody can skim is a log nobody reads. A line carries a code and a short value, never an inventory dump. An open decision logs once when it starts and again only when its state changes; the inbox-wait summary logs only when the inbox's membership changes.

- **Event log** — `<logs>/<agent-name>/<agent-name>.md`, append-only, one per agent. The head file is live and protected; when it exceeds 100 KB or its oldest entry passes 30 days it **rotates**: the segment closes as `<agent-name>-<YYYY-MM>.md` beside the head, and the closed segment — not the head — ages to `<history>` per `archive-retention` (§ Content lifecycle).
- **State snapshot** — `<logs>/Vault-State-Index.md`, one shared file overwritten each run by `build_state_index`; the janitor and analyst read it as their work list.
- **Convention lines** — a convention an agent confirms by evidence (e.g. "standards carry no parent uplink, 0/34") is logged once with code `convention` (the rule, the evidence, the date); every agent consults the log's `convention` lines before re-deriving one. A settled convention graduates into the structured store `<logs>/Conventions.md` — one line per convention: `code | scope predicate | evidence | date` — which `build_state_index` consults at detect time to suppress the settled class, replacing per-case hand patches; a convention that keeps recurring beyond its store row graduates to a one-line entry in this file via the queue.
- **Run boundary** — a pass opens its log with `run-start` and closes it with `run-end`; a `run-start` with no matching `run-end` marks the pass unrecorded and its fix history unverified.
- **Claim evidence** — a line asserting a file's state names the evidence establishing it: the probe run, the hash compared, or the read performed.
- **Fix-verification manifest** — `<logs>/fix-verification.md`, one line per approved kit fix: `<fix-id> | <signal-type> | <target> | <spec>`, signal types `log-line` / `finding-absent` / `block-present`; the fix-effect check reads it each scheduled pass and logs `fix-verified` or re-raises the fix's item.
- **Claim check** — a log line asserting a file's state carries a machine-readable signal in its value cell: `claim: <path> | <expected condition>`. The next `build_state_index` run re-derives each claim from disk and emits `claim-verified` or `claim-false`; a `claim-false` row opens the retraction. The check lands in a different pass from the claim — a claimant never certifies itself.

## Inbox output convention

A run that produces one note lands it loose in `<inbox>`, un-versioned; a run that produces a working set lands one container `<inbox>/<topic-or-date>-<skill-slug>/`, human-facing files at its root and all working/transient material in the container's `<notes>` subfolder (the note-type token, named by § Subfolders — not a special literal). The container in the inbox is itself the resume state, and `-vNNN` marks a revision (archive the prior copy first). On filing, the whole container moves to its destination as a unit, its `<notes>` following it and merging into a matching-named subfolder if one exists. Each skill's SKILL defines its own members and gate file.

## Group approval

A session, agent or skill that creates a folder with one human facing file, and many supporting `<notes>/` documents names one **gate file:** **— the document the user reads**. Approving it auto-approves the rest of the set (stamped `reviewed: true` and `auto-reviewed`) and the filing-agent files them all; until the gate file is approved, no member files.

**Every gate file opens with a decision header** — at most five lines: what this set adds or changes, what specifically deserves a real read (the load-bearing claims or edits), and what approval will trigger (where things file). The header is what the For-You card surfaces; it exists so a review is a decision, not a rubber stamp. The canonical shape lives in the `Format-Gate` format note.

**Member timing follows the gate, both directions.** A member reviewed *ahead of* its gate waits in the inbox with its set — it files only when the gate does; an individual approval never splits a working set. A member that turns reviewed (or surfaces) *after* its gate already filed is a straggler: it is auto-reviewed and filed to the set's filed home — resolved from the gate's new location or the filing log — so it rejoins its set instead of stranding in the inbox.

## Skill slugs

Canonical slugs. `inbox-container` marks whether the skill produces a multi-file working set (yes) or lands loose (no); the `<inbox>` `script-skip` recognizes a `*-<short-slug>/` subfolder as a real working set only for slugs marked yes, where `<short-slug>` is the slug minus its `note-kit-` prefix (`*-research/`, `*-session/`, `*-reviews/`-style container names stay valid after the rename).

| slug                      | inbox-container | container-home                              | description                                                        |
| ------------------------- | --------------- | ------------------------------------------ | ------------------------------------------------------------------ |
| note-kit-handoff          | **yes**         | `<inbox>/<date>-<topic>-session/`           | session wrap-up working set: session log is the **gate file**, atoms and standards in the container — approving the gate auto-reviews the set |
| note-kit-transcription    | no              | —                                          | single-file text transcription, lands loose; a declared type (title or frontmatter) survives processing |
| note-kit-research         | yes             | `<inbox>/<topic-or-date>-research/`         | multi-file research working set; Synthesis gate, stages in `<notes>` |
| note-kit-verify-claims    | yes             | parent or project's `<inbox>/<sources>/`    | multi-file citation working set; cited copy gate, transient in `<notes>` |
| note-kit-review           | yes             | parent or project's `<inbox>/<reviews>/`    | multi-file review working set; Summary gate, audits in `<notes>`   |
| note-kit-youtube-to-note  | yes             | parent or project's `<inbox>/<sources>/`    | container: cited note at root, raw transcript in `<notes>`         |
| note-kit-processor        | yes             | `<inbox>/<source-slug>-note-kit-processor/`              | multi-file note-kit batch processing working set                   |
| note-kit-plan             | no              | —                                          | one canonical plan per scope, lands loose or updates in place      |
| note-kit-execute          | no              | —                                          | works a scope's canonical plan in place — open items to completion, blind-verified, plan and project docs updated |
| note-kit-red-vs-blue      | yes             | parent or project's `<inbox>/`              | adversarial instrument-hardening working set; Round Index gate, manifests and matches in `<notes>`. **Structure-altering rounds against vault mechanisms run only in `<sandbox-vault>`, never the canon vault** |

A container is created when its first output file is written — never up front — and removed if the run aborts before producing anything, so empty husks are not born.

## Tags

Open vocabulary in the `tags:` list. A file has one `type` but any number of tags; this table seeds agent-emitted tags, and project, area, and domain tags grow over time. These tags carry the file's **provenance and confidence tier** — who vouches for it, and how much.

| tag             | alias         | scope | description                                                   |
| --------------- | ------------- | ----- | ------------------------------------------------------------- |
| inferred        | auto-inferred | agent | **a permanent truth-tier marker**: a machine filled this in; never user-verified; prone to rot; less true than a reviewed value. Stays for life as provenance. |
| user-confirmed  |               | vault | the user explicitly validated this value after the fact — outranks `inferred`, short of full review |
| auto-fixed      | autofixed     | agent | a script auto-corrected this file's path, name, or contents   |
| auto-reviewed   |               | agent | a group member approved when its gate file was approved       |
| draft           |               | vault | inbox draft awaiting user review                              |
| review-flag     | needs-review  | vault | a pointer to this file's open `<user-queue>` item — the queue entry is the real record; the tag only marks the file |
| review-complete |               | vault | a review-flagged item an agent has resolved                   |

**No re-litigation:** an agent does not re-infer or re-question a value already tagged `inferred` unless new evidence contradicts it. An agent that resolves a `review-flag` item replaces the tag with `review-complete`, so the flag clears and the item is not re-picked; a `review-flag` never exists without its matching queue item.

**Fast-moving domains** — a reference note in one of these domains additionally requires a version or date anchor for the provenance lint (`no-provenance`); `config_variables.py` parses this table:

| fast-moving domain |
| ------------------ |
| Claude-Code        |
| Obsidian           |
| Software-Licensing |

## Weighted types

A **weighted type** carries an optional `weight` frontmatter field — a stored integer counting how often the kit has re-derived the standard it holds. Today three types carry it: **voice, design, format**. The table is the policy; it extends if another type should ever accrue weight.

| weighted type | carries `weight` |
| ------------- | ---------------- |
| voice         | yes              |
| design        | yes              |
| format        | yes              |

The filing-agent bumps `weight` on merge — a precision-refinement and a recurrence-tally both increment it; a producer records the recurrence as a `type: addendum` onto the standard, and the merge raises the count. A heavier standard is enforced harder: the review crit and the analyst check the heaviest standards first, and severity also lives in the standard's own language. `weight` never lands on a type outside this table. It is **experimental** — measured by the analyst, pending a keep-or-cut decision.

## Atomicity tests

The shared definition of a vault-worthy atomic note — one self-contained, context-free, reusable claim. Both note-kit-handoff (live-session capture) and note-kit-processor (existing-source mining) recognise an atom by the same six tests; apply them in order and act on the first that fails.

1. **One concept (the "and" test).** A claim needing an "and", comma splice, or "; additionally" to be accurate is two claims — split.
2. **Independent reusability (the borrow test).** Two facts different future notes would borrow are separate atoms; a fact only useful beside another merges in.
3. **Context-independence (the survival test).** Stripped of its session, project, or source, the note still asserts something true and usable, or it folds back into the log.
4. **Link, don't embed (the single-source test).** A reusable sibling fact is `[[wikilink]]`ed, never restated; a shared fact lives in one note.
5. **Evergreen, not event (the tense test).** An atom states how something *is* ("the accessor returns null before init"), not what happened; lift the generalization or leave it in the log.
6. **Done (the subtraction test).** Finished when removing any sentence loses a reusable fact and no second independent claim remains to split off.

The two failure modes: too **big** (compound — caught by 1, 2, 6) splits; too **small, stale, or too tied** (caught by 3, 5) folds back.

## Actions

The canonical shapes for the structural verbs a producing agent picks. The action-agent matches an approved item to a row; an item matching no row is still carried out as directed, archive-first on any destructive step, never refused.

| action             | shape                                                  | authorized-agents                                                | description                                                           |
| ------------------ | ------------------------------------------------------ | ---------------------------------------------------------------- | --------------------------------------------------------------------- |
| Rename file        | `<file>` → `<new-name>`                                | filing-agent, janitor-agent, action-agent                        | rename in place; rename-with-link-integrity updates inbound wikilinks |
| Revise / Edit file | `<file>`: edit body in place                           | filing-agent, janitor-agent, action-agent                        | rewrite the existing file, same path and name; archive-first          |
| Set frontmatter    | `<file>`: `<field>`: old → new                         | filing-agent, janitor-agent, action-agent                        | set or update a frontmatter field                                     |
| Archive file       | `<file>` → `<archive>/<source-path>/<date>-<filename>` | filing-agent, janitor-agent, action-agent                        | copy to archive, confirm, remove source                               |
| Create folder      | `<projects>/<name>/`                                   | filing-agent, janitor-agent, analyst-agent, action-agent, skills in `<inbox>/` | create a missing project, area, or subfolder                          |
| Relocate file      | `<path-from>` → `<path-to>`                            | filing-agent, analyst-agent, action-agent                        | active-to-active move; archive-first, then rename-with-link-integrity |
| Create index       | `<parent-folder>/<index>.md`                           | filing-agent, analyst-agent                                      | build an index note linking a folder's members                        |

**Link integrity is mandatory on every in-vault move.** A rename or relocate of an active file runs through `rename_with_link_integrity` so inbound wikilinks follow the file; a move that bypasses it leaves ghosts the `dangling-link` finding then surfaces (§ Log files). When the helper cannot run, record the broken inbound links for the janitor's next pass rather than leaving the move silent.

## Holds and approvals

The one home for what blocks work and what is pre-approved; both tables are **user-editable**. A hold exists only when completion requires something the agent cannot produce; every agent resolves a potential hold against this table. Everything else completes.

| hold | trigger | completable remainder |
| ---- | ------- | --------------------- |
| user-presence | the step needs the user's accounts, hardware, or eyes — a login, a purchase, an on-device or on-screen confirmation | do everything up to that step (build, deploy, stage, write the spec); record the live part and its continuation as a resume record on the relevant `<plan>` and clear the queue line (§ Queue protocol) |
| live-process | a running external process is writing into the target tree (an active sim, a mid-flight run) | act on everything outside the tree; re-check next pass — the hold expires with the process |
| user-deferral | the user recorded "wait" / "decide later" in the queue or its history | escalate the decision onto the relevant `<plan>` as a resume record and clear the queue line; the plan carries it until the user answers, the session brief surfacing it on engagement (§ Queue protocol) |

**A hold escalation writes a resume record.** The escalated decision lands in the plan's Open Decisions as one line — `- [ ] HOLD (YYYY-MM-DD) — <forward-looking resume prompt>` — the blocked step restated as the opening move of a new session ("Let's resume `<project>, starting by <the blocked step>`"). The holds surface and the session brief match on the `HOLD (` prefix; once the queue line clears, the plan entry is the only live copy, and every resolution path — session work, a resume run, a supersede — closes that same line.

**Guardrail floor (not table-editable):** refuse and surface a mass or unbounded destructive operation with no named, fully-specified targets; archive first before every destructive step (§ Versioning and archiving discipline); write only inside the vault, its kit root, `<user-home>/repos/`, and the work-root store — the last two as a **depositor** under § Asset folders (whole-tree moves and deposits), never editing inside a tree there on an unattended pass (§ Operational documents); run outward publication (a repo push, an external send) only on an approved item naming it. Within the floor, a modification the user asked for executes.

**Standing approvals** — recurring classes with consent granted here once. A hold row **outranks** a standing approval; § Asset folders still governs filing — an approval that edits inside an asset tree names its development workspace. When a hold escalation recurs for an unlisted class, the analyst proposes a new row.

| class | pre-approved scope | verification |
| ----- | ------------------ | ------------ |
| approved kit fix | an `[x]` queue item naming the kit file and the change — scripts, SKILLs, hooks, CONFIG | archive-first; apply; re-read and diff-verify; one log line |
| local agent redeploy | copy vault-source `scheduled-tasks/` SKILLs to `<user-home>/.claude/scheduled-tasks/note-kit-*` on an approved item | hash-compare every copied file; pre-redeploy copies archived |
| note-kit-ui pipeline | edit plugin source in the repo working tree (`<areas>/Note-Kit/Assets/note-kit/plugin/note-kit-ui/src/` — a named development workspace), esbuild, deploy `main.js`+`styles.css` to `.obsidian/plugins/note-kit-ui/`, reload | desktop screenshot via obsidian-cli; only the on-device mobile look stays user-presence |
| local automation build | build and test headless tooling on this machine (e.g. a batch-render or data-processing spawner) under a project's `Assets/` workspace | the build's own test output; anything needing a licensed GUI app or the user's eyes splits off as user-presence |
| inbox edit-drop | a frontmatter-less FILE at the `<inbox>` root carrying a clear directive over its own content executes inline the pass it is found: archive the original, make the directed edit, deposit the result as a `reviewed: false` draft (a folder drop stays under § Asset folders' whole-move rule) | the deposited draft's review is the gate; one log line per drop |

## Harness permissions

The harness allowlist is **defined here and mirrored** into `settings.local.json` by `sync_config` (merge; hand-added entries are preserved). Edit this table, then re-run sync.

| rule | reason |
| ---- | ------ |
| `Edit(.claude/**)` | approved kit fixes execute unattended (§ Holds and approvals) |
| `Write(.claude/**)` | same — new helper scripts, logs, generated files |
| `Bash(python .claude/scripts/*)` | kit scripts (sync, state index, helpers) run on schedule |
| `Write(<user-home>/repos/**)` | depositor writes for migration lanes and the deposit convention (§ Asset folders); whole-tree moves only, never editing inside a repo |

## Rules

The always-on obligation set, canonical here and distributed by `sync_config`: the `rule` column lands in CLAUDE.md and AGENTS.md as the `## Always-on rules` orientation block (full text, read at session start), and the `reminder` column becomes the generated `RULES.md` the cadence hook injects (§ Rules injection). A `reminder` is the rule's shorthand — the shortest phrase that re-anchors the behavior mid-session, paying fewer recurring injection tokens than the teaching version (e.g. rule: the full reviewed-reset sentence with its exceptions; reminder: "Edit a `reviewed: true` note → reset to `reviewed: false`, back to `<inbox>` (exceptions in CONFIG)"). An empty `reminder` cell injects the rule at full text. Condense a reminder only on observed drift, and keep the no-delete prohibition expanded — condensing it measurably costs compliance ([[Synthesis-Table]]).

| rule | reminder |
| ---- | -------- |
| Your output is a draft: write it to `<inbox>` with `reviewed: false`. | |
| Never delete or destructively edit vault content outside of the `<inbox>`; version and `<archive>` it instead. | |
| Write only inside the vault, its kit root, `<user-home>/repos/`, and the work-root store — the last two as a depositor (whole-tree moves and deposits), never editing inside a tree there on an unattended pass. | |
| Editing a `reviewed: true` note resets it to `reviewed: false` in the same change, and moves it back to the `<inbox>`. Exceptions live in CONFIG: a living document (cover, canonical plan) is maintained in place, and a queue-approved frontmatter repair keeps its approval. | Edit a `reviewed: true` note → reset to `reviewed: false`, back to `<inbox>` (exceptions in CONFIG). |
| Give every note a `type` and a link up (`parent` or `project`) so nothing is orphaned. Stamp only a link that resolves — search first; no match means leave it empty for inference, never invent a name. | Every note: `type` + a resolving link up (`parent`/`project`); no match → leave empty, never invent. |
| Search the vault before writing anything new (`mcp__vault__vault_search` before Glob/Grep); never invent names, fields, or mechanisms. | `mcp__vault__vault_search` before writing anything new; never invent names, fields, or mechanisms. |
| Assets carry no frontmatter. | |
| Artifacts stand alone: no conversation residue, changelog, attribution, or next-step notes in draft files. | Artifacts stand alone: no conversation residue in drafts. |
| Procedural text instructs; knowledge text declares; both in plain words. | |
| A question asked in chat that goes unanswered lands in the `<user-queue>` before the session ends. | An unanswered chat question lands in `<user-queue>` before session end. |
| Sub-agents inherit none of this — name every tool and rule they need in the spawning prompt. | Sub-agents inherit none of this — name every tool and rule in the spawning prompt. |
| Maintain any active plans and relevant project docs, crossing out finished work and correcting course on altered targets; after delivering content or acting on work directly, update them to match and revise the delivery to the user's weighted standards. | After delivering or acting directly, update active plans and project docs (cross out done, correct course), and revise to the user's weighted standards. |
| A verification pass that amends a fact updates or flags every document that states it — source guide, synthesis, cover — not only the atoms. | A verified amendment updates or flags every document stating the fact, not only the atoms. |
| Use the vault's configured skills (§ Skill slugs) when alias actions are invoked (planning, researching, etc.). | Alias actions (plan, research, …) run their configured skill (§ Skill slugs). |
| All edits to vault content outside of `<inbox>` check with the destination's mission, plans, and project scope; alterations are logged in `<archive>`. | Edits outside `<inbox>`: check the destination's mission, plans, scope; log alterations in `<archive>`. |
| On a repeated correction or lengthy confusion, stop and raise the issue in the `<user-queue>` to clarify intent rather than guessing again. | Repeated correction → stop and raise it in `<user-queue>`, not another guess. |
| Consult the settled conventions before re-deriving or re-reporting a finding: the structured store `<logs>/Conventions.md` and each log's `convention` lines govern their scopes until their evidence changes (§ Log files). | Check `<logs>/Conventions.md` + log `convention` lines before re-deriving or re-reporting a settled finding. |

## Rules injection

`hooks/load-rules.py` injects `RULES.md` on a cadence: the session's first prompt and every `rules-injection-period` prompts after (1, 31, 61, … at the default). The hook reads the period from the table below at each invocation; an unreadable or missing value falls back to 30. Period 1 means every prompt. `RULES.md` itself is generated from § Rules by `sync_config` — edit the table, never the file.

| setting | value |
| ------- | ----- |
| `rules-injection-period` | 30 |

## Queue protocol

`<user-queue>` (AI → user) and `<machine-queue>` (user → AI) mirror in structure: each a checkable `.md` one party writes and the other reads. The canonical item shape is `Format-User-Queue` / `Format-Machine-Queue` (templates/format; filed as format notes in the vault), enforced through the action-agent SKILL; only a judgment call reaches `<user-queue>`. A routine fixable violation goes to a script.

- **A checked item clears; an unchecked one waits.** The `[x]` mark is the trigger to clear: the action-agent carries out a checked option if it is immediately executable, otherwise it escalates that decision onto the relevant canonical `<plan>` as an open decision (addendum-merged onto its step, else appended to the plan's open decisions), archives the queue snapshot, clears the line, and logs it — and this holds even when the checked option is a hold or "decide later" choice, since the `[x]` itself clears the item, not the content of the option. A `[-]` rejection is removed with its reason logged. An undecided `[ ]` item stays in the queue as the user's pending decision — the agent neither escalates nor re-writes it. A decision the user has acted on lives on its plan, where the session brief and For-You surface it; an undecided one stays where the user can see and check it. (An item the live record has already resolved or made moot is superseded and cleared regardless of its checkbox.)
- **No checkbox, no item.** Write every `<user-queue>` item as one `###` heading with at least one `- [ ]` option line — the UI surfaces only checkbox decisions. Offer a derivable candidate as its own concrete option first — an unhomed folder proposes `Create project <Folder-Name>` from its own name; when no candidate exists, ask for the missing information *as* the option; for an advisory, use the dismissal option `- [ ] Acknowledged — clear this item`. Every option names a processable action — approve a choice, reject, or dismiss; the one legal deferral is a hold option naming its conversion — its `[x]` clears the line into a resume record on the relevant `<plan>` (§ Holds and approvals). Binds every queue writer: filing, janitor, analyst, action-agent, and any skill surfacing a clarification.
- **One judgment per item.** Every option on an item is an alternative resolution of the same judgment. A set of independently executable steps is a checklist, and a checklist routes to the relevant `<plan>`, never the queue. A producer pass raises as many open decisions as its evidence genuinely yields — one item per decision, each independently evidenced (ruled 2026-07-26: the former one-decision-per-pass cap is retired); a decision scoped to a specific plan's execution still routes to that plan. Binds every queue writer: filing, janitor, analyst, action-agent, and any skill surfacing a clarification.
- **One ask.** Gather everything execution needs into the proposal — destination, name, scope, each missing value as an option or `REPLACE-WITH-` field. Walk the execution forward before writing the item. The analyst flags a follow-up question on an answered item.
- **Resolve a hold by disposition.** An approved item the agent cannot finish this pass is resolved across three dispositions while the rest of the request runs unattended (splitting into sub-agents where that helps): supersede a stale item, or escalate a user-reliant element onto the relevant `<plan>` as an open decision — either way the queue line clears.
- **Supersede a stale approval.** Each run, read every open item against the live record its work tracks — a stated action with its date, the linked `<plan>`, the cover page, the repo. When that record carries the action's outcome — a commit, a shipped version, a plan gate marked done — or the work moving underneath has made the item moot, supersede it: archive the queue snapshot, retire the line, and log it `queue-superseded` naming the record. A step on the user's own clock, like a repo push, is superseded by the next push that lands.
- **Outcomes go to `<logs>`.** Write results, answers, and FYIs to the producer's log in `<logs>/<agent>/` or an inbox note; open decisions live on their plans, not the queue.
- **Plain language.** A proposal reads in established vocabulary the user can answer without opening other documents — no internal shorthand or codename references.
- **One item per open decision.** A decision awaiting the user produces one queue item, re-logged only on a state change.
- **Elevate a user-presence need to a `<plan>`.** When an action or part of one needs the user present — a login or gated fetch, an on-screen judgment, an un-launched dependency, an irreversible edit — split that part off and reframe the remainder to run unattended. A split yields two items: the user-present part as an `<addendum>` to the project's canonical `<plan>`, or a `<plan>` in `<inbox>` when it matches no project; with the continuation recorded as an open decision on that `<plan>`, naming only what the agent can carry out next run. When the part matches a step in the canonical `<plan>`, open or closed, archive the `<addendum>` and clear the queue line.
- **A true hold elevates at once.** An item that is indivisible, tied to no project, and risky — a vault-wide refactor that would break agent function — moves to a `<plan>` in `<inbox>` on the first pass, the decision recorded there, and the queue line clears.
- **An un-actionable item migrates.** When a `<machine-queue>` item resolves to no action at all — naming no skill, content, target, or directive even after shape recovery — the agent removes the line (queue snapshot archived first) and raises one `<user-queue>` clarification quoting the original text verbatim, with a fill-in option to restate the instruction. An understood but live-blocked line elevates to a `<plan>` (above); an unintelligible line has no work to hold a place for, so it migrates to one clarification.
- **Raised here.** A question asked in chat that goes unanswered lands here before the session ends, in the proposal shape. An unattended run infers past routine gaps and raises only a mission-critical question or judgment call here as a decision — a blocker resolves by the hold disposition (§ Holds and approvals), never as a raised item; the user expects full automation and answers no in-chat question mid-run. Queue the decision or deliberately drop the question, and continue the run either way.
- **A checked item never lingers.** A `[x]` item resolves the pass it is read and its line clears — executed when immediately runnable, otherwise escalated onto its plan (above). An undecided `[ ]` item stays for the user.

## Versioning and archiving discipline

Before any destructive operation (overwrite, supersede, merge-onto-target, revise-in-place), the agent archives the source to `<archive>/<source-path>/<date>-<filename>-<version>` and confirms the copy exists before removing the source. Nothing is deleted outright. **A scripted rewrite is a destructive operation:** a helper script that rewrites a structured file archives the source first and verifies a structural invariant (section count, entry count, parseability) immediately after the write — a verification failure restores the archived copy.

**A supersession is one transaction.** A plan ratification executes as one logged pass — the successor filed, the superseded copy marked (`status`, `superseded_by`, a forward link to the successor), the cover's canonical-plan line updated, and each move per-move verified — so a half-landed ratification never exists between passes.

**A manual operation is a logged operation.** A hand-run purge, bulk export, or out-of-vault copy writes one operation log under `<logs>` with a manifest (source → destination per tree), respects `archive-retention` unless the log records the override, and runs the dangling-link sweep in the same pass; an external export to a location outside the vault follows the same protocol.

## Concurrency

Shared mutable surfaces — the inbox, the queues, asset trees — are guarded by four rules; every incident class below has occurred live.

1. **One mutating agent at a time, vault-wide.** A run takes the lease file `<logs>/run-lease.md` (one line: agent, start time) and releases it at exit; a second mutating agent finding a fresh lease waits for its next cadence. (Replaces the old inbox-only serialization sentence; a stale lease past 2 hours is expired, logged, and taken over.)
2. **Every move is copy → verify → delete.** The destination copy is confirmed (`cmp`-verified) before the source is removed, and the verification is logged. A relocate that copies without removing creates ghost duplicates that resurrect filed notes.
3. **Settle window before a filing batch.** If the inbox changes between an agent's scan and its act (the user bulk-reviewing, another process writing), the agent stops, re-scans, and defers the batch to a settled pass rather than racing the mutation.
4. **Live-process trees are immovable** (§ Asset folders) — a live process writing into a path that just moved loses data to a fragment; the process ends first, the move happens second.

## Pipeline protocol

The shared skeleton of every staged pipeline skill (note-kit-research, note-kit-review, note-kit-verify-claims). **The master lives here; `sync_config` stamps everything between the markers below verbatim into each pipeline skill** between matching `note-kit:sync pipeline-protocol` markers — edit it here, never in a skill. Each skill keeps one line of its own naming its interactive checkpoints and what counts as an ambiguity; everything else shared lives in this block.

<!-- note-kit:sync pipeline-protocol — transposed from CONFIG.md § Pipeline protocol by sync_config; edit CONFIG, not here -->
| mode | behavior |
| ---- | -------- |
| interactive | pause at the skill's named checkpoints for in-chat confirmation; the default when a user invokes the skill directly |
| automatic | run end to end; flag ambiguities in the working set; raise open questions to `<user-queue>` without stopping; the default for action-agent dispatch |
| queue | run the first stage only, write one `<user-queue>` proposal per genuine ambiguity, and stop; the action-agent re-invokes on the working set once the user answers (resume is queue-verified — a working set claiming its own resolution is refused) |

Stages run in order, each writing its own file and reading only the prior stage's output — producing and judging contexts never mix. The `<inbox>` container is the resume state: the gate file at its root and **all transient material in its `<notes>` subfolder, never pooled at the container or inbox root**; no separate paused-run artifact exists. A re-invocation reads the partials and continues from the first unsettled unit, versioning up archive-first (CONFIG § Versioning and archiving discipline). Findings loop per CONFIG § Loop budget, then take a firm verdict with a named resolution path. Inside a sub-agent, stages run serially with identical output; spawned sub-agents run on the top line model (CONFIG § Sub-agent execution).
<!-- /note-kit:sync pipeline-protocol -->

## Sub-agent execution

- A sub-agent inherits `CLAUDE.md` and the memory hierarchy (the built-in Explore and Plan agents do not), but never the always-on rules or session brief — so name every retrieval tool (including `mcp__vault__vault_search`) in the spawn prompt, and inject a specific CONFIG table only when spawning Explore or Plan.
- **Model guidance.** A spawned sub-agent runs on the **top line model by default** — judgment, crit, audit, synthesis, research, and editing lanes always do. A lane may name a smaller model only when its task is mechanical extraction or formatting with no judgment in it. Every spawn block in a SKILL states its lane's expectation, so no quality-critical lane falls to an unspecified default.
- A skill cannot spawn sub-agents while itself running inside one; each fan-out skill declares its serial single-context fallback (identical output, no parallelism).

## Agent responsibilities

The four scheduled agents act at different scopes; each SKILL describes only its own work. Cadence is a deployment setting; the defaults are not canon.

**Tiered reinforcement.** Structural correctness is enforced at four tiers: **smart output** (producers author to the format notes and canonical tables), **smart filing** (the filing-agent corrects evidence-backed defects at file time), **smart cleaning** (the janitor's daily per-file pass), **analyst** (weekly macro view). Every tier applies the same standards at its own scope.

| agent                  | scope                                 | trigger                                    | recommended cadence | does                                                                                                                                                                                                                                    |
| ---------------------- | ------------------------------------- | ------------------------------------------ | ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| note-kit-janitor-agent | whole vault, per file                 | `audit.py` run log                         | daily               | resolves inference the script could not — parent, project, type, tags — and **owns structural cleanup**: corrects an evidence-backed wrong declared type, merges a duplicate or foreign map into the canon index, and enforces **one canonical plan per scope** (a subordinate plan is legal when it declares `parent` at the canonical plan with a lane label — § Types; enforcement reads a missing declaration, then links, merges, or archives). Backstops filing-agent inference. Flags a stale `reviewed: true` note whose plan or spec changed underneath it. |
| note-kit-filing-agent  | inbox file vs. its destination        | `reviewed: true` inbox files               | daily               | moves approved `reviewed: true` inbox files to their homes and stages orphaned inbox assets; a **work**-classified asset tree files to the repos root per § Asset folders, while a note-linked asset keeps its project `Assets/` destination; **infers past gaps and files** — the janitor corrects a wrong inference (`inferred` tags mark it); a hold resolves by disposition (§ Queue protocol) and appears on the holds surface (§ Holds and approvals). **Never touches a user drop** — a frontmatter-less file at the inbox root is the action-agent's. |
| note-kit-analyst-agent | whole vault, macro                    | accumulated logs + state index          | weekly              | statistical view over the logs and the state index: suggests splits, consolidation, indexes, and canonical renames; flags recurring prompt-correction patterns; drafts a kit plan per multi-finding root cause, queue-linked; flips a 30-day-idle project to `status: open`; reports drafts past the dwell window as one digest line; stewards the built-in memory under the user gate. |
| note-kit-action-agent  | both queues and inbox drops           | pending approved work                      | hourly              | executes approved `<user-queue>` items the pass it finds them, **filing-shaped moves included**; raises a needed back-and-forth as one `<user-queue>` decision the same pass; acts on the `<machine-queue>` checklist; **owns every user drop at the `<inbox>` root** — routes a skill instruction to its skill, a content drop onward, and surfaces an un-actionable drop to `<user-queue>`; re-invokes a skill whose queued clarifications the user answered. |

Scheduled agents run from `<user-home>/.claude/scheduled-tasks/note-kit-*` via the Claude Code **Desktop app**; a terminal-only install runs the same SKILL bodies as cloud routines (`/schedule`) instead.

## Optional UI plugin

`note-kit-ui` (an Obsidian plugin) is an **optional, one-way presentation add-on**: it reads the kit's `type`/`reviewed`/`status` frontmatter and styles it at render time (including plain verdict tags like `**VERIFIED | STRUCTURAL | api-docs**`). The kit never depends on it — no skill, agent, or script references the plugin — and the kit is fully functional without it. It ships with the installer as an optional step, versions on its own cadence, and lives at `<vault-root>/.obsidian/plugins/note-kit-ui/`.

## Reintegration

Importing cold material (`<history>`, an old vault) back into active use is a gated protocol, never an ad-hoc copy:

1. One container per batch: `<inbox>/<domain>-Review/` with a `Reintegration-Manifest.md` **gate file** (contents, compatibility check, proposed homes).
2. **Copy, never move** — the cold original stays in place until the import is filed and confirmed.
3. Every batch updates its row in the **History-Reintegration-Tracker** (a filed canonical note under the Note-Kit area — not an inbox draft), which dispositions every cold set as keep-cold or import. Cold content has no other route into the corpus.

## Self-modification

How an **approved** change to the kit's own files lands. **The user's `[x]` on a queue item naming the file and the change is the supervision** — the action-agent executes it unattended per § Holds and approvals (archive-first, apply, diff-verify, log). An agent's own idea for a kit change queues first. The two artifact kinds install differently:

- **Skills** (`<skills>/note-kit-*/SKILL.md`) are auto-discovered as project skills in vault sessions — a vault-source edit is **live for vault work immediately**, no install step. The `<user-home>/.claude/skills/` copy matters only for use outside this vault; it updates on redeploy.
- **Scheduled agents** run **only** from the deployed `<user-home>/.claude/scheduled-tasks/note-kit-*` copies — a vault-source edit is the source of record but changes nothing until redeployed. On an approved item, edit the vault source and run the local redeploy (hash-verified, § Holds and approvals) in the same pass; verify against the deployed file ([[Deployed-Is-Not-Loaded]]).
- **Outward publication:** run a store-back to the publication repo or a git push only on an approved item naming it, git author checked first. A whole-kit release waits while the driving plan holds open, non-deferred checkboxes; flip a checkbox only on an independent read of the artifact.

**Janitor mechanical carve-out (queue-decided 2026-07-24).** The janitor repairs the mechanical contradiction classes without a queue item: supersession markers and forward links; doubled-canonical resolution where the successor is unambiguous (ratified, newer, linked); cover state-line refresh; frontmatter and index membership. Each repair runs archive-first with one log line and surfaces in the analyst report. A genuine judgment call — which of two divergent texts wins — queues.

## Deprecation

A retirement event — a mechanism, folder scheme, vocabulary term, or machine name taken out of service — appends its tokens to § Retired tokens and runs a blast-radius sweep in the same pass: grep the live docs for the retired mechanism, edit or queue every hit, and log each one. The table plus the sweep is the whole event; a deprecation that touches only the files named in the moment leaves stragglers.

## Retired tokens

Tokens retired from live documents. `config_variables.py` parses this table; `audit.py` greps live docs each janitor pass and emits `retired-token` per hit — scripts inside project `Assets/` are in scope for path tokens. A pattern row matches a form rather than a literal.

| token | retired-date | replacement | scope |
| ----- | ------------ | ----------- | ----- |
| `<outbox>` folder (`Outbox/`) | 2026-07-26 | user drops are frontmatter-less files at the `<inbox>` root; `<machine-queue>` is `<inbox>/Machine-Queue.md` | live docs |
| `NN-` numbered folder prefixes, pattern (`00-Inbox`, `01-Projects`, `02-Assets`, `99-…`) | 2026-07-10 | plain type-derived names (§ Numbering) | live docs + Assets scripts |

## Code-canon registry

Every canonical code tree the vault documents, one row each — documentation cites canon homes from this table, stale-copy detection reads it as the reference list, and a canon living outside both the vault and `<user-home>/repos/` stays visible to the instruments. `config_variables.py` parses this table; the external-canon liveness check consumes it read-only — the kit never writes inside a repo. A row whose home reads `unresolved` is a standing `canon-doc-stale` hit until the tree is located or retired. Each install seeds its own rows; a migration or deposit appends a row for every tree it creates.

| canonical tree | home | vault seam note |
| -------------- | ---- | --------------- |
| note-kit template tree | `<areas>/Note-Kit/Assets/note-kit/` | [[Note-Kit]] |

## Format notes

Each drift-prone type's canonical shape lives in a `format` note (`Format-<Type>` — `Format-Session`, `Format-Reference`, `Format-Plan`, `Format-Gate`, …), referenced from § Types. They are both the generation reference and the conformance reference:

- **Producers consult them while authoring** — fetch `Format-<Type>` before writing a typed note and use its shape instead of re-deriving one. A producer never *edits* a format note mid-run, and a missing format note is never license to invent a shape: fall back to § Types and flag the gap.
- **The analyst checks filed notes against them** for type-shape drift; the janitor and filing agents carry out the per-file corrections it surfaces.
- A format note carries the type's **target length** where one is set (session ~600 words; queue-proposal context ≤ 2 lines; gate decision header ≤ 5 lines) — the budget is a checkable standard, not a vibe.

## Helper-script automation

A new script registers its trigger here in the same change.

| script | trigger |
|---|---|
| `config_variables.py` | imported by every kit script at startup |
| `config_shape.py` | head of every `sync_config.py` run (sync aborts on a refusal); manual invocation supported. Validates every table a parser depends on — § Folders (roots + token table), § Types, § Numbering, § Rules, § Tags, § Actions, § Skill slugs, § Subfolders: repairs safe drift in place (whitespace, tabs, missing pipes, malformed or missing separator rows) and refuses unsafe drift (missing section, table, or required column; a missing or duplicate token; a duplicate type, marker, tag, action, slug, or subfolder; a mid-row cell-count mismatch; an unclosed backtick; an empty load-bearing cell) with a file:line report |
| `sync_config.py` | end of any session that edited `CONFIG.md`; any pass that edits a `weight` field — a filing merge, the janitor, the action-agent, a live session — runs it in the same pass; daily. Runs `config_shape.py` first, regenerates the CLAUDE/AGENTS orientation tables and `## Always-on rules` blocks, generates `RULES.md` from § Rules (reminder column, full text where empty), stamps the § Pipeline protocol block into the pipeline skills, and mirrors § Harness permissions into `settings.local.json` (merge, hand-added entries preserved) |
| `build_state_index.py` | start of each janitor-agent run (apply mode — a detect-only audit refreshes nothing); consumed again by analyst-agent. Records a per-file body content hash in the snapshot; `reviewed-stale` fires only on a recorded content change newer than the review, with bulk-touch (≥10 shared mtimes) and reciprocal-pair findings suppressed; counts archived members for lifecycle types so a healthy lifecycle never reads `type-unused`; flags a loose non-md asset whose identical bytes recur (`duplicate-asset`) or whose filename carries divergent content (`diverged-asset`) within one owning root — asset folders (git/`.keep-whole`) and `complete` projects excluded, the janitor reconciles; the `dangling-link` finding suppresses a lowercase-kebab memory slug cited inside an immutable `type: session` log — vault basenames are Title-Case, so a bare kebab slug reads as a `<user-home>/.claude` memory citation; sessions only, every other type stays in scope |
| `audit.py` (at `<kit-root>/scheduled-tasks/janitor-agent/`) | each janitor-agent run; detect-only by default, writes only with `--apply`; invokes normalize_type, normalize_tag, rename_with_link_integrity, and subfolder_housekeeping inline; reverts an off-canon kit folder name; resolves missing dates deterministically (archive provenance → session date → import date, tagged `inferred`); flags a duplicate canonical plan per scope (`duplicate-canonical-plan`); validates hook registrations in `settings.json`/`settings.local.json` — a malformed matcher group is silently ignored by the runner, so a dead or script-less registration is flagged (`dead-hook-registration`, `missing-hook-script`, `hooks-settings-unparseable`); never walks the vault root's loose files or an asset folder's interior |
| `subfolder_housekeeping.py` | inline by audit.py each janitor run; prunes empty subfolders and empty indexes (deterministic) |
| `index_helpers.py` | filing-agent and analyst-agent index inserts |
| `rename_with_link_integrity.py` | audit.py rename passes; manual invocation supported |
| `normalize_type.py` / `normalize_tag.py` | inline by audit.py and filing-agent |
| `scaffold_vault.py` | manual; documented in README.md. Seeds `<machine-queue>` and `<user-queue>` with checkable example items; `--with-ui-plugin <dir>` installs the note-kit-ui Obsidian plugin and merges it into `community-plugins.json` |
| `dedup_vtt.py` | invoked by the youtube-to-note skill |
| `verify_claims_log.py` | end of each verify-claims run |
| `age_to_cold_storage.py` | daily; logs `<archive>` item age past `archive-retention` with a manifest line per sweep; skips § Cold storage retained ledgers permanently; the analyst judges the move into `<history>` |
| `rotate_logs.py` | run start of every scheduled agent pass; rotates any `<logs>/<agent>/<agent>.md` head past 100 KB or 30 days into `<agent>-<YYYY-MM>.md` beside it (§ Log files); `age_to_cold_storage.py` ages the closed segment |
| `archive_first.py` | imported by every kit script that deletes, renames, or overwrites a filed file; writes the pre-image to the mirrored `<archive>` path on a probed free name and returns only on a verified copy — a mutating call site that bypasses it is an `audit.py` finding (`archive-first-bypass`) |
| `verify_surface_parity.py` | weekly, on the analyst pass; read-only newline-normalized diff of live `.claude/` against the template tree, licensed divergences allowlisted pair-exact, generated per-install files excluded; emits `surface-drift` |
| `verify_replica_parity.py` | manual, on any machine holding a replica; diffs the local markdown set against the synced `Vault-State-Index.md` (paths + body hashes); emits `replica-divergence` — the Obsidian-Sync partition instrument |
| `build_holds_surface.py` | end of every action-agent pass (SKILL § 5); regenerates `<logs>/Holds.md` from every live plan's open `HOLD (` records — generated, never hand-edited |
| `hooks/config-sync.py` | PostToolUse hook on Edit/Write/MultiEdit. Runs `sync_config` when an edit touches `CONFIG.md` so the generated tables in `CLAUDE.md`/`AGENTS.md`/`RULES.md` stay in sync with the canon. Silent: prints nothing and exits 0, never re-invoking the session. |
| `event_action_launcher.py` | invoked by the queue watcher on a queue event (UI plan change request); `--dry-run` default until the watcher lands. Takes a single-instance coalescing guard at `<logs>/event-launcher.lock` (pid plus start) before anything else, so simultaneous invocations collapse to one run — a live holder turns every later arrival into one `coalesce-defer` line at exit 0, a stale guard (dead pid, or age past the coalescing span) is displaced with a printed note, and the guard releases from a `finally` on every exit path; settles on 5 minutes of mtime quiet across `<user-queue>`, `<machine-queue>`, and the frontmatter-less `<inbox>`-root drops, bounded by the 15-minute coalescing span; reads the run lease under § Concurrency rule 1 and defers to a fresh holder without taking it; spawns `claude -p` against the deployed action-agent SKILL with `--output-format json`, `--permission-mode`, `--max-budget-usd`, and `CREATE_NO_WINDOW` so no console flashes; appends one `event-launch` line to `<logs>/action-agent/action-agent.md` only when `--spawn` actually launches |
