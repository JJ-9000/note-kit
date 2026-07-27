#!/usr/bin/env python3
"""
smoke_test.py
==================

End-to-end smoke test for the assembled note-kit. Proves that the kit installs
into a throwaway vault, that the deterministic layer runs against it without
ImportError or any other exception, and that a FRESH install is clean — the
audit proposes ~0 changes and never touches kit files under .claude/.

What it does:
  1. Scaffolds a disposable vault into a temp directory (NOT --clean, so the
     install survives for the steps below). The scaffold installs the whole kit
     to <tmp>/.claude/, writes settings.json + .mcp.json, creates the on-demand
     inbox subfolders, and runs sync-config once.
  2. Asserts the fresh install is correct, not just non-crashing:
       - <tmp>/.mcp.json exists and registers the `vault` MCP server.
       - <tmp>/<inbox-assets>/ exists; both queue files exist; retired
         surfaces are not scaffolded.
  3. Runs, against that install, each deterministic entry point and asserts a
     clean exit (returncode 0, no traceback on stderr):
       - audit.py --dry-run                      (janitor deterministic layer)
       - build_state_index.py               (vault snapshot)
       - sync_config.py --vault-root <tmp>  (CONFIG -> CLAUDE/AGENTS sync)
  4. Asserts the detect-only contract and a CLEAN fresh-install pass:
       - audit.py --dry-run writes NOTHING — no state snapshot, no ledger
         directory (the snapshot refresh is gated behind --apply);
       - the snapshot written by the direct build_state_index run proposes zero
         .claude/ paths (the kit must never propose moving its own files) and
         carries ~0 open findings (a fresh scaffold is already compliant).
         ## Detector status and ## Near-duplicate are read as diagnostics: with
         no index.db on a fresh install the near-duplicate detector skips, and
         the assertion is that the snapshot RECORDS that skip.
     Fails loudly if the dry run wrote anything, or the snapshot proposes a kit
     file, carries open findings, or drops a detector skip silently.
  5. Asserts the Lane-F portability + write-safety acceptance checks:
       - the fresh scaffold's settings.json (and the scaffold's own _HOOKS
         fallback) carry the live-proven `$CLAUDE_PROJECT_DIR` + "shell": "bash"
         hook form;
       - install_daemon re-substitutes config.yaml from its pristine template
         across a simulated vault move (two roots, correct paths both times);
       - an audit.py --apply pass against the scaffolded sandbox lands its
         pre-images in the per-run <archive>/<UTC-date>-audit-apply/ bundle with
         a manifest line each, reparse-verifies the rewritten note, persists its
         findings to the event ledger, and — across two runs with a hand-touch
         between them — fires reviewed-stale in the run-2 PERSISTED snapshot.
     (The cold-storage search-exclusion check is DEFERRED — it needs a live
     daemon instance the headless battery cannot cheaply stand up.)
  6. Removes the temp vault.

The --apply exercise runs LAST and only against the disposable scaffolded
sandbox, never the canon vault. It drives writes through env vars
(JANITOR_VAULT_ROOT / JANITOR_APPLY=1 / a scratch JANITOR_LEASE_PATH), which
every module and the build_state_index sub-subprocess read from the same
environment; the whole fixture is built before the first run (settled tree).

Each script is run as a fresh subprocess with the SAME interpreter, so an
ImportError surfaces exactly as it would in production (sys.path is set up by
each script from its own location inside the installed .claude/scripts/).

Run:
    python .claude/scripts/smoke_test.py [--keep]

Exit code 0 = PASS, 1 = FAIL. Prints a per-step table and a PASS/FAIL summary.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
_KIT_ROOT = _SCRIPTS_DIR.parent  # <vault>/.claude/ in an install, or the drop's .claude/

# Resolve semantic folder names from the kit's own CONFIG.md so the snapshot
# path below tracks CONFIG renames rather than hardcoding a literal archive name.
sys.path.insert(0, str(_SCRIPTS_DIR))
from config_variables import _folder_by_semantic, token_path  # noqa: E402

_INBOX_FOLDER = _folder_by_semantic("inbox")
_ARCHIVE_FOLDER = _folder_by_semantic("archive")
_AREAS_FOLDER = _folder_by_semantic("areas")
_REFERENCE_FOLDER = _folder_by_semantic("reference")
_INBOX_ASSETS_REL = token_path("inbox-assets")
_LOGS_REL = token_path("logs")
_USER_QUEUE_REL = token_path("user-queue")
_MACHINE_QUEUE_REL = token_path("machine-queue")


# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------

class StepResult:
    def __init__(self, name: str, ok: bool, detail: str = "") -> None:
        self.name = name
        self.ok = ok
        self.detail = detail


_RESULTS: list[StepResult] = []


def _record(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append(StepResult(name, ok, detail))
    status = "PASS" if ok else "FAIL"
    print(f"  [{status}] {name}" + (f" - {detail}" if detail else ""))
    return ok


# ---------------------------------------------------------------------------
# Subprocess runner with exception/ImportError detection
# ---------------------------------------------------------------------------

_TRACEBACK_MARKERS = ("Traceback (most recent call last)", "ImportError", "ModuleNotFoundError")


def _run(label: str, cmd: list[str], *, env: dict | None = None, cwd: str | None = None) -> bool:
    """Run cmd; PASS iff returncode == 0 and stderr shows no traceback/ImportError."""
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=env,
            cwd=cwd,
            timeout=300,
        )
    except Exception as exc:  # subprocess itself blew up (e.g. interpreter gone)
        return _record(label, False, f"subprocess error: {exc}")

    stderr = proc.stderr or ""
    # A traceback/ImportError on stderr is a failure even if returncode is 0
    # (e.g. a helper invoked by the script swallowed the non-zero exit).
    has_trace = any(marker in stderr for marker in _TRACEBACK_MARKERS)

    if proc.returncode != 0:
        tail = stderr.strip().splitlines()[-3:] if stderr.strip() else ["(no stderr)"]
        return _record(label, False, f"exit {proc.returncode}: " + " / ".join(tail))
    if has_trace:
        tail = stderr.strip().splitlines()[-3:]
        return _record(label, False, "traceback/ImportError on stderr: " + " / ".join(tail))
    return _record(label, True, "clean exit")


# ---------------------------------------------------------------------------
# Fresh-install assertions (fix 5): the scaffold must produce a clean,
# self-consistent install — not merely a non-crashing one.
# ---------------------------------------------------------------------------

def _assert_mcp_json(tmp_vault: Path) -> None:
    """<tmp>/.mcp.json exists and registers the `vault` HTTP MCP server."""
    mcp = tmp_vault / ".mcp.json"
    if not mcp.exists():
        _record("fresh-install: .mcp.json present", False, f"MISSING: {mcp}")
        return
    try:
        data = json.loads(mcp.read_text(encoding="utf-8"))
        server = (data.get("mcpServers") or {}).get("vault") or {}
        ok = server.get("type") == "http" and "8765" in str(server.get("url", ""))
        detail = "vault HTTP server registered" if ok else f"unexpected block: {server!r}"
        _record("fresh-install: .mcp.json registers vault server", ok, detail)
    except Exception as exc:
        _record("fresh-install: .mcp.json registers vault server", False, f"parse error: {exc}")


def _assert_on_demand_dirs(tmp_vault: Path) -> None:
    """The on-demand inbox subfolders skills write into must exist post-scaffold,
    both queue files must be seeded, and retired surfaces (00-Actions/,
    Checkpoints/, the legacy 00-Action-Queue.md) must NOT be scaffolded."""
    inbox = tmp_vault / _INBOX_FOLDER
    for label, p in [
        ("fresh-install: <inbox-assets>/", tmp_vault / _INBOX_ASSETS_REL),
        ("fresh-install: <user-queue> seeded", tmp_vault / _USER_QUEUE_REL),
        ("fresh-install: <machine-queue> seeded", tmp_vault / _MACHINE_QUEUE_REL),
    ]:
        _record(label, p.exists(), "present" if p.exists() else f"MISSING: {p}")
    for label, p in [
        ("fresh-install: retired <inbox>/00-Actions/ not scaffolded",
         inbox / "00-Actions"),
        ("fresh-install: retired 00-Action-Queue.md not scaffolded",
         inbox / "00-Action-Queue.md"),
    ]:
        _record(
            label,
            not p.exists(),
            "absent" if not p.exists() else f"PRESENT (retired surface): {p}",
        )


def _snapshot_path(tmp_vault: Path) -> Path:
    """The single shared state snapshot — <logs>/Vault-State-Index.md.

    CONFIG § Log files: the snapshot lives at the <logs> ROOT (not under an
    agent folder); the path resolves from the CONFIG token table.
    build_state_index owns it; audit.py refreshes it via build_state_index only
    under --apply — a detect-only run writes nothing. There are no per-run log
    files in v005, so this one file is the audit's whole proposed-work surface.
    """
    return tmp_vault / _LOGS_REL / "Vault-State-Index.md"


def _assert_dry_run_wrote_nothing(tmp_vault: Path) -> None:
    """audit.py --dry-run must write NOTHING: no state snapshot (its refresh is
    gated behind --apply) and no janitor ledger directory."""
    snap = _snapshot_path(tmp_vault)
    _record(
        "detect-only: no state snapshot written by --dry-run",
        not snap.exists(),
        "none" if not snap.exists() else f"UNEXPECTED: {snap}",
    )
    ledger_dir = tmp_vault / _LOGS_REL / "janitor-agent"
    _record(
        "detect-only: no ledger dir created by --dry-run",
        not ledger_dir.exists(),
        "none" if not ledger_dir.exists() else f"UNEXPECTED: {ledger_dir}",
    )


# Open-finding codes whose presence is inherent to a brand-new EMPTY scaffold
# and therefore not a defect: every canonical type has zero members, so the
# audit's drift pass emits one `type-unused` per type. These are analyst-macro
# observations, not per-file fixes the scaffold should have prevented.
_BENIGN_EMPTY_VAULT_CODES = frozenset({"type-unused"})


def _open_findings(snapshot_text: str) -> list[tuple[str, str]]:
    """Return (code, raw_row) for each state row in the ## Open findings section.

    A clean section is the single sentinel '(no open findings)' and yields [].
    A populated section carries pipe-delimited state rows
    (`timestamp | actor | code | target | count`, CONFIG § Log files) — both
    the count-rollup rows and the per-file detail rows. `code` is the third pipe
    field. The HTML shape comment and blank separators are skipped.
    """
    rows: list[tuple[str, str]] = []
    in_section = False
    for ln in snapshot_text.splitlines():
        s = ln.strip()
        if s.startswith("## Open findings"):
            in_section = True
            continue
        if in_section and s.startswith("## "):  # next section ends it
            break
        if not in_section or not s:
            continue
        if s.startswith("<!--") or s == "(no open findings)":
            continue
        if "|" not in s:
            continue
        parts = [p.strip() for p in s.split("|")]
        code = parts[2] if len(parts) >= 3 else parts[0]
        rows.append((code, s))
    return rows


def _section_body(snapshot_text: str, heading: str) -> str:
    """Return the text between `heading` and the next `## ` heading.

    Empty string when the heading is absent, so a caller asserting on a
    section's contents reads a missing section as a missing record.
    """
    out: list[str] = []
    in_section = False
    for ln in snapshot_text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            if in_section:
                break
            in_section = s == heading
            continue
        if in_section:
            out.append(ln)
    return "\n".join(out)


def _assert_clean_audit(tmp_vault: Path) -> None:
    """The shared state snapshot must propose no .claude/ path and carry no
    actionable open finding (a brand-new empty scaffold's `type-unused` macro
    excepted).

    v005 logging model (CONFIG § Log files): two artifacts under <logs>, no
    per-run files. The snapshot here comes from the DIRECT build_state_index
    run (audit.py refreshes it only under --apply; a detect-only run writes
    nothing). A fresh scaffold is already compliant, so the snapshot must:
      - name no path under .claude/ outside ## Detector status and
        ## Near-duplicate (proposing to touch kit files is the stale-install /
        dot-dir-scan failure this guards against; a skip reason naming the
        daemon's own index path is a diagnostic, and a fresh install by
        definition has no index.db for it to name — so what is asserted there is
        that the skip is RECORDED), and
      - carry no open finding except the inherent empty-vault `type-unused`
        (every canonical type has zero members in a just-created vault). Any
        other finding — a stray-folder on the scaffold's own subfolders, a
        would-move, a missing-frontmatter — means the install is not pristine.
        Detector-status rows (`near-duplicate-skipped` and its truncation twin)
        are not findings at all: build_state_index routes them to their own
        section, because reporting how a detector RAN proposes nothing to act
        on. `near-duplicate-skipped` fires on EVERY fresh install.
    """
    snap = _snapshot_path(tmp_vault)
    if not snap.exists():
        _record("fresh-install: state snapshot written", False, f"missing: {snap}")
        return
    _record("fresh-install: state snapshot written", True, snap.name)

    text = snap.read_text(encoding="utf-8")

    # (c1) The kit never PROPOSES touching its own files: no row outside the
    #      diagnostic sections may name a path under .claude/. Match both slash
    #      styles.
    #
    #      ## Detector status and ## Near-duplicate are exempt, and the exemption
    #      is the honest reading rather than a loosened gate. The near-duplicate
    #      detector reads the vault-search daemon's own embeddings out of
    #      .claude/vault-search/data/index.db. A fresh install HAS no index.db —
    #      that is what "fresh" means — so the detector skips and records where
    #      the index would have been. Asserting that path is absent would be
    #      asserting a state a fresh install cannot reach. What the fresh install
    #      owes instead is the RECORD: when the detector skips, the snapshot says
    #      so in ## Detector status, out of ## Open findings, with no fallback
    #      similarity substituted.
    exempt_sections = {"## Detector status", "## Near-duplicate"}
    section = ""
    claude_hits: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("## "):
            section = s
            continue
        if section in exempt_sections:
            continue
        if ".claude/" in s or ".claude\\" in s:
            claude_hits.append(s)

    # The skip, when it happens, is recorded — never silently dropped.
    nd_skipped = "(skipped — " in text
    skip_recorded = "near-duplicate-skipped" in _section_body(text, "## Detector status")
    skip_ok = skip_recorded if nd_skipped else True

    _record(
        "fresh-install: snapshot proposes no .claude/ path (daemon-absent skip recorded)",
        not claude_hits and skip_ok,
        "none"
        if not claude_hits and skip_ok
        else (f"{len(claude_hits)} hit(s): {claude_hits[0][:80]}" if claude_hits
              else "near-duplicate skipped but no near-duplicate-skipped row in "
                   "## Detector status"),
    )

    # (c2) No actionable open finding. `type-unused` is inherent to an empty
    # scaffold and excepted; every other code is a real proposed change.
    findings = _open_findings(text)
    benign = [r for code, r in findings if code in _BENIGN_EMPTY_VAULT_CODES]
    actionable = [(code, r) for code, r in findings if code not in _BENIGN_EMPTY_VAULT_CODES]
    if benign:
        # Informational, non-failing: the expected empty-vault macro rows.
        _record(
            f"fresh-install: {len(benign)} benign type-unused macro row(s) "
            "(empty vault, expected)",
            True,
            "excepted",
        )
    distinct = sorted({code for code, _ in actionable})
    _record(
        "fresh-install: snapshot has no actionable open findings",
        not actionable,
        "none" if not actionable
        else f"{len(actionable)} row(s); codes: {', '.join(distinct)}",
    )


# ---------------------------------------------------------------------------
# Regression guard: an unreviewed addendum survives a filing/index pass.
#
# 2026-06-16 — the filing-agent merged a `reviewed: false` addendum into its
# target plan (archive-first) because the target RESOLVED, bypassing the user's
# approval gate. The fix makes the addendum's OWN `reviewed` field the merge
# gate (CONFIG § Operational documents; filing-agent SKILL §1/§2/§2a). This is
# the deterministic backstop: the shared work-surface the agents read must
# treat a reviewed:false addendum as a WAITING draft — never merge it into its
# target, never archive it, never flip its `reviewed` flag — even when its
# `target` resolves to a real vault file.
# ---------------------------------------------------------------------------

def _hash(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _assert_addendum_survives(tmp_vault: Path, scripts_dir: Path, env: dict) -> None:
    """Drop a reviewed:false addendum whose target resolves, run the snapshot
    builder, and assert the addendum is untouched: still in the inbox, byte
    unchanged, still reviewed:false, its target plan not merged into, and the
    addendum surfaced as a waiting draft in the review backlog (not filed)."""
    target = tmp_vault / _AREAS_FOLDER / "Addendum-Survival-Target-Plan.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "---\ntype: plan\ntags:\n  - plan\ndate: 2026-06-16\n"
        'parent: "[[Note-Kit]]"\nreviewed: true\nstatus: in-progress\n---\n\n'
        "# Addendum-Survival-Target-Plan\n\nOriginal plan body — no merged content.\n",
        encoding="utf-8",
    )
    addendum = tmp_vault / _INBOX_FOLDER / "Addendum-Survival-Sample.md"
    addendum.parent.mkdir(parents=True, exist_ok=True)
    addendum.write_text(
        "---\ntype: addendum\ntags:\n  - addendum\ndate: 2026-06-16\n"
        'target: "[[Addendum-Survival-Target-Plan]]"\nreviewed: false\nstatus: draft\n---\n\n'
        "# Addendum-Survival-Sample\n\nProposed edit that merges into the target plan.\n",
        encoding="utf-8",
    )

    add_before, tgt_before = _hash(addendum), _hash(target)

    bsi = scripts_dir / "build_state_index.py"
    proc = subprocess.run(
        [sys.executable, str(bsi)],
        capture_output=True, text=True, env=env, cwd=str(tmp_vault), timeout=300,
    )
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-1:] or ["nonzero exit"]
        _record("addendum-survival: build_state_index clean exit", False, tail[0])
        return
    _record("addendum-survival: build_state_index clean exit", True, "rc 0")

    add_ok = addendum.exists() and _hash(addendum) == add_before
    _record(
        "addendum-survival: reviewed:false addendum untouched in inbox",
        add_ok,
        "unchanged" if add_ok else "MUTATED OR MOVED — the merge gate leaked",
    )

    still_false = addendum.exists() and "reviewed: false" in addendum.read_text(encoding="utf-8")
    _record(
        "addendum-survival: addendum still reviewed:false (no auto-approve)",
        still_false,
        "false" if still_false else "FLIPPED to reviewed:true",
    )

    tgt_ok = _hash(target) == tgt_before
    _record(
        "addendum-survival: target plan not merged into",
        tgt_ok,
        "unchanged" if tgt_ok else "MERGED — unreviewed content reached the target",
    )

    # The deterministic layer leaves inbox drafts alone — the addendum must
    # appear in NO open finding (a proposal to file, merge, or relocate it).
    # Filing/merging an addendum is the agent's gated decision, never an
    # automatic one keyed off a resolving target.
    snap = _snapshot_path(tmp_vault)
    findings = _open_findings(snap.read_text(encoding="utf-8")) if snap.exists() else []
    proposed = [row for _, row in findings if "Addendum-Survival-Sample" in row]
    _record(
        "addendum-survival: deterministic layer proposes no action on the inbox addendum",
        not proposed,
        "no finding targets it" if not proposed
        else f"a finding proposes acting on the unreviewed addendum: {proposed[0][:80]}",
    )


# ---------------------------------------------------------------------------
# Lane-F additions: hook-form, installer re-substitution, and the --apply
# write-safety exercise. A deferred check is announced but not counted toward
# PASS/FAIL, so the summary stays honest about what the headless battery covers.
# ---------------------------------------------------------------------------

def _note_deferred(name: str, reason: str) -> None:
    """Announce a check the headless battery cannot cheaply run. Not recorded —
    it neither passes nor fails, it is skipped for a stated reason."""
    print(f"  [DEFER] {name} - {reason}")


def _load_installed_module(name: str, path: Path):
    """Import a module from an explicit file path (the INSTALLED copy in the
    scaffold), so the battery tests the shipped code. None on any failure."""
    try:
        spec = importlib.util.spec_from_file_location(f"_smoke_{name}", path)
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        # Register before exec: a module-level construct that looks itself up in
        # sys.modules during class creation (e.g. a @dataclass on Python 3.14)
        # raises AttributeError if the module is not yet registered under its name.
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        sys.modules.pop(f"_smoke_{name}", None)  # never leave a partial module registered
        return None


# A proven hook command addresses its script through $CLAUDE_PROJECT_DIR (the
# vault root Claude Code exports) — never the relative ./.claude/ form that
# resolved to a doubled path and shipped silently-dead hooks.
_PROVEN_HOOK_RE = re.compile(r"\$CLAUDE_PROJECT_DIR/\.claude/hooks/[^\s\"']+")


def _hook_handlers(settings: dict):
    """Yield every command-handler dict across all events and matcher groups."""
    for groups in (settings.get("hooks") or {}).values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            for handler in group.get("hooks") or []:
                if isinstance(handler, dict):
                    yield handler


def _assert_hook_form_proven(label: str, settings: dict) -> None:
    """Every hook handler uses the $CLAUDE_PROJECT_DIR command form under
    "shell": "bash" — the live-proven form."""
    handlers = list(_hook_handlers(settings))
    if not handlers:
        _record(label, False, "no hook handlers found")
        return
    bad = [
        str(h.get("command") or "(empty)")
        for h in handlers
        if not _PROVEN_HOOK_RE.search(str(h.get("command") or ""))
        or h.get("shell") != "bash"
    ]
    _record(
        label,
        not bad,
        f"{len(handlers)} handler(s), all $CLAUDE_PROJECT_DIR + shell:bash"
        if not bad else f"{len(bad)} not proven-form: {bad[0][:60]}",
    )


def _assert_scaffold_hooks_proven(tmp_vault: Path, installed_scripts: Path) -> None:
    """Item 1 acceptance: the fresh scaffold's settings.json parses and every
    hook registration uses the live-proven form; and the scaffold's own module-
    level _HOOKS fallback (written when a bare clone ships no settings.json)
    carries the same form."""
    settings_path = tmp_vault / ".claude" / "settings.json"
    if not settings_path.exists():
        _record("hooks: fresh scaffold settings.json present", False, f"MISSING: {settings_path}")
        return
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _record("hooks: fresh scaffold settings.json parses", False, f"parse error: {exc}")
        return
    _record("hooks: fresh scaffold settings.json parses", True, "valid JSON")
    _assert_hook_form_proven(
        "hooks: fresh scaffold registrations are the proven form", settings
    )

    mod = _load_installed_module("scaffold_vault", installed_scripts / "scaffold_vault.py")
    if mod is None or not isinstance(getattr(mod, "_HOOKS", None), dict):
        _record("hooks: scaffold _HOOKS fallback is the proven form", False,
                "could not load installed scaffold_vault._HOOKS")
        return
    _assert_hook_form_proven(
        "hooks: scaffold _HOOKS fallback is the proven form", mod._HOOKS
    )


def _assert_installer_resubstitutes(tmp_vault: Path) -> None:
    """Item 3 acceptance: install_daemon writes config.yaml FROM its pristine
    config.yaml.template and never edits the template, so a re-install after a
    vault move re-substitutes the new root instead of re-indexing the old path.
    Two substitutions to the SAME dest from two different roots must each produce
    that root's paths, and the template must keep its placeholders."""
    daemon_dir = tmp_vault / ".claude" / "vault-search"
    template = daemon_dir / "config.yaml.template"
    installer_path = daemon_dir / "install_daemon.py"
    if not (template.exists() and installer_path.exists()):
        _record("installer: template + install_daemon.py present", False,
                f"missing template or installer under {daemon_dir}")
        return
    _record("installer: template + install_daemon.py present", True, "present")

    mod = _load_installed_module("install_daemon", installer_path)
    if mod is None or not hasattr(mod, "_substitute_config"):
        _record("installer: _substitute_config importable", False,
                "could not load install_daemon._substitute_config")
        return

    work = Path(tempfile.mkdtemp(prefix="note-kit-installer-"))
    try:
        root_a = work / "vault-a"
        root_b = work / "vault-b"
        root_a.mkdir()
        root_b.mkdir()
        dest = work / "config.yaml"  # same dest both installs — a vault move
        home = Path(os.path.expanduser("~"))

        mod._substitute_config(template, dest, vault_root=root_a, home=home,
                               data_dir=root_a / "data", inbox="Inbox")
        text_a = dest.read_text(encoding="utf-8")
        ok_a = root_a.as_posix() in text_a and root_b.as_posix() not in text_a
        _record("installer: install #1 writes root-A paths", ok_a,
                "root-A paths" if ok_a else "root-A path missing or contaminated")

        mod._substitute_config(template, dest, vault_root=root_b, home=home,
                               data_dir=root_b / "data", inbox="Inbox")
        text_b = dest.read_text(encoding="utf-8")
        ok_b = root_b.as_posix() in text_b and root_a.as_posix() not in text_b
        _record("installer: install #2 (moved vault) re-substitutes root-B paths", ok_b,
                "root-B, no stale root-A" if ok_b else "STALE root-A survived the move")

        pristine = "<VAULT_ROOT>" in template.read_text(encoding="utf-8")
        _record("installer: template stays pristine across installs", pristine,
                "placeholders intact" if pristine else "template was substituted in place")
    except Exception as exc:
        _record("installer: re-substitution exercise", False, f"error: {exc}")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _file_contains(path: Path, needle: str, tries: int = 4) -> bool:
    """True when `needle` is in `path`. Re-reads a few times to defeat a stale
    read-after-write hiccup on the audit subprocess's just-written file (Windows
    temp storage occasionally serves an empty/partial read right after a foreign
    process closes the file). A genuine absence stays absent across every retry,
    so this hardens against a transient without masking a real miss."""
    for attempt in range(tries):
        try:
            if path.exists() and needle in path.read_text(encoding="utf-8"):
                return True
        except OSError:
            pass
        if attempt < tries - 1:
            import time
            time.sleep(0.1)
    return False


def _split_frontmatter(text: str) -> tuple[str | None, str | None]:
    """Minimal front-matter reparse: (frontmatter_block, body) or (None, None)
    when the text is not a `---` frontmatter document."""
    if not text.startswith("---"):
        return None, None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None, None
    return parts[1], parts[2]


def _assert_apply_exercise(
    tmp_vault: Path, installed_scripts: Path, installed_audit: Path
) -> None:
    """Item 4: exercise audit.py's mutating --apply path against the scaffolded
    sandbox (never the canon vault). One pass whose per-run archive bundle,
    reparse-verify, and findings persistence are asserted, plus the persisted-
    snapshot reviewed-stale acceptance across two runs with a hand-touch between.

    Settled tree: the whole fixture is built before the first run. Path globals:
    the vault root, apply gate, and lease path travel as env vars every module
    (and the build_state_index sub-subprocess) reads from the same environment.
    The lease is a scratch temp file, never the vault's <logs>/run-lease.md.
    """
    if not installed_audit.exists():
        _record("apply-exercise: audit.py installed", False, "audit.py not installed")
        return

    refs = tmp_vault / _REFERENCE_FOLDER
    refs.mkdir(parents=True, exist_ok=True)

    # (a) a note missing only `date` — pass16 stamps it (the kit's highest-volume
    #     frontmatter write), routing its pre-image into the run bundle.
    missing_date = refs / "Apply-Missing-Date-Fixture.md"
    missing_date.write_text(
        "---\ntype: reference\ntags:\n  - reference\n"
        'parent: "[[Apply-Fixture-Parent]]"\n---\n\n'
        "# Apply-Missing-Date-Fixture\n\nNo date; pass16 stamps it under --apply.\n",
        encoding="utf-8",
    )
    # (b) reviewed-stale pair: a reviewed:true note linking an upstream reference
    #     whose body is edited between the two runs.
    upstream = refs / "Apply-Stale-Upstream.md"
    upstream.write_text(
        "---\ntype: reference\ntags:\n  - reference\ndate: 2020-01-01\n"
        'parent: "[[Apply-Fixture-Parent]]"\n---\n\n'
        "# Apply-Stale-Upstream\n\nUpstream body version one.\n",
        encoding="utf-8",
    )
    reviewed_note = refs / "Apply-Reviewed-Stale-Note.md"
    reviewed_note.write_text(
        "---\ntype: reference\ntags:\n  - reference\ndate: 2020-01-01\n"
        'parent: "[[Apply-Fixture-Parent]]"\nreviewed: true\n---\n\n'
        "# Apply-Reviewed-Stale-Note\n\nReviewed against [[Apply-Stale-Upstream]].\n",
        encoding="utf-8",
    )
    missing_before = _hash(missing_date)

    # Scratch lease OUTSIDE the vault's own <logs>/run-lease.md (live-held): a
    # dedicated temp file, cleaned up at the end.
    lease_fd, lease_name = tempfile.mkstemp(prefix="note-kit-apply-lease-", suffix=".md")
    os.close(lease_fd)
    lease_path = Path(lease_name)
    lease_path.unlink(missing_ok=True)

    apply_env = {
        **os.environ,
        "JANITOR_VAULT_ROOT": str(tmp_vault),
        "JANITOR_APPLY": "1",
        "JANITOR_LEASE_PATH": str(lease_path),
    }

    def _run_apply(tag: str) -> bool:
        proc = subprocess.run(
            [sys.executable, str(installed_audit), "--apply"],
            capture_output=True, text=True, env=apply_env, cwd=str(tmp_vault), timeout=300,
        )
        stderr = proc.stderr or ""
        trace = any(m in stderr for m in _TRACEBACK_MARKERS)
        ok = proc.returncode == 0 and not trace
        tail = " / ".join(stderr.strip().splitlines()[-2:]) if stderr.strip() else "clean"
        return _record(
            f"apply-exercise: audit.py --apply [{tag}] clean exit",
            ok, "rc 0" if ok else f"rc {proc.returncode}: {tail}",
        )

    try:
        # RUN 1 — the mutating pass.
        if not _run_apply("run 1"):
            return

        # (1) per-run bundle <archive>/<UTC-date>-audit-apply/ with a mirrored
        #     pre-image and a manifest line per archived file.
        bundles = sorted((tmp_vault / _ARCHIVE_FOLDER).glob("*-audit-apply"))
        bundle = bundles[-1] if bundles else None
        _record(
            "apply-exercise: per-run <archive>/<date>-audit-apply/ bundle created",
            bundle is not None,
            bundle.name if bundle else "no <archive>/*-audit-apply/ dir",
        )
        if bundle is None:
            return
        preimage = bundle / _REFERENCE_FOLDER / "Apply-Missing-Date-Fixture.md"
        _record(
            "apply-exercise: bundle holds the mirrored pre-image",
            preimage.exists(),
            "mirrored source-relative" if preimage.exists() else f"MISSING: {preimage}",
        )
        manifest = bundle / "manifest.md"
        man_ok = _file_contains(manifest, "Apply-Missing-Date-Fixture.md")
        _record(
            "apply-exercise: manifest carries a line per archived file",
            man_ok,
            "manifest line present" if man_ok else "no manifest line for the rewritten file",
        )
        # The pre-image is the ARCHIVE-FIRST original: it lacks the `date:` the
        # live file gained under the rewrite.
        pre_is_original = preimage.exists() and "date:" not in preimage.read_text(encoding="utf-8")
        _record(
            "apply-exercise: pre-image is the archive-first original",
            pre_is_original,
            "pre-rewrite bytes" if pre_is_original else "pre-image already carries the rewrite",
        )

        # (2) reparse-verify: the rewritten note reparses to a valid frontmatter
        #     document with the stamped date and its body intact. structured_rewrite
        #     restores the pre-image on an invariant failure, so a valid live file
        #     is proof the post-write reparse passed.
        block, body = _split_frontmatter(missing_date.read_text(encoding="utf-8"))
        reparse_ok = (
            block is not None
            and "date:" in block
            and body is not None
            and "pass16 stamps it" in body
        )
        _record(
            "apply-exercise: reparse-verify — rewritten note valid post-write",
            reparse_ok,
            "valid frontmatter + stamped date, body intact" if reparse_ok else "post-write reparse failed",
        )
        _record(
            "apply-exercise: rewrite changed the file (mutating path ran)",
            _hash(missing_date) != missing_before,
            "mutated" if _hash(missing_date) != missing_before else "UNCHANGED — apply path did not write",
        )

        # (3) findings persisted: the completed action survives in the append-only
        #     ledger, and the shared snapshot was refreshed.
        ledger = tmp_vault / _LOGS_REL / "janitor-agent" / "janitor-agent.md"
        led_ok = _file_contains(ledger, "Apply-Missing-Date-Fixture.md")
        _record(
            "apply-exercise: findings persisted to the event ledger",
            led_ok,
            "date-resolved event recorded" if led_ok else "no ledger trace of the run",
        )
        snap = _snapshot_path(tmp_vault)
        _record(
            "apply-exercise: shared snapshot refreshed under --apply",
            snap.exists(),
            "snapshot present" if snap.exists() else f"MISSING: {snap}",
        )

        # (4) persisted-snapshot reviewed-stale: hand-touch the reviewed note's
        #     upstream between two --apply runs; the SECOND run's PERSISTED
        #     snapshot must fire reviewed-stale (the double-snapshot fix — the
        #     finding lands in the durable snapshot, not only the transient
        #     pre-pass one).
        upstream.write_text(
            "---\ntype: reference\ntags:\n  - reference\ndate: 2020-01-01\n"
            'parent: "[[Apply-Fixture-Parent]]"\n---\n\n'
            "# Apply-Stale-Upstream\n\nUpstream body version TWO — edited content.\n",
            encoding="utf-8",
        )
        lease_path.unlink(missing_ok=True)
        if not _run_apply("run 2"):
            return
        snap_text = snap.read_text(encoding="utf-8") if snap.exists() else ""
        fired = any(
            "reviewed-stale" in ln and "Apply-Reviewed-Stale-Note.md" in ln
            for ln in snap_text.splitlines()
        )
        _record(
            "apply-exercise: persisted snapshot fires reviewed-stale on run 2",
            fired,
            "reviewed-stale row present" if fired else "reviewed-stale absent from persisted snapshot",
        )
    finally:
        lease_path.unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the assembled note-kit.")
    parser.add_argument(
        "--keep",
        action="store_true",
        help="Do not delete the scaffolded temp vault (for debugging).",
    )
    args = parser.parse_args()

    print("note-kit smoke test")
    print(f"  kit root: {_KIT_ROOT}")

    scaffold_src = _SCRIPTS_DIR / "scaffold_vault.py"
    if not scaffold_src.exists():
        _record("locate scaffold", False, f"not found: {scaffold_src}")
        return _summary()

    # 1. Scaffold a disposable vault into a temp dir. We create the temp dir
    #    ourselves and pass it as --path WITHOUT --clean, so the install survives
    #    for the steps below; we remove it at the end. (--path under the system
    #    temp dir would also satisfy the scaffold's --clean guard, but we want
    #    the tree to persist between steps.)
    tmp_vault = Path(tempfile.mkdtemp(prefix="note-kit-smoke-"))
    print(f"  temp vault: {tmp_vault}")

    try:
        ok = _run(
            "scaffold install (.claude/ + settings.json + sync)",
            [sys.executable, str(scaffold_src), "--path", str(tmp_vault)],
        )

        # Resolve the installed scripts dir — every later step must run the
        # INSTALLED copy so its own sys.path bootstrap is exercised.
        installed_scripts = tmp_vault / ".claude" / "scripts"
        installed_audit = tmp_vault / ".claude" / "scheduled-tasks" / "janitor-agent" / "audit.py"

        # Verify the install actually produced the pieces we are about to run.
        for label, p in [
            ("install: .claude/CONFIG.md", tmp_vault / ".claude" / "CONFIG.md"),
            ("install: .claude/CLAUDE.md", tmp_vault / ".claude" / "CLAUDE.md"),
            ("install: .claude/RULES.md", tmp_vault / ".claude" / "RULES.md"),
            ("install: .claude/settings.json", tmp_vault / ".claude" / "settings.json"),
            ("install: scripts/build_state_index.py",
             installed_scripts / "build_state_index.py"),
            ("install: janitor-agent/audit.py", installed_audit),
        ]:
            _record(label, p.exists(), "present" if p.exists() else f"MISSING: {p}")

        # 1b. Fresh-install structural assertions — the scaffold must produce a
        #     clean, self-consistent install: the retrieval-spine .mcp.json and
        #     the on-demand inbox subfolders skills write into.
        _assert_mcp_json(tmp_vault)
        _assert_on_demand_dirs(tmp_vault)

        # 2a. audit.py --dry-run — this is the import-blocker canary. It imports
        #     the helper modules; a missing one is an ImportError here.
        janitor_env = {**os.environ, "JANITOR_VAULT_ROOT": str(tmp_vault)}
        if installed_audit.exists():
            audit_ok = _run(
                "audit.py --dry-run",
                [sys.executable, str(installed_audit), "--dry-run"],
                env=janitor_env,
                cwd=str(tmp_vault),
            )
            # 2a'. Detect-only contract: a dry run writes NOTHING — no state
            #      snapshot, no ledger directory.
            if audit_ok:
                _assert_dry_run_wrote_nothing(tmp_vault)
        else:
            _record("audit.py --dry-run", False, "audit.py not installed; cannot run")

        # 2b. build_state_index.py — vault snapshot.
        bsi = installed_scripts / "build_state_index.py"
        if bsi.exists():
            bsi_ok = _run(
                "build_state_index.py",
                [sys.executable, str(bsi)],
                env=janitor_env,
                cwd=str(tmp_vault),
            )
            # 2b'. Clean fresh-install assertion: read the snapshot this direct
            #      run wrote and fail loudly if it named a .claude/ path or
            #      carried any open finding on a freshly scaffolded
            #      (already-compliant) vault.
            if bsi_ok:
                _assert_clean_audit(tmp_vault)
                # Regression guard (2026-06-16): a reviewed:false addendum whose
                # target resolves must survive a deterministic pass untouched.
                # Runs AFTER the clean-audit assertion so its fixture files do
                # not perturb the fresh-install findings check above.
                _assert_addendum_survives(tmp_vault, installed_scripts, janitor_env)
        else:
            _record("build_state_index.py", False, "not installed; cannot run")

        # 2c. sync_config.py --vault-root <tmp> — CONFIG -> CLAUDE/AGENTS.
        sync = installed_scripts / "sync_config.py"
        if sync.exists():
            _run(
                "sync_config.py --vault-root",
                [sys.executable, str(sync), "--vault-root", str(tmp_vault)],
                cwd=str(tmp_vault),
            )
        else:
            _record("sync_config.py --vault-root", False, "not installed; cannot run")

        # 3. Lane-F acceptance checks. The read-only ones (hook form, installer
        #    re-substitution) run first; the DEFERRED cold-storage check is only
        #    announced. The --apply exercise MUTATES the sandbox, so it runs LAST
        #    — after every clean-vault assertion above.
        _assert_scaffold_hooks_proven(tmp_vault, installed_scripts)
        _assert_installer_resubstitutes(tmp_vault)
        _note_deferred(
            "cold-storage search-exclusion (aged-out file returns no search hit)",
            "needs a live vault-search daemon (venv + embedding model + port 8765); "
            "not headless-cheap and would collide with a running daemon. "
            "Covered by Lane V's own daemon tests.",
        )
        _assert_apply_exercise(tmp_vault, installed_scripts, installed_audit)

    finally:
        # 3. Clean up the temp vault unless --keep.
        if args.keep:
            print(f"  --keep set; leaving temp vault at {tmp_vault}")
        else:
            shutil.rmtree(tmp_vault, ignore_errors=True)
            print(f"  cleaned up {tmp_vault}")

    return _summary()


def _summary() -> int:
    total = len(_RESULTS)
    passed = sum(1 for r in _RESULTS if r.ok)
    failed = total - passed
    print()
    print("=" * 60)
    if failed == 0 and total > 0:
        print(f"SMOKE TEST: PASS ({passed}/{total} steps)")
        return 0
    print(f"SMOKE TEST: FAIL ({passed}/{total} passed, {failed} failed)")
    for r in _RESULTS:
        if not r.ok:
            print(f"  - {r.name}: {r.detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
