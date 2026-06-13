"""
scaffold_vault.py
======================

Creates a clean, self-consistent vault from this kit's own CONFIG.md.

Produces:
  - Folder structure declared in CONFIG.md § Folders (wildcard rows skipped),
    plus the on-demand subfolders the skills write into first
    (<inbox-assets> and <logs>, both resolved from the CONFIG token table).
  - The two queue files, seeded with worked example items a new user can check
    off or delete: <machine-queue> (<outbox>/Machine-Queue.md, a user->AI
    checklist) and <user-queue> (<inbox>/User-Queue.md, one example proposal
    in the canonical proposal shape).
  - Optionally (--with-ui-plugin <dir>): installs the note-kit-ui Obsidian
    plugin (main.js / manifest.json / styles.css copied into
    <vault>/.obsidian/plugins/note-kit-ui/), MERGES "note-kit-ui" into
    .obsidian/community-plugins.json (created if absent; other entries are
    never touched), and installs the sibling Note-Kit theme
    (<kit>/theme/Note-Kit -> <vault>/.obsidian/themes/Note-Kit/, selected via
    cssTheme in appearance.json when one is freshly written).
  - A full install of the kit at <scaffold>/.claude/ (hooks, skills,
    scheduled-tasks, scripts, CONFIG.md, CLAUDE.md, AGENTS.md, RULES.md) — the
    location Claude Code auto-loads CLAUDE.md from, vault-wide. The whole kit
    lives under .claude/, so the canon files ride along inside the copytree.
  - A sandbox marker prepended to the installed .claude/CLAUDE.md.
  - A .claude/settings.json wiring the three hooks (UserPromptSubmit,
    SessionStart, Stop) so a fresh install has active hooks without hand-paste.
  - A <vault>/.mcp.json registering the vault-search daemon as the `vault`
    HTTP MCP server, so mcp__vault__vault_search is available once the daemon
    is installed + running and the server is approved in Claude Code.
  - A sync-config pass (--vault-root) populating .claude/CLAUDE.md §
    Session-start defaults and .claude/AGENTS.md.

Modes:
  (default)   Create a fresh vault: folders + full .claude/ install + the files
              above. Refuses to overwrite a *separate* existing .claude/ (use
              --upgrade for that).
              IN-PLACE: when the target's .claude/ IS this kit's own home — the
              repo is checked out at the vault root, so .claude/ is already
              present — the kit copy is skipped and the folders + config files
              are scaffolded *around* the existing kit. Existing settings.json /
              .mcp.json are preserved, never clobbered.
  --upgrade   Refresh an EXISTING install's executable code only. Re-copies
              scripts/, skills/, scheduled-tasks/, and hooks/ from this kit into
              <vault>/.claude/, and PRESERVES the user-owned files untouched:
              CONFIG.md, CLAUDE.md, AGENTS.md, RULES.md, settings.json,
              .mcp.json, and all vault content. Then re-runs sync-config.
              Orientation/rule changes in a new kit version are NOT auto-merged
              — re-review the preserved files against the new manifest.
              In-place (target's .claude/ is this kit's home) there is nothing
              to re-copy — the code already IS the live code — so it only
              re-runs sync-config.

Usage:
  python scripts/scaffold_vault.py [--path <dir>] [--clean]
  python scripts/scaffold_vault.py --path <vault>   # in-place ok: kit at the vault root
  python scripts/scaffold_vault.py --path <vault> --with-ui-plugin <plugin-dir>
  python scripts/scaffold_vault.py --upgrade <vault>
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_KIT_ROOT = _SCRIPTS_DIR.parent

sys.path.insert(0, str(_SCRIPTS_DIR))
from config_variables import (  # noqa: E402
    FOLDER_ROUTING,
    SUBFOLDERS,
    _folder_by_semantic,
    token_path,
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------



def _under_tempdir(path: Path) -> bool:
    """True if `path` lives under the system temp directory."""
    try:
        tmp = Path(tempfile.gettempdir()).resolve()
        return tmp == path or tmp in path.parents
    except Exception:
        return False


def _is_in_place(kit_dir: Path) -> bool:
    """True when <vault>/.claude/ IS this kit's own home.

    This is the case when the kit is checked out *at the vault root* — i.e. the
    repo you cloned is itself the Obsidian vault, so `.claude/` is already in
    place. Installing then means scaffolding the folders and config files
    *around* the existing kit; the kit must NOT be copied onto itself. That
    self-copy is a no-op everywhere and, on Windows, fails outright with a
    sharing violation (WinError 32) because the running interpreter holds its
    own source files open.
    """
    try:
        return kit_dir.resolve() == _KIT_ROOT.resolve()
    except OSError:
        return False


# Executable-code subtrees refreshed by --upgrade. User-owned files
# (CONFIG.md, CLAUDE.md, AGENTS.md, RULES.md, settings.json inside .claude/, the
# root-level .mcp.json) and all vault content are NOT in this list and are left
# untouched.
_UPGRADE_CODE_DIRS = ("scripts", "skills", "scheduled-tasks", "hooks", "templates")
# Preserved files that live inside <vault>/.claude/.
_UPGRADE_PRESERVED_CLAUDE = (
    "CONFIG.md", "CLAUDE.md", "AGENTS.md", "RULES.md", "settings.json",
)
# Preserved files that live at the vault root (outside .claude/).
_UPGRADE_PRESERVED_ROOT = (".mcp.json",)


def _run_upgrade(vault_root: Path) -> int:
    """Refresh an existing install's executable code only; preserve user files.

    Re-copies the kit's executable subtrees into <vault>/.claude/, leaves the
    user-owned canon files and all vault content untouched, then re-runs
    sync-config against the vault. Prints a refreshed-vs-preserved report.
    """
    vault_root = vault_root.resolve()
    kit_dir = vault_root / ".claude"
    if not kit_dir.is_dir():
        print(
            f"[upgrade] No existing install at {kit_dir}. "
            "--upgrade refreshes an installed vault; run without --upgrade to "
            "create a fresh one.",
            file=sys.stderr,
        )
        return 2

    in_place = _is_in_place(kit_dir)
    refreshed: list[str] = []
    if in_place:
        print(
            f"[upgrade] In-place install: {kit_dir} is this kit's own home "
            "(kit checked out at the vault root). The executable code IS the "
            "live code — nothing to re-copy. Re-running config sync only."
        )
    else:
        for sub in _UPGRADE_CODE_DIRS:
            src = _KIT_ROOT / sub
            if not src.is_dir():
                continue
            dst = kit_dir / sub
            shutil.copytree(src, dst, dirs_exist_ok=True)
            refreshed.append(f".claude/{sub}/")

    preserved = [
        f".claude/{name}" for name in _UPGRADE_PRESERVED_CLAUDE if (kit_dir / name).exists()
    ]
    preserved += [
        name for name in _UPGRADE_PRESERVED_ROOT if (vault_root / name).exists()
    ]

    print(f"[upgrade] Vault: {vault_root}")
    print("[upgrade] Refreshed (executable code re-copied from kit):")
    for r in refreshed:
        print(f"  + {r}")
    print("[upgrade] Preserved (user-owned, left untouched):")
    for p in preserved:
        print(f"  = {p}")
    print("  = all vault content")

    # Re-sync the orientation tables from the vault's own (preserved) CONFIG.md.
    sync_script = kit_dir / "scripts" / "sync_config.py"
    if sync_script.exists():
        result = subprocess.run(
            [sys.executable, str(sync_script), "--vault-root", str(vault_root)],
            cwd=str(vault_root),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[upgrade] WARNING: sync_config exited {result.returncode}.")
            if result.stderr:
                print(result.stderr[:500])
        else:
            print("[upgrade] sync_config ran; .claude/CLAUDE.md and .claude/AGENTS.md re-synced.")
    else:
        print(f"[upgrade] WARNING: sync_config not found at {sync_script}. Skipping re-sync.")

    print(
        "\n[upgrade] Note: orientation/rule changes in this kit version are NOT "
        "auto-merged. Re-review the preserved files against the new manifest."
    )
    return 0


# --upgrade short-circuits the fresh-vault flow: refresh code, re-sync, exit.


def main() -> None:
    """Entry point — runs only when invoked as a script, never on import."""
    parser = argparse.ArgumentParser(description="Scaffold a vault from CONFIG.md (real install, in-place, or --clean sandbox).")
    parser.add_argument(
        "--path",
        type=Path,
        default=None,
        help="Output directory. Created if absent. Defaults to a temp directory.",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Delete the vault after creation. For automated smoke tests.",
    )
    parser.add_argument(
        "--upgrade",
        type=Path,
        default=None,
        metavar="VAULT",
        help="Refresh an existing install's executable code (scripts/, skills/, "
             "scheduled-tasks/, hooks/) from this kit, preserving user-owned files "
             "(CONFIG.md, CLAUDE.md, AGENTS.md, RULES.md, settings.json, .mcp.json, "
             "and vault content). Then re-runs sync-config.",
    )
    parser.add_argument(
        "--with-ui-plugin",
        type=Path,
        default=None,
        metavar="PLUGIN_DIR",
        help="Optional. Directory holding the note-kit-ui Obsidian plugin files "
             "(main.js, manifest.json, styles.css). They are copied into "
             "<vault>/.obsidian/plugins/note-kit-ui/ and 'note-kit-ui' is merged "
             "into .obsidian/community-plugins.json (created if absent; existing "
             "entries are never removed). The sibling Note-Kit theme "
             "(<kit>/theme/Note-Kit) is installed in the same pass.",
    )
    args = parser.parse_args()
    if args.upgrade is not None:
        sys.exit(_run_upgrade(args.upgrade))


    # --clean deletes the vault tree after creation. Refuse — before creating
    # anything — on a --path that is not under the system temp directory: that is
    # almost certainly a real vault, not a throwaway. The default (no --path) is an
    # auto-created tempdir and always passes this guard.
    if args.clean and args.path is not None and not _under_tempdir(args.path.resolve()):
        print(
            "[scaffold] REFUSING --clean: "
            f"{args.path.resolve()} is not under the system temp dir "
            f"({Path(tempfile.gettempdir()).resolve()}). "
            "--clean is for disposable smoke-test vaults only. "
            "Delete a real vault by hand if that is the intent.",
            file=sys.stderr,
        )
        sys.exit(2)

    vault: Path = args.path or Path(tempfile.mkdtemp(prefix="note-kit-scaffold-"))
    vault.mkdir(parents=True, exist_ok=True)

    # Beginner footgun: no --path means a DISPOSABLE sandbox in a temp folder, not
    # the user's real vault. Say so loudly and show the real-install command, then
    # continue (the sandbox is still useful for eyeballing the structure). Callers
    # that pass --path or --clean never see this.
    if args.path is None and not args.clean:
        print(
            f"[scaffold] NOTE: no --path given — installing into a DISPOSABLE sandbox "
            f"vault in a temp folder:\n    {vault}\n"
            "This is NOT your real vault and is easy to lose track of. To install "
            "into your real vault instead, re-run with:\n"
            f"    python {Path(__file__).name} --path <your-vault-path>",
            file=sys.stderr,
        )

    # Is the kit checked out AT this vault root? Then <vault>/.claude/ is already
    # this kit's own home: the copy in step 2 is skipped (it would be a self-copy)
    # and the folders + config files are scaffolded around the existing kit.
    kit_dir = vault / ".claude"
    in_place = _is_in_place(kit_dir)

    # Refuse to clobber a *separate* existing install. A populated <vault>/.claude/
    # that is NOT this kit's own home is an already-installed vault; re-running the
    # fresh flow would overwrite user-owned canon (CONFIG.md, settings.json,
    # .mcp.json). Use --upgrade to refresh code only. The in-place case (.claude/
    # IS this kit) is expected and allowed — step 2 skips the kit copy and the
    # settings.json / .mcp.json writes below preserve any existing file. --clean
    # (disposable temp vault) is exempt — its tree is created and deleted in-run.
    if not args.clean and kit_dir.is_dir() and not in_place:
        print(
            f"[scaffold] REFUSING: {kit_dir} already exists — this looks "
            "like an installed vault. Re-running the fresh scaffold would overwrite "
            "user-owned files (CONFIG.md, settings.json, .mcp.json). "
            f"To refresh executable code only, run: "
            f"python {Path(__file__).name} --upgrade {vault}",
            file=sys.stderr,
        )
        sys.exit(2)


    # ---------------------------------------------------------------------------
    # 1. Folder structure from FOLDER_ROUTING
    # ---------------------------------------------------------------------------

    for folder_path in FOLDER_ROUTING:
        if "*" not in folder_path:
            (vault / folder_path).mkdir(parents=True, exist_ok=True)

    print(f"[scaffold] Created {sum(1 for f in FOLDER_ROUTING if '*' not in f)} folders.")

    # On-demand inbox/archive subfolders the skills and agents write into before
    # anything else creates them — so a first asset or log write does not land in
    # a missing directory. Both paths come from the CONFIG § Folders token table
    # (<inbox-assets>, <logs>). (Checkpoints and the 00-Actions drop folder are
    # retired surfaces: the outbox owns drops, and resume state lives in working
    # sets.)
    _on_demand_dirs = [
        vault / token_path("inbox-assets"),
        vault / token_path("logs"),
    ]
    for d in _on_demand_dirs:
        d.mkdir(parents=True, exist_ok=True)
    print(f"[scaffold] Created {len(_on_demand_dirs)} on-demand inbox/archive subfolders.")

    # § Subfolders (Sessions, Research, Plans, Notes, etc.) are
    # parent-scoped: they live under a project or area, not at the vault root.
    # A fresh vault has no parent yet, so seeding them globally would produce
    # dangling orphan folders that do not belong to any declared parent. The
    # correct approach is on-first-use creation: the filing-agent proposes a
    # "Create folder" action (CONFIG § Actions) the first time a typed note is
    # filed into a new parent, and the action-agent executes it. This mirrors
    # how the existing on-demand dirs above are handled and requires no
    # pre-seeding here.


    # ---------------------------------------------------------------------------
    # 2. Install the kit into <scaffold>/.claude/
    #    This is the install location Claude Code reads from: rules/, hooks/,
    #    skills/, scheduled-tasks/, scripts/, plus CONFIG.md and the orientation
    #    pair CLAUDE.md / AGENTS.md. Claude Code auto-loads CLAUDE.md from .claude/
    #    vault-wide, so the orientation file must live here (not the vault root,
    #    not a project subfolder).
    # ---------------------------------------------------------------------------

    # kit_dir / in_place were resolved right after vault.mkdir above.
    if in_place:
        print(
            f"[scaffold] In-place install: kit already present at "
            f"{kit_dir.relative_to(vault)}/ (checked out at the vault root) — "
            "skipping kit copy."
        )
    else:
        shutil.copytree(_KIT_ROOT, kit_dir, dirs_exist_ok=True)
        print(f"[scaffold] Kit installed to {kit_dir.relative_to(vault)}/")


    # ---------------------------------------------------------------------------
    # 3. Mark the installed .claude/CLAUDE.md as a scaffolded sandbox.
    #    The kit's real CLAUDE.md was installed at .claude/CLAUDE.md by step 2 and
    #    is the orientation file Claude Code loads — do not overwrite it. Prepend a
    #    non-destructive sandbox blockquote so the vault is recognizable as a
    #    throwaway without losing the orientation content sync-config edits below.
    #
    #    The marker is gated on --clean: only a disposable test vault (deleted at
    #    the end of this run) gets stamped "Not a real install. Safe to delete." A
    #    --path real install must NOT carry that warning, because it is a real
    #    install and the line would be false and self-deleting advice.
    # ---------------------------------------------------------------------------

    installed_claude = kit_dir / "CLAUDE.md"
    _SANDBOX_MARKER = (
        "> **Scaffolded test vault** — generated by "
        "`scripts/scaffold_vault.py`. Not a real install. Safe to delete.\n\n"
    )
    if installed_claude.exists():
        if args.clean:
            existing = installed_claude.read_text(encoding="utf-8")
            if "Scaffolded test vault" not in existing:
                installed_claude.write_text(_SANDBOX_MARKER + existing, encoding="utf-8")
            print(f"[scaffold] Sandbox marker added to {installed_claude.relative_to(vault)}.")
        else:
            print(
                f"[scaffold] Real install (no --clean): left "
                f"{installed_claude.relative_to(vault)} unmarked."
            )
    else:
        # No CLAUDE.md shipped in the kit — write a minimal orientation file so the
        # .claude/ install still has one for sync-config to populate. The sandbox
        # marker rides along only under --clean (disposable vault).
        marker = _SANDBOX_MARKER if args.clean else ""
        installed_claude.write_text(
            "# Vault — CLAUDE.md\n\n"
            + marker
            + "## Vault structure\n\n"
            "```\n"
            + "\n".join(f"{fp}/" for fp in FOLDER_ROUTING if "*" not in fp)
            + "\n```\n\n"
            "## Kit location\n\n"
            f"Rules, scripts, and scheduled-task SKILLs are installed at `.claude/`.\n",
            encoding="utf-8",
        )
        print(f"[scaffold] Minimal {installed_claude.relative_to(vault)} written (kit shipped none).")


    # ---------------------------------------------------------------------------
    # 4. .claude/settings.json — wire the three hooks
    #    A fresh install needs active hooks without hand-pasting JSON. The commands
    #    are written relative to the vault root (./.claude/hooks/...), matching the
    #    main README; Claude Code runs hooks with the vault as the working dir.
    # ---------------------------------------------------------------------------

    settings_path = kit_dir / "settings.json"
    _HOOKS = {
        "hooks": {
            "UserPromptSubmit": [
                {"type": "command", "command": "python ./.claude/hooks/load-rules.py"}
            ],
            "SessionStart": [
                {"type": "command", "command": "python ./.claude/hooks/session-start-context.py"}
            ],
            "Stop": [
                {"type": "command", "command": "python ./.claude/hooks/session-end-audit.py"}
            ],
        }
    }
    if settings_path.exists():
        print(
            f"[scaffold] settings.json already present at "
            f"{settings_path.relative_to(vault)} — left as-is (not overwritten). "
            "If hooks misbehave, compare it against README Part 4."
        )
    else:
        settings_path.write_text(json.dumps(_HOOKS, indent=2) + "\n", encoding="utf-8")
        print(f"[scaffold] settings.json written to {settings_path.relative_to(vault)} (3 hooks wired).")


    # ---------------------------------------------------------------------------
    # 4b. <vault>/.mcp.json — register the vault-search daemon as the `vault`
    #     HTTP MCP server. This is the retrieval spine: mcp__vault__vault_search
    #     only exists when this file registers the server AND the daemon is
    #     installed + running AND the user approves the `vault` server when Claude
    #     Code prompts. Until then, skills/agents fall back to Glob/Grep. The block
    #     is templated verbatim from the working live config.
    # ---------------------------------------------------------------------------

    mcp_path = vault / ".mcp.json"
    _MCP = {
        "mcpServers": {
            "vault": {
                "type": "http",
                "url": "http://127.0.0.1:8765/mcp",
            }
        }
    }
    if mcp_path.exists():
        print(
            f"[scaffold] .mcp.json already present at "
            f"{mcp_path.relative_to(vault)} — left as-is (not overwritten). "
            "If search is missing, compare it against README Part 5."
        )
    else:
        mcp_path.write_text(json.dumps(_MCP, indent=2) + "\n", encoding="utf-8")
        print(f"[scaffold] .mcp.json written to {mcp_path.relative_to(vault)} (vault MCP server registered).")


    # ---------------------------------------------------------------------------
    # 5. Seed the two queue files with worked example items (CONFIG § Queue
    #     protocol): <machine-queue> (<outbox>/Machine-Queue.md — the user
    #     writes a checklist; the AI acts on it) and <user-queue>
    #     (<inbox>/User-Queue.md — the AI writes proposals; the user checks
    #     them off). Both paths resolve from the CONFIG token table. The
    #     examples are clearly marked so a new user can learn the loop by
    #     watching it run, then check them off or delete them. Existing queue
    #     files (in-place / re-run installs) are never clobbered.
    # ---------------------------------------------------------------------------

    inbox_folder = _folder_by_semantic("inbox")
    inbox_path = vault / inbox_folder
    inbox_path.mkdir(parents=True, exist_ok=True)

    outbox_folder = _folder_by_semantic("outbox")
    outbox_path = vault / outbox_folder
    outbox_path.mkdir(parents=True, exist_ok=True)
    _today = date.today().isoformat()

    machine_queue = vault / token_path("machine-queue")
    machine_queue.parent.mkdir(parents=True, exist_ok=True)
    if machine_queue.exists():
        print(f"[scaffold] {machine_queue.name} already present — left as-is.")
    else:
        machine_queue.write_text(
            "# Machine Queue\n\n"
            "You write checklist items here; the AI acts on them "
            "(CONFIG § Queue protocol).\n\n"
            "## Example items\n\n"
            "These are worked examples to try the system with — run one, or "
            "delete them all and write your own:\n\n"
            "- [ ] research the best way to organize a recipe collection\n"
            "- [ ] make a note summarizing how this vault's folders are organized\n"
            "- [ ] plan a small first project so I can watch the project workflow run\n",
            encoding="utf-8",
        )
        print(f"[scaffold] {machine_queue.name} seeded with example items in {outbox_folder}/.")

    _notes_sub = next(
        (row.subfolder for row in SUBFOLDERS.values()
         if "note" in getattr(row, "type_defaults", ())),
        None,
    )
    _example_note_home = (
        f"{_folder_by_semantic('areas')}/{_notes_sub}" if _notes_sub
        else _folder_by_semantic('areas')
    )

    user_queue = vault / token_path("user-queue")
    user_queue.parent.mkdir(parents=True, exist_ok=True)
    if user_queue.exists():
        print(f"[scaffold] {user_queue.name} already present — left as-is.")
    else:
        user_queue.write_text(
            "# User Queue\n\n"
            "The AI writes proposals here; you resolve each by checkbox "
            "(CONFIG § Queue protocol; item shape per Format-User-Queue). "
            "Every item is a `###` heading with at least one `- [ ]` option "
            "beneath it — the note-kit-ui Decide bucket surfaces only that "
            "shape.\n\n"
            "## scaffold — example proposals\n\n"
            "Worked examples in the canonical proposal shape — check an option "
            "to see the loop run, or delete them:\n\n"
            "### Welcome-Note.md needs a home (example)\n\n"
            "An inbox draft needs a home; choose where it files.\n\n"
            f"- [ ] file it as a note under `{_example_note_home}/Welcome-Note.md`\n"
            f"- [ ] keep it in `{inbox_folder}/` for now\n"
            "- [ ] delete this item — it is only an example\n\n"
            f"_proposed: {_today} by scaffold (example)_\n\n"
            "### Unlisted tag `recipes` (example)\n\n"
            "A tag appeared that CONFIG § Tags does not list; decide its fate.\n\n"
            "- [ ] add `recipes` to CONFIG § Tags as a domain tag\n"
            "- [ ] delete this item — it is only an example\n\n"
            f"_proposed: {_today} by scaffold (example)_\n",
            encoding="utf-8",
        )
        print(f"[scaffold] {user_queue.name} seeded with example proposals in "
              f"{inbox_folder}/.")


    # ---------------------------------------------------------------------------
    # 5b2. Seed the starter format notes: the Format-<Type> shapes producers
    #      consult before authoring (CONFIG § Format notes). Copied from
    #      .claude/templates/format/ into <areas>/<format-subfolder>/; an
    #      existing file is never overwritten, so user-edited shapes survive
    #      re-runs and upgrades.
    # ---------------------------------------------------------------------------

    templates_dir = _KIT_ROOT / "templates" / "format"
    fmt_sub = next(
        (row.subfolder for row in SUBFOLDERS.values()
         if "format" in getattr(row, "type_defaults", ())),
        None,
    )
    if templates_dir.is_dir() and fmt_sub is None:
        print("[scaffold] WARNING: CONFIG § Subfolders has no format row — format notes not seeded. "
              "Add the row and re-run, or copy .claude/templates/format/ by hand.")
    if templates_dir.is_dir() and fmt_sub is not None:
        areas_folder = _folder_by_semantic("areas")
        fmt_dest = vault / areas_folder / fmt_sub
        fmt_dest.mkdir(parents=True, exist_ok=True)
        _fmt_seeded = 0
        for _tpl in sorted(templates_dir.glob("*.md")):
            _tpl_dest = fmt_dest / _tpl.name
            if not _tpl_dest.exists():
                shutil.copy2(_tpl, _tpl_dest)
                _fmt_seeded += 1
        print(f"[scaffold] {_fmt_seeded} format notes seeded to "
              f"{areas_folder}/{fmt_sub}/ (existing format notes preserved).")

    # ---------------------------------------------------------------------------
    # 5c. Optional Obsidian UI plugin install (--with-ui-plugin <dir>): copy the
    #     plugin files into <vault>/.obsidian/plugins/note-kit-ui/ and MERGE
    #     "note-kit-ui" into .obsidian/community-plugins.json. The JSON is a
    #     plain array of plugin ids: created when absent, appended when missing
    #     the id, and other entries are never removed. An unparseable existing
    #     file is left untouched (warn, skip) rather than overwritten.
    # ---------------------------------------------------------------------------

    if args.with_ui_plugin is not None:
        plugin_src = args.with_ui_plugin.resolve()
        plugin_files = ("main.js", "manifest.json", "styles.css")
        present = [f for f in plugin_files if (plugin_src / f).is_file()]
        if not plugin_src.is_dir() or not present:
            print(
                f"[scaffold] WARNING: --with-ui-plugin: no plugin files found at "
                f"{plugin_src} (expected any of {', '.join(plugin_files)}). "
                "Skipping plugin install.",
                file=sys.stderr,
            )
        else:
            plugin_dest = vault / ".obsidian" / "plugins" / "note-kit-ui"
            plugin_dest.mkdir(parents=True, exist_ok=True)
            for fname in present:
                shutil.copy2(plugin_src / fname, plugin_dest / fname)
            print(
                f"[scaffold] note-kit-ui plugin installed "
                f"({', '.join(present)}) -> .obsidian/plugins/note-kit-ui/."
            )

            cp_path = vault / ".obsidian" / "community-plugins.json"
            entries: list | None = []
            if cp_path.exists():
                try:
                    loaded = json.loads(cp_path.read_text(encoding="utf-8"))
                    entries = loaded if isinstance(loaded, list) else None
                except Exception:
                    entries = None
            if entries is None:
                print(
                    f"[scaffold] WARNING: {cp_path} exists but is not a JSON "
                    "array — left untouched. Enable 'note-kit-ui' in Obsidian "
                    "by hand.",
                    file=sys.stderr,
                )
            elif "note-kit-ui" in entries:
                print("[scaffold] community-plugins.json already lists note-kit-ui.")
            else:
                entries.append("note-kit-ui")
                cp_path.write_text(
                    json.dumps(entries, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    "[scaffold] 'note-kit-ui' merged into "
                    ".obsidian/community-plugins.json "
                    f"({len(entries)} entr{'y' if len(entries) == 1 else 'ies'} total)."
                )

            # The UI ships as plugin + theme; the Note-Kit theme sits beside the
            # plugin in the kit (<kit>/theme/Note-Kit). Derive it from the plugin
            # dir and install it in the same pass. Absent -> plugin-only, note it.
            installed_theme_name = None
            theme_src = plugin_src.parent.parent / "theme" / "Note-Kit"
            theme_present = [
                f for f in ("theme.css", "manifest.json")
                if (theme_src / f).is_file()
            ]
            if "theme.css" not in theme_present:
                print(
                    f"[scaffold] note: no theme at {theme_src}; installed the "
                    "plugin without the Note-Kit theme.",
                    file=sys.stderr,
                )
            else:
                theme_name = theme_src.name
                try:
                    manifest = json.loads(
                        (theme_src / "manifest.json").read_text(encoding="utf-8")
                    )
                    if (
                        isinstance(manifest, dict)
                        and isinstance(manifest.get("name"), str)
                        and manifest["name"].strip()
                    ):
                        theme_name = manifest["name"].strip()
                except Exception:
                    pass
                theme_dest = vault / ".obsidian" / "themes" / theme_name
                theme_dest.mkdir(parents=True, exist_ok=True)
                for fname in theme_present:
                    shutil.copy2(theme_src / fname, theme_dest / fname)
                installed_theme_name = theme_name
                print(
                    f"[scaffold] '{theme_name}' theme installed "
                    f"({', '.join(theme_present)}) -> .obsidian/themes/{theme_name}/."
                )

            # The kit's calm appearance ships with the UI: no ribbon, no view
            # header, compact base type, and the Note-Kit theme when it installed.
            # Written only when no appearance.json exists — an existing vault's
            # appearance is the user's, untouched.
            ap_path = vault / ".obsidian" / "appearance.json"
            if not ap_path.exists():
                appearance = {
                    "theme": "system",
                    "showRibbon": False,
                    "showViewHeader": False,
                    "nativeMenus": False,
                    "baseFontSize": 12,
                    "baseFontSizeAction": False,
                    "accentColor": "",
                }
                if installed_theme_name:
                    appearance["cssTheme"] = installed_theme_name
                ap_path.write_text(
                    json.dumps(appearance, indent=2) + "\n", encoding="utf-8"
                )
                print(
                    "[scaffold] calm appearance defaults written "
                    "(.obsidian/appearance.json)"
                    + (
                        f", theme '{installed_theme_name}' selected."
                        if installed_theme_name
                        else "."
                    )
                )
            elif installed_theme_name:
                print(
                    "[scaffold] existing appearance.json preserved — select "
                    f"'{installed_theme_name}' in Settings -> Appearance -> Themes."
                )
            else:
                print("[scaffold] existing appearance.json preserved.")


    # ---------------------------------------------------------------------------
    # 6. Run sync-config against the install so .claude/CLAUDE.md and
    #    .claude/AGENTS.md get their § Session-start defaults tables. --vault-root
    #    points sync-config at the installed .claude/ pair (and the vault's archive
    #    for the sync log) rather than the kit-source dev location.
    # ---------------------------------------------------------------------------

    sync_script = kit_dir / "scripts" / "sync_config.py"
    if sync_script.exists():
        result = subprocess.run(
            [sys.executable, str(sync_script), "--vault-root", str(vault)],
            cwd=str(vault),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"[scaffold] WARNING: sync_config exited {result.returncode}.")
            if result.stderr:
                print(result.stderr[:500])
        else:
            print("[scaffold] sync_config ran; .claude/CLAUDE.md § Session-start defaults and .claude/AGENTS.md populated.")
    else:
        print(f"[scaffold] WARNING: sync_config not found at {sync_script}. Skipping.")


    # ---------------------------------------------------------------------------
    # 7. Output / cleanup
    # ---------------------------------------------------------------------------

    print(f"\nScaffold vault: {vault}")

    # Daemon next-steps. The kit ships the vault-search daemon under
    # .claude/vault-search/ (the copytree above already installed it). It is not
    # runnable until its venv + deps + config exist, which install_daemon.py sets
    # up. We point the user there but never auto-run it: that installer creates a
    # venv and downloads a model, and the daemon binds port 8765 — both are
    # deliberate steps, not something a scaffold should do silently. Skipped for
    # --clean (the vault is about to be deleted).
    if not args.clean:
        daemon_installer = kit_dir / "vault-search" / "install_daemon.py"
        if daemon_installer.exists():
            print()
            print("Search service (optional): finish setup by running the daemon installer")
            print("from the vault root — it creates a venv, installs deps, and writes config:")
            print()
            print(f"  python {Path('.claude') / 'vault-search' / 'install_daemon.py'}")
            print()
            print("It prints the command to start the daemon and a health check. The kit")
            print("works without it (skills fall back to text search); see README Part 5.")

    if args.clean:
        shutil.rmtree(vault)
        print("Cleaned up.")



if __name__ == "__main__":
    main()
