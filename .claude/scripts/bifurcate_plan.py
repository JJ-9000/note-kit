#!/usr/bin/env python3
"""
bifurcate_plan.py
======================

Split a managed plan at session end. The deterministic half of the
plan-bifurcation idea ([[Plan-Bifurcation-At-Session-End]]): a checked box is
done, an unchecked one is not, so the split needs no judgment and lives in code,
not in instructional prose that would rot.

What it does, given one filed plan:

    1. Move every completed (`- [x]`) item to the plan's changelog,
       `<slug>-Changelog.md` beside the plan, under a dated heading and
       grouped by the section each item came from. The changelog is created
       with frontmatter on first use.
    2. Write the open remainder (`- [ ]` items + structure) to `<inbox>` as a
       fresh `reviewed: false` draft, so the person re-approves the trimmed plan
       and the filing agent re-files it. This is the "raise to inbox" step.
    3. Archive the original filed plan first — copy, hash-verify, then delete —
       before the changelog and raised draft land, so a copy that fails to
       verify loses nothing (source kept, no partial changelog/draft/archive).

Stay out of `<inbox>`
---------------------
The inbox is the person's review space, full of in-flight drafts. This script
never selects, reads-for-processing, or edits a file that already lives there:
a plan whose path is under `<inbox>` is refused (`skipped-inbox`). Its only
write into the inbox is *creating* the one trimmed-plan draft, and it refuses
rather than clobber an existing file of that name. Nothing else touches the
inbox; the changelog lands beside the plan and the archive lands under
`<archive>`.

Stay out of dot-directories
---------------------------
Discovery never descends into a dot-directory (CONFIG § Folders / Scan
exclusions — the kit's own `.claude/` install dir, `.obsidian`, `.trash`, and
any future dot-directory at any depth). Those hold tooling/config and
operational documents, never vault content; a `type: plan` file under one (a
kit plan, for instance) must never be bifurcated.

Folder names (`<inbox>`, `<archive>`) are resolved from CONFIG.md via
config_variables, never hard-coded.

Public API
----------
    bifurcate_plan(plan_path, vault_root, *, now=None, dry_run=False)
        -> BifurcateResult
    discover_filed_plans(vault_root) -> list[Path]
        Every `type: plan` note outside `<inbox>`, `<archive>`, and every
        dot-directory.

Run with no arguments to execute the self-tests against a temp fixture:
    python scripts/bifurcate_plan.py
Run with a plan path to operate (dry-run unless --apply):
    python scripts/bifurcate_plan.py <plan.md> [--apply]
"""
from __future__ import annotations

import hashlib
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import (  # noqa: E402
    FILE_HANDLING,
    _folder_by_semantic,
    folder_for_wildcard,
    is_excluded_dir,
    resolve_vault_root,
)
from frontmatter_helpers import (  # noqa: E402
    read_text_or_none,
    read_text_strict,
    split_frontmatter,
    write_text,
)


# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

_CHECK_DONE = re.compile(r"^\s*[-*] \[[xX]\]\s")
_CHECK_OPEN = re.compile(r"^\s*[-*] \[ \]\s")
_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class BifurcateResult:
    """Outcome of one bifurcation.

    status:
        "bifurcated"        — split ran; changelog, inbox draft, archive written.
        "skipped-inbox"     — the plan lives under <inbox>; refused by design.
        "skipped-not-plan"  — the file is not `type: plan`.
        "noop-nothing-done" — no completed items, nothing to split.
        "aborted"           — a guard fired (e.g. inbox draft would clobber).
    """

    status: str
    plan: Path
    changelog: Path | None = None
    inbox_draft: Path | None = None
    archived: Path | None = None
    completed_count: int = 0
    open_count: int = 0
    error: str | None = None


# ---------------------------------------------------------------------------
# Body / copy helpers  (frontmatter parse/split/edit is frontmatter_helpers)
# ---------------------------------------------------------------------------

def _sha256(path: Path) -> str:
    """Content hash of a file — the copy-before-delete verify."""
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _collapse_blank_runs(text: str) -> str:
    """Collapse 3+ consecutive blank lines (left by removed items) to 2."""
    return re.sub(r"\n{4,}", "\n\n\n", text)


# ---------------------------------------------------------------------------
# Inbox / archive location
# ---------------------------------------------------------------------------

def _inbox_dir(vault_root: Path) -> Path:
    name = folder_for_wildcard("<inbox>") or _folder_by_semantic("inbox")
    return (vault_root / name).resolve()


def _archive_dir(vault_root: Path) -> Path:
    return (vault_root / _folder_by_semantic("archive")).resolve()


def _is_under(path: Path, directory: Path) -> bool:
    path = path.resolve()
    directory = directory.resolve()
    return path == directory or directory in path.parents


def _in_excluded_dir(path: Path) -> bool:
    """True if any path segment is a dot-directory (CONFIG § Scan exclusions).

    Covers the kit's own `.claude/`, `.obsidian`, `.trash`, and any future
    dot-directory at any depth — tooling/config, never vault content.
    """
    return any(is_excluded_dir(part) for part in path.parts)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def _classify_body(body: str) -> tuple[list[tuple[str, str]], list[str], int]:
    """Walk the body once.

    Returns:
        completed   — list of (section_heading, raw_line) for each `[x]` item.
        kept_lines  — the body lines with every `[x]` line removed.
        open_count  — number of `[ ]` items remaining.
    """
    completed: list[tuple[str, str]] = []
    kept: list[str] = []
    open_count = 0
    current_section = ""
    for line in body.split("\n"):
        heading = _HEADING.match(line)
        if heading:
            current_section = heading.group(2).strip()
            kept.append(line)
            continue
        if _CHECK_DONE.match(line):
            completed.append((current_section, line.rstrip()))
            continue  # dropped from the trimmed plan
        if _CHECK_OPEN.match(line):
            open_count += 1
        kept.append(line)
    return completed, kept, open_count


def _render_changelog_entry(
    completed: list[tuple[str, str]], plan_stem: str, date_str: str
) -> str:
    """One dated entry: the completed items, grouped by their source section.

    A blank line surrounds every heading, matching [[Format-Changelog]].
    """
    out = ["", f"## {date_str} — from {plan_stem}", ""]
    last_section = None
    for section, line in completed:
        if section != last_section:
            if section:
                out += ["", f"### {section}", ""]
            last_section = section
        out.append(line)
    out.append("")
    text = re.sub(r"\n{3,}", "\n\n", "\n".join(out))
    return text if text.endswith("\n") else text + "\n"


def _new_changelog_text(plan_stem: str, plan_fm: dict, date_str: str) -> str:
    """Frontmatter + intro for a changelog created on first use.

    Tags are carried from the plan (so the changelog stays project-appropriate
    without hard-coding any project), with `plan` dropped and `changelog` added.
    """
    tags = plan_fm.get("tags") or []
    if isinstance(tags, str):
        tags = [tags]
    tags = [t for t in tags if t != "plan"]
    if "changelog" not in tags:
        tags.append("changelog")
    parent = plan_fm.get("parent")

    lines = ["---", "type: note", "tags:"]
    lines += [f"  - {t}" for t in tags]
    lines.append(f"date: {date_str}")
    if parent:
        lines.append(f'parent: "{parent}"')
    lines.append("status: complete")
    lines.append("---")
    lines.append("")
    title = plan_stem.replace("-", " ")
    lines.append(f"# {title} — Changelog")
    lines.append("")
    lines.append(
        f"The completed-items changelog for [[{plan_stem}]]. When that plan "
        f"bifurcates at session end, its checked items split out here under a "
        f"dated heading and the open remainder rises to the inbox for "
        f"re-approval, done by `bifurcate_plan.py`."
    )
    return "\n".join(lines) + "\n"


def bifurcate_plan(
    plan_path: Path,
    vault_root: Path,
    *,
    now: datetime | None = None,
    dry_run: bool = False,
) -> BifurcateResult:
    """Split one filed plan into its changelog + a raised inbox draft.

    Refuses any plan under `<inbox>` (the stay-out-of-inbox guard) or under a
    dot-directory (the scan-exclusion guard).
    """
    plan_path = Path(plan_path)
    vault_root = Path(vault_root)
    date_str = (now or datetime.now()).strftime("%Y-%m-%d")

    if not plan_path.exists():
        return BifurcateResult("aborted", plan_path, error=f"plan not found: {plan_path}")

    # --- Guard: stay out of dot-directories (scan-exclusion) --------------
    if _in_excluded_dir(plan_path):
        return BifurcateResult(
            "skipped-inbox", plan_path,
            error="plan lives under a dot-directory (scan-excluded); never bifurcate kit/config",
        )

    # --- Guard: stay out of the inbox -------------------------------------
    if _is_under(plan_path, _inbox_dir(vault_root)):
        return BifurcateResult(
            "skipped-inbox", plan_path,
            error="plan lives under <inbox>; bifurcate only filed plans",
        )

    text = read_text_strict(plan_path)
    fmobj = split_frontmatter(text)
    fm = fmobj.to_dict()

    if str(fm.get("type", "")).strip().lower() != "plan":
        return BifurcateResult(
            "skipped-not-plan", plan_path,
            error=f"type is {fm.get('type')!r}, not 'plan'",
        )

    completed, kept_lines, open_count = _classify_body(fmobj.body)
    if not completed:
        return BifurcateResult(
            "noop-nothing-done", plan_path, open_count=open_count,
            error="no completed items to split",
        )

    stem = re.sub(r"^\d+-", "", plan_path.stem)  # plans are unprefixed, but be safe
    changelog_path = plan_path.parent / f"{stem}-Changelog.md"
    inbox_draft = _inbox_dir(vault_root) / f"{stem}.md"
    archive_path = _archive_dir(vault_root) / f"{stem}-bifurcated-{date_str}.md"

    # --- Guard: never clobber an existing inbox draft ---------------------
    if inbox_draft.exists():
        return BifurcateResult(
            "aborted", plan_path, changelog=changelog_path,
            completed_count=len(completed), open_count=open_count,
            error=f"inbox draft already exists, refusing to clobber: {inbox_draft}",
        )

    # --- Build the trimmed plan (open items only) -------------------------
    # Set the inbox additions on the raw frontmatter block (targeted line
    # rewrites via the shared helper), keeping key order, comments, and quoting.
    for key, value in FILE_HANDLING.inbox_additions.items():
        fmobj.set_field(key, value)
    trimmed_header = fmobj.opening + fmobj.inner + fmobj.closing
    trimmed_body = _collapse_blank_runs("\n".join(kept_lines))
    trimmed_text = trimmed_header + trimmed_body

    # --- Build the changelog content -------------------------------------
    entry = _render_changelog_entry(completed, stem, date_str)
    if changelog_path.exists():
        changelog_text = read_text_strict(changelog_path).rstrip("\n") + "\n" + entry
    else:
        changelog_text = _new_changelog_text(stem, fm, date_str) + entry

    result = BifurcateResult(
        "bifurcated", plan_path,
        changelog=changelog_path, inbox_draft=inbox_draft, archived=archive_path,
        completed_count=len(completed), open_count=open_count,
    )
    if dry_run:
        return result

    # --- Archive the original FIRST and verify the copy by hash -----------
    # Archive-first, before any other write (the structured_rewrite discipline):
    # a copy that fails to verify leaves NO changelog, NO raised draft, and NO
    # archive residue, and the untouched source lets a clean retry succeed.
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(plan_path, archive_path)
    if not archive_path.is_file() or _sha256(archive_path) != _sha256(plan_path):
        if archive_path.exists():
            archive_path.unlink()  # remove the corrupt copy — no residue
        result.status = "aborted"
        result.error = f"archive copy verify failed; source kept: {archive_path}"
        result.changelog = None
        result.inbox_draft = None
        result.archived = None
        return result

    # --- Write changelog + raised draft, then remove the archived original -
    write_text(changelog_path, changelog_text)
    inbox_draft.parent.mkdir(parents=True, exist_ok=True)
    write_text(inbox_draft, trimmed_text)
    plan_path.unlink()
    return result


def discover_filed_plans(vault_root: Path) -> list[Path]:
    """Every `type: plan` note outside `<inbox>`, `<archive>`, and every
    dot-directory.

    Dot-directories (CONFIG § Folders / Scan exclusions — `.claude`, `.obsidian`,
    `.trash`, …) hold tooling/config and operational documents, never vault
    content; a `type: plan` file under one (a kit plan, for instance) is skipped
    so discovery never queues it for bifurcation.
    """
    vault_root = Path(vault_root)
    inbox = _inbox_dir(vault_root)
    archive = _archive_dir(vault_root)
    out: list[Path] = []
    for p in vault_root.rglob("*.md"):
        # Scan-exclusion: never descend through a dot-directory.
        if _in_excluded_dir(p):
            continue
        if _is_under(p, inbox) or _is_under(p, archive):
            continue
        text = read_text_or_none(p)
        if text is None:
            continue  # not valid UTF-8 — skip, never decode lossily
        fm = split_frontmatter(text).to_dict()
        if str(fm.get("type", "")).strip().lower() == "plan":
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _run_self_tests() -> None:
    failures = 0
    fixed_now = datetime(2026, 6, 2, 12, 0, 0)

    def fail(msg: str) -> None:
        nonlocal failures
        print(f"FAIL {msg}")
        failures += 1

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        inbox = vault / (folder_for_wildcard("<inbox>") or "Inbox")
        archive = vault / _folder_by_semantic("archive")
        plans = vault / "Projects" / "Demo" / "Plans"
        for d in (inbox, archive, plans):
            d.mkdir(parents=True, exist_ok=True)

        plan = plans / "Demo-Plan.md"
        plan.write_text(
            "---\n"
            "type: plan\n"
            "tags:\n  - demo\n  - plan\n"
            "date: 2026-06-01\n"
            'parent: "[[00-Demo]]"\n'
            "---\n\n"
            "# Demo Plan\n\n"
            "Intro line.\n\n"
            "## Build\n\n"
            "- [x] First thing landed.\n"
            "- [ ] Second thing still open.\n\n"
            "## Verify\n\n"
            "- [x] A check that passed.\n",
            encoding="utf-8",
        )

        # --- Case A: a clean bifurcation ---------------------------------
        res = bifurcate_plan(plan, vault, now=fixed_now)
        if res.status != "bifurcated":
            fail(f"A status={res.status} error={res.error}")
        if res.completed_count != 2 or res.open_count != 1:
            fail(f"A counts done={res.completed_count} open={res.open_count} (want 2/1)")
        if plan.exists():
            fail("A original plan still in place (should be archived)")
        cl = plans / "Demo-Plan-Changelog.md"
        if not cl.exists():
            fail("A changelog not created")
        else:
            ct = cl.read_text(encoding="utf-8")
            for must in ("type: note", "- changelog", "## 2026-06-02 — from Demo-Plan",
                         "### Build", "### Verify", "First thing landed", "A check that passed"):
                if must not in ct:
                    fail(f"A changelog missing {must!r}")
            if "Second thing still open" in ct:
                fail("A changelog wrongly carried an open item")
            if "- plan" in ct:
                fail("A changelog kept the 'plan' tag")
        draft = inbox / "Demo-Plan.md"
        if not draft.exists():
            fail("A inbox draft not written")
        else:
            dt = draft.read_text(encoding="utf-8")
            if "reviewed: false" not in dt or "status: draft" not in dt:
                fail("A inbox draft missing reviewed/status")
            if "Second thing still open" not in dt:
                fail("A inbox draft lost the open item")
            if "First thing landed" in dt or "A check that passed" in dt:
                fail("A inbox draft kept a completed item")
            if "## Build" not in dt or "Intro line." not in dt:
                fail("A inbox draft dropped structure/prose")
        if not list(archive.glob("Demo-Plan-bifurcated-*.md")):
            fail("A original not archived")
        if res.status == "bifurcated":
            print("OK   case A: clean bifurcation (changelog + raised draft + archive)")

        # --- Case B: refuse a plan that lives in the inbox ---------------
        inbox_plan = inbox / "Inbox-Plan.md"
        inbox_plan.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# X\n\n- [x] done\n",
            encoding="utf-8",
        )
        res_b = bifurcate_plan(inbox_plan, vault, now=fixed_now)
        if res_b.status != "skipped-inbox":
            fail(f"B status={res_b.status} (want skipped-inbox)")
        elif not inbox_plan.exists():
            fail("B touched the inbox plan (must leave it untouched)")
        else:
            print("OK   case B: plan under <inbox> refused, left untouched")

        # --- Case C: nothing completed -----------------------------------
        plan_c = plans / "Open-Only-Plan.md"
        plan_c.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# Y\n\n- [ ] not done\n",
            encoding="utf-8",
        )
        res_c = bifurcate_plan(plan_c, vault, now=fixed_now)
        if res_c.status != "noop-nothing-done":
            fail(f"C status={res_c.status} (want noop-nothing-done)")
        elif not plan_c.exists():
            fail("C consumed a plan with nothing to split")
        else:
            print("OK   case C: nothing-done is a no-op")

        # --- Case D: not a plan ------------------------------------------
        note = plans / "Just-A-Note.md"
        note.write_text(
            "---\ntype: note\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# Z\n\n- [x] done\n",
            encoding="utf-8",
        )
        res_d = bifurcate_plan(note, vault, now=fixed_now)
        if res_d.status != "skipped-not-plan":
            fail(f"D status={res_d.status} (want skipped-not-plan)")
        else:
            print("OK   case D: non-plan refused")

        # --- Case E: never clobber an existing inbox draft ---------------
        plan_e = plans / "Demo-Plan.md"  # name collides with the case-A draft
        plan_e.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# E\n\n- [x] done\n",
            encoding="utf-8",
        )
        res_e = bifurcate_plan(plan_e, vault, now=fixed_now)
        if res_e.status != "aborted":
            fail(f"E status={res_e.status} (want aborted on clobber)")
        elif not plan_e.exists():
            fail("E destroyed the plan despite aborting")
        else:
            print("OK   case E: refuses to clobber an existing inbox draft")

        # --- Case F: append to an existing changelog ---------------------
        plan_f = plans / "Second-Plan.md"
        plan_f.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# F\n\n"
            "## Round\n\n- [x] round one done\n- [ ] round two open\n",
            encoding="utf-8",
        )
        bifurcate_plan(plan_f, vault, now=fixed_now)
        # Re-create the same filed plan with a second completed item, bifurcate again.
        (inbox / "Second-Plan.md").unlink()  # clear the raised draft so it won't clobber
        plan_f.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# F\n\n"
            "## Round\n\n- [x] round two done\n",
            encoding="utf-8",
        )
        bifurcate_plan(plan_f, vault, now=fixed_now)
        cl_f = (plans / "Second-Plan-Changelog.md").read_text(encoding="utf-8")
        if cl_f.count("## 2026-06-02 — from Second-Plan") != 2:
            fail("F changelog did not accumulate two dated entries")
        elif "round one done" not in cl_f or "round two done" not in cl_f:
            fail("F changelog missing an accumulated item")
        else:
            print("OK   case F: second bifurcation appends a new dated entry")

        # --- Case G: refuse a plan under a dot-directory (scan-exclusion) -
        kit_plans = vault / ".claude" / "skills" / "demo" / "Plans"
        kit_plans.mkdir(parents=True, exist_ok=True)
        kit_plan = kit_plans / "Kit-Plan.md"
        kit_plan.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# Kit\n\n- [x] done\n",
            encoding="utf-8",
        )
        res_g = bifurcate_plan(kit_plan, vault, now=fixed_now)
        if res_g.status != "skipped-inbox":
            fail(f"G status={res_g.status} (want skipped-inbox for dot-dir plan)")
        elif not kit_plan.exists():
            fail("G consumed a kit plan under .claude/ (must leave it untouched)")
        else:
            print("OK   case G: plan under .claude/ refused, left untouched")

        # --- Case H: discovery skips dot-directory plans -----------------
        found = discover_filed_plans(vault)
        if any(_in_excluded_dir(p) for p in found):
            fail("H discover_filed_plans returned a plan under a dot-directory")
        elif kit_plan not in found:
            print("OK   case H: discovery excludes the .claude/ plan")
        else:
            fail("H discovery included the .claude/ plan")

        # --- Case I: archive-verify failure is atomic — no residue (A1) ---
        plan_i = plans / "Atomic-Plan.md"
        plan_i.write_text(
            "---\ntype: plan\ntags:\n  - demo\ndate: 2026-06-02\n---\n\n# I\n\n"
            "## Round\n\n- [x] done\n- [ ] open\n",
            encoding="utf-8",
        )
        plan_i_bytes = plan_i.read_bytes()
        # Force the archive copy's hash to mismatch its source.
        _orig_sha = globals()["_sha256"]
        globals()["_sha256"] = (
            lambda p: _orig_sha(p) + ("|X" if "Atomic-Plan-bifurcated" in str(p) else "")
        )
        try:
            res_i = bifurcate_plan(plan_i, vault, now=fixed_now)
        finally:
            globals()["_sha256"] = _orig_sha
        cl_i = plans / "Atomic-Plan-Changelog.md"
        draft_i = inbox / "Atomic-Plan.md"
        # Capture the post-abort state as booleans BEFORE the clean retry, which
        # legitimately (re)creates the changelog and draft.
        aborted_ok = res_i.status == "aborted"
        src_kept = plan_i.exists() and plan_i.read_bytes() == plan_i_bytes
        no_cl = not cl_i.exists()
        no_draft = not draft_i.exists()
        no_arch = not list(archive.glob("Atomic-Plan-bifurcated-*.md"))
        if not aborted_ok:
            fail(f"I status={res_i.status} (want aborted on verify fail)")
        if not src_kept:
            fail("I source plan not kept byte-identical")
        if not no_cl:
            fail("I changelog residue left after aborted verify")
        if not no_draft:
            fail("I inbox draft residue left after aborted verify")
        if not no_arch:
            fail("I corrupt archive copy left behind (no residue expected)")
        # A clean retry (real hashing) must succeed with no clobber wedge.
        res_i2 = bifurcate_plan(plan_i, vault, now=fixed_now)
        retry_ok = res_i2.status == "bifurcated" and not plan_i.exists()
        if not retry_ok:
            fail(f"I clean retry did not succeed: status={res_i2.status}")
        if aborted_ok and src_kept and no_cl and no_draft and no_arch and retry_ok:
            print("OK   case I: archive-verify failure atomic (no draft/changelog/archive), clean retry succeeds")

    if failures:
        print(f"\n{failures} self-test failure(s).")
        sys.exit(1)
    print("\nAll self-tests passed.")


def _cli(argv: list[str]) -> None:
    apply = "--apply" in argv
    paths = [a for a in argv if not a.startswith("-")]
    if not paths:
        _run_self_tests()
        return
    vault_root = resolve_vault_root()
    if vault_root is None:
        print(
            "could not resolve vault root; set NOTE_KIT_VAULT_ROOT or run inside a vault",
            file=sys.stderr,
        )
        raise SystemExit(2)
    for raw in paths:
        res = bifurcate_plan(Path(raw), vault_root, dry_run=not apply)
        mode = "APPLIED" if apply and res.status == "bifurcated" else (
            "DRY-RUN" if res.status == "bifurcated" else res.status.upper())
        print(f"[{mode}] {raw}")
        print(f"  status={res.status} done={res.completed_count} open={res.open_count}")
        if res.changelog:
            print(f"  changelog -> {res.changelog}")
        if res.inbox_draft:
            print(f"  raised    -> {res.inbox_draft}")
        if res.archived:
            print(f"  archived  -> {res.archived}")
        if res.error:
            print(f"  note: {res.error}")


if __name__ == "__main__":
    _cli(sys.argv[1:])
