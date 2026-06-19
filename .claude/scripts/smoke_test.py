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
       - the snapshot written by the direct build_state_index run names zero
         .claude/ paths (the kit must never propose moving its own files) and
         carries ~0 open findings (a fresh scaffold is already compliant).
     Fails loudly if the dry run wrote anything, or the snapshot names a kit
     file or carries open findings.
  5. Removes the temp vault.

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
import json
import os
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


def _assert_clean_audit(tmp_vault: Path) -> None:
    """The shared state snapshot must have zero .claude/ paths and no actionable
    open findings (a brand-new empty scaffold's `type-unused` macro excepted).

    v005 logging model (CONFIG § Log files): two artifacts under <logs>, no
    per-run files. The snapshot here comes from the DIRECT build_state_index
    run (audit.py refreshes it only under --apply; a detect-only run writes
    nothing). A fresh scaffold is already compliant, so the snapshot must:
      - name no path under .claude/ (proposing to touch kit files is the
        stale-install / dot-dir-scan failure this guards against), and
      - carry no open finding except the inherent empty-vault `type-unused`
        (every canonical type has zero members in a just-created vault). Any
        other finding — a stray-folder on the scaffold's own subfolders, a
        would-move, a missing-frontmatter — means the install is not pristine.
    """
    snap = _snapshot_path(tmp_vault)
    if not snap.exists():
        _record("fresh-install: state snapshot written", False, f"missing: {snap}")
        return
    _record("fresh-install: state snapshot written", True, snap.name)

    text = snap.read_text(encoding="utf-8")

    # (c1) Zero .claude/ paths anywhere in the snapshot. Match both slash styles.
    claude_hits = [
        ln.strip() for ln in text.splitlines()
        if ".claude/" in ln or ".claude\\" in ln
    ]
    _record(
        "fresh-install: snapshot has zero .claude/ paths",
        not claude_hits,
        "none" if not claude_hits else f"{len(claude_hits)} hit(s): {claude_hits[0][:80]}",
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
