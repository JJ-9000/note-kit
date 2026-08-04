#!/usr/bin/env python3
"""
scheduled_gate_launcher.py
==========================

The gated deployment wrapper for the four note-kit scheduled agents — the
production entry point a Windows Task Scheduler trigger runs on each cadence
(Note-Kit-Model-Tiering-and-Skill-Queue-Plan § D). It generalizes
`event_action_launcher.py` from "one agent, event-fired, settle-debounced" to
"any of the four agents, cron-fired, gate-checked": the deterministic gate
(`agent_gate.py`) runs BEFORE any model loads, and a provably-empty scope
spawns nothing at all.

Steps, in order
---------------
0. **Lock.** A per-agent single-instance guard at
   `<logs>/gate-launcher-<agent>.lock` (pid + start, stale holders displaced
   through a rename) — `event_action_launcher.acquire_launch_lock` with a
   per-agent path. A second fire while one invocation runs prints one
   `lock-defer` line and exits 0.
1. **Lease.** Read `<logs>/run-lease.md` (CONFIG § Concurrency rule 1) with
   `event_action_launcher.read_lease_status` — read-only, never taken. A
   fresh holder prints `lease-defer` and exits 0; the cadence retries. The
   gate is not consulted under a live lease: a mid-mutation corpus would
   fingerprint mid-edit state.
2. **Gate.** `agent_gate.evaluate(vault_root, agent)` — stdlib, no model.
   `quiet` exits 0 with nothing written anywhere: no spawn, no log line, no
   fingerprint update, so repeated quiet passes stay quiet and cost nothing.
   A gate crash is treated as work with a `gate-error` reason (fail-open to
   the pre-gate status quo) — a gate bug must never starve the agents.
3. **Launch.** On `work`: read the deployed SKILL at
   `<user-home>/.claude/scheduled-tasks/note-kit-<agent>-agent/SKILL.md`,
   spawn `claude -p` with `--output-format json` and `--max-budget-usd` (the
   per-agent table below), the SKILL body on stdin, `CREATE_NO_WINDOW`,
   detached. Deliberately NO `--permission-mode`: the run inherits
   `permissions.defaultMode` from `<user-home>/.claude/settings.json`
   (`bypassPermissions`), the same mode Desktop-fired agents run under today
   — where `event_action_launcher` passes `--permission-mode acceptEdits`,
   this launcher passes no mode flag at all.
4. **Record.** Append one `gate-launch` line to the agent's own event log
   (`<logs>/<stem>/<stem>.md`, CONFIG § Log files shape) and write the gate's
   fingerprint to `<logs>/<agent>-gate-fingerprint.json` — both only when a
   spawn actually happened, so a deferral or failure leaves the gate armed.

Kit imports
-----------
The proven primitives (lock, lease read, spawn, log append, claude-bin
resolve) are IMPORTED from `event_action_launcher.py`, not copied: deployed,
this file sits beside it in `<kit-root>/scripts/`, and `sys.path` is pinned
to the module's own directory so a Task Scheduler start (whose default cwd is
System32) resolves the siblings regardless of working directory. During
rehearsal the kit directory is named via `--scripts-dir` or
`NOTE_KIT_SCRIPTS_DIR`. A failed kit import exits 1 after appending one
`launcher-error` line with self-contained code — fail closed and loud,
because without `config_variables` (which hard-fails on a broken CONFIG by
design) the lease cannot be read, and spawning a mutating agent blind to the
lease risks a double-run.

Usage
-----
    pythonw scheduled_gate_launcher.py --vault-root <vault> --agent janitor
    python  scheduled_gate_launcher.py --vault-root <vault> --agent action --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

import agent_gate  # sibling; stdlib-only, safe to import unconditionally

ACTOR = "gate-launcher"
_LOCK_STALE_SECONDS = 900.0     # the coalescing bound event_action_launcher uses

# Per-agent spend ceilings. The action figure is research-backed
# (2026-07-26-Event-Driven-Action-Agent-Research § economics: $6.48 median /
# $6.89 mean acting pass, so $10 clears a normal pass and stops a runaway).
# The other three are conservative guesses pending § C grading — wider scopes
# get wider ceilings — and every value yields to --max-budget-usd.
DEFAULT_BUDGETS_USD = {
    "action": 10.00,
    "filing": 10.00,
    "janitor": 15.00,
    "analyst": 25.00,
}

_LOG_HEAD = """---
type: log
tags:
  - {stem}
  - log
date: {date}
---

# {stem} event log

Append-only, one pipe-line per action: `timestamp | actor | code | target | value`.
"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def agent_full_name(agent: str) -> str:
    return f"note-kit-{agent}-agent"


def agent_stem(agent: str) -> str:
    return f"{agent}-agent"


# ---------------------------------------------------------------------------
# Kit-module resolution
# ---------------------------------------------------------------------------

def locate_kit_scripts(vault_root: Path,
                       explicit: Optional[Path] = None) -> Optional[Path]:
    """The directory holding event_action_launcher.py, or None.

    Order: an explicit --scripts-dir, NOTE_KIT_SCRIPTS_DIR, this file's own
    directory (the deployed layout), then `<vault-root>/.claude/scripts` (a
    launcher run from outside the kit against a real vault).
    """
    candidates = [
        explicit,
        Path(os.environ["NOTE_KIT_SCRIPTS_DIR"])
        if os.environ.get("NOTE_KIT_SCRIPTS_DIR") else None,
        _SCRIPT_DIR,
        vault_root / ".claude" / "scripts",
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        candidate = Path(candidate)
        if (candidate / "event_action_launcher.py").is_file():
            return candidate
    return None


def import_kit(scripts_dir: Path):
    """Import event_action_launcher from the kit scripts directory.

    Importing it pulls in config_variables (which parses CONFIG.md at import
    time and hard-fails on a parse error) and run_lease. The caller decides
    what a failure means; this helper only raises.
    """
    path = str(Path(scripts_dir).resolve())
    if path not in sys.path:
        sys.path.insert(0, path)
    return importlib.import_module("event_action_launcher")


# ---------------------------------------------------------------------------
# Self-contained logging (used before the kit import is known-good, and for
# the log-head template — event_action_launcher's head is action-agent-only)
# ---------------------------------------------------------------------------

def log_line(code: str, target: str, value: str,
             *, now: Optional[datetime] = None) -> str:
    stamp = _format_ts(now or _now_utc())
    return f"{stamp} | {ACTOR} | {code} | {target} | {value}"


def append_own_log(log_path: Path, line: str,
                   *, stem: str, now: Optional[datetime] = None) -> None:
    """Append one line to the agent's event-log head, creating a properly
    tagged head when absent. Append-only; reuses the file's dominant newline
    so a CRLF log stays CRLF (the same discipline as
    event_action_launcher.append_log_line, generalized past its
    action-agent-only head template).
    """
    now = now or _now_utc()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        head = _LOG_HEAD.format(stem=stem, date=now.strftime("%Y-%m-%d"))
        with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(head)
    data = log_path.read_bytes()
    crlf = data.count(b"\r\n")
    newline = b"\r\n" if crlf > (data.count(b"\n") - crlf) else b"\n"
    prefix = b"" if (not data or data.endswith(b"\n")) else newline
    with open(log_path, "ab") as fh:
        fh.write(prefix + line.encode("utf-8") + newline)


# ---------------------------------------------------------------------------
# The launch pieces this launcher owns (deliberately NOT reused)
# ---------------------------------------------------------------------------

def build_command(claude_bin: str, max_budget_usd: float,
                  model: Optional[str] = None) -> list[str]:
    """The headless argv. The prompt arrives on stdin.

    No `--permission-mode`: the spawned run inherits the global
    `permissions.defaultMode` from `<user-home>/.claude/settings.json`
    (bypassPermissions) for parity with Desktop-fired scheduled agents —
    the one deliberate divergence from event_action_launcher.build_command.
    """
    cmd = [
        claude_bin,
        "-p",
        "--output-format", "json",
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]
    if model:
        cmd += ["--model", model]
    return cmd


def build_prompt(agent: str, skill_path: Path, vault_root: Path,
                 reasons: list[str]) -> str:
    """The stdin prompt: the deployed SKILL body under a fixed launch header.

    The header names the vault, the launch mode, and the gate reasons, so the
    run knows it was gate-fired on a real signal rather than a blind cadence.
    """
    body = skill_path.read_text(encoding="utf-8")
    gate_note = "; ".join(reasons) if reasons else "work"
    return (
        f"Run the {agent_full_name(agent)} pass now, unattended, following "
        f"the SKILL below verbatim.\n"
        f"vault-root: {vault_root}\n"
        f"launch: scheduled-gated ({ACTOR}); gate: {gate_note}\n"
        f"skill-source: {skill_path}\n\n"
        f"{body}"
    )


def record_fingerprint(fp_path: Path, fingerprint: dict) -> None:
    """Write the gate's fingerprint atomically (temp + replace).

    Called only after a spawn actually happened — this write is what turns
    the gate quiet, so it must never precede the run it stands for.
    """
    fp_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = fp_path.with_name(fp_path.name + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(fingerprint, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, fp_path)


def literal_drift(eal) -> list[str]:
    """The gate literals that do not match the live CONFIG token table.

    agent_gate is stdlib-only by design, so its folder literals are constants;
    this check (run where config_variables IS importable) turns a CONFIG
    folder rename into a visible `gate-literal-drift` forced-work instead of
    a silent watch of the wrong folder.
    """
    pairs = [
        ("inbox", agent_gate.INBOX_REL),
        ("user-queue", agent_gate.USER_QUEUE_REL),
        ("machine-queue", agent_gate.MACHINE_QUEUE_REL),
        ("logs", agent_gate.LOGS_REL),
    ]
    drift = []
    for token, literal in pairs:
        try:
            configured = eal.token_path(token, literal)
        except Exception:
            configured = literal
        if configured.replace("\\", "/") != literal:
            drift.append(f"<{token}>={configured!r} gate={literal!r}")
    return drift


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

@dataclass
class Outcome:
    """Everything one invocation decided, printed, and wrote."""
    exit_code: int = 0
    printed: list[str] = field(default_factory=list)
    gate: Optional[agent_gate.GateResult] = None
    command: list[str] = field(default_factory=list)
    spawned: bool = False
    pid: Optional[int] = None
    logged: str = ""
    fingerprint_recorded: bool = False


def run(vault_root: Path, agent: str, *,
        dry_run: bool = False,
        claude_bin: Optional[str] = None,
        model: Optional[str] = None,
        max_budget_usd: Optional[float] = None,
        skill_path: Optional[Path] = None,
        capture: Optional[Path] = None,
        wait: bool = False,
        expiry_hours: Optional[float] = None,
        lock_stale_seconds: float = _LOCK_STALE_SECONDS,
        scripts_dir: Optional[Path] = None,
        clock: Callable[[], datetime] = _now_utc,
        spawn_fn: Optional[Callable] = None,
        gate_fn: Optional[Callable] = None) -> Outcome:
    """Lock, lease, gate, then launch — or exit quietly with nothing written."""
    vault_root = Path(vault_root).resolve()
    out = Outcome()
    stem = agent_stem(agent)
    budget = (DEFAULT_BUDGETS_USD.get(agent, 10.00)
              if max_budget_usd is None else max_budget_usd)
    own_log = vault_root / agent_gate.LOGS_REL / stem / f"{stem}.md"
    last_capture = (vault_root / agent_gate.LOGS_REL /
                    f"gate-launcher-{stem}-last.json")

    def emit(code: str, target: str, value: str) -> None:
        line = log_line(code, target, value, now=clock())
        out.printed.append(line)
        print(line)

    # --- kit import (fail closed, loud) -----------------------------------
    kit_dir = locate_kit_scripts(vault_root, scripts_dir)
    if kit_dir is None:
        emit("launcher-error", stem,
             "event_action_launcher.py not found (looked at --scripts-dir, "
             "NOTE_KIT_SCRIPTS_DIR, this script's directory, "
             "<vault-root>/.claude/scripts); no lease visibility, no launch")
        try:
            append_own_log(own_log, out.printed[-1], stem=stem, now=clock())
        except OSError:
            pass
        out.exit_code = 1
        return out
    try:
        eal = import_kit(kit_dir)
    except Exception as exc:                     # config parse failures included
        emit("launcher-error", stem,
             f"kit import failed from {kit_dir}: {exc!r}; a broken CONFIG "
             f"halts every kit script — no lease visibility, no launch")
        try:
            append_own_log(own_log, out.printed[-1], stem=stem, now=clock())
        except OSError:
            pass
        out.exit_code = 1
        return out

    spawn = spawn_fn or eal.spawn_detached
    expiry = eal.DEFAULT_EXPIRY_HOURS if expiry_hours is None else expiry_hours
    lock_file = vault_root / agent_gate.LOGS_REL / f"gate-launcher-{agent}.lock"

    # --- 0. lock -----------------------------------------------------------
    guard = eal.acquire_launch_lock(
        lock_file, now=clock(), stale_after_seconds=lock_stale_seconds,
        target=stem)
    if not guard.acquired:
        emit("lock-defer", stem,
             f"another {ACTOR} invocation is live ({guard.describe()}); "
             f"no launch; the cadence retries")
        out.exit_code = 0
        return out
    try:
        if guard.took_over:
            emit("lock-takeover", stem,
                 f"stale guard displaced ({guard.took_over}); proceeding")
        if guard.unguarded:
            emit("lock-unguarded", stem,
                 f"{guard.unguarded}; proceeding without the guard")

        # --- 0b. previous spawn's outcome ----------------------------------
        # A detached spawn cannot report its own death, so each run reads the
        # prior spawn's captured result first. An errored result (an auth
        # failure, a refused turn) gets one visible log line, and both the
        # capture and the fingerprint clear — the fingerprint's spawn-time
        # record stood for a pass that never happened, so clearing it re-arms
        # the gated work instead of letting it starve behind a failed launch.
        if last_capture.is_file():
            try:
                prior = last_capture.read_text(encoding="utf-8",
                                               errors="replace")
            except OSError:
                prior = ""
            if '"is_error":true' in prior.replace(" ", ""):
                snippet = " ".join(prior.split())[:200]
                if dry_run:
                    emit("last-launch-errored", stem,
                         f"dry-run: previous spawn's captured result reports "
                         f"an error; a live run would log it, clear the "
                         f"fingerprint, and re-arm; {snippet}")
                else:
                    line = log_line(
                        "last-launch-errored", stem,
                        f"previous spawn's captured result reports an error; "
                        f"fingerprint cleared to re-arm; {snippet}",
                        now=clock())
                    out.printed.append(line)
                    print(line)
                    try:
                        append_own_log(own_log, line, stem=stem, now=clock())
                    except OSError:
                        pass
                    for stale in (
                        agent_gate.fingerprint_path_for(vault_root, agent),
                        last_capture,
                    ):
                        try:
                            stale.unlink()
                        except OSError:
                            pass

        # --- 1. lease ------------------------------------------------------
        lease = eal.read_lease_status(
            eal.lease_path_for(vault_root), now=clock(), expiry_hours=expiry)
        if lease.held:
            emit("lease-defer", stem,
                 f"{lease.describe()}; no launch, gate not consulted "
                 f"(mid-mutation state would mis-fingerprint); the cadence "
                 f"retries")
            out.exit_code = 0
            return out
        # A non-empty lease whose timestamp will not parse reads "expired"
        # from run_lease (anti-deadlock for an ACQUIRING run, which takes
        # over and logs what it displaces). A launcher only observes: an
        # illegible claim is still a claim, so it defers rather than
        # spawning beside whoever wrote it.
        if lease.state == "expired" and lease.started is None and lease.raw:
            emit("lease-illegible", stem,
                 f"non-empty lease with unparseable timestamp "
                 f"({lease.raw[:80]!r}); deferring — an acquiring run may "
                 f"take it over, a launcher never spawns past it")
            out.exit_code = 0
            return out

        # --- 2. gate -------------------------------------------------------
        evaluate = gate_fn or agent_gate.evaluate
        gate_error = ""
        try:
            gate = evaluate(vault_root, agent, now=clock())
        except Exception as exc:
            gate_error = f"gate-error {exc!r}"
            gate = agent_gate.GateResult(
                agent=agent, verdict="work", reasons=[gate_error],
                fingerprint={},
                fingerprint_path=agent_gate.fingerprint_path_for(
                    vault_root, agent))
        out.gate = gate

        drift = literal_drift(eal)
        if drift:
            gate.verdict = "work"
            gate.reasons.append("gate-literal-drift " + "; ".join(drift))

        if not gate.work:
            # The whole point of the gate: a quiet pass writes NOTHING — no
            # spawn, no log line, no fingerprint update. The print below is
            # stdout only (invisible under pythonw), for manual fires.
            emit("gate-quiet", stem,
                 "provably nothing to do; no spawn, nothing written")
            out.exit_code = 0
            return out

        # --- 3. launch -----------------------------------------------------
        resolved_bin = eal.resolve_claude_bin(claude_bin)
        if resolved_bin is None:
            emit("launch-failed", stem,
                 "claude CLI not found on PATH or at the WinGet shim")
            out.exit_code = 2
            return out
        skill = Path(skill_path) if skill_path else eal.deployed_skill_path(
            agent_full_name(agent))
        if not skill.is_file():
            emit("launch-failed", str(skill),
                 f"deployed SKILL missing; {agent_full_name(agent)} runs "
                 f"only from its deployed copy")
            out.exit_code = 2
            return out

        prompt = build_prompt(agent, skill, vault_root, gate.reasons)
        cmd = build_command(resolved_bin, budget, model)
        out.command = cmd
        prompt_bytes = len(prompt.encode("utf-8"))
        digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        flags = eal.creation_flags()
        shape = (f"cmd={subprocess.list2cmdline(cmd)}; cwd={vault_root}; "
                 f"creationflags=0x{flags:08x}"
                 f"{' (CREATE_NO_WINDOW)' if flags else ' (posix)'}; "
                 f"stdin={skill} ({prompt_bytes}B, sha256 {digest}); "
                 f"mode=inherited-from-settings; gate={'; '.join(gate.reasons)}")

        if dry_run:
            emit("dry-run", stem, f"would launch: {shape}; nothing written")
            out.exit_code = 0
            return out

        spawned = spawn(cmd, vault_root, prompt,
                        capture=capture if capture is not None else last_capture,
                        wait=wait)
        if spawned.error or spawned.pid is None:
            emit("launch-failed", stem,
                 f"{shape}; error={spawned.error or 'no pid'}")
            out.exit_code = 2
            return out

        out.spawned = True
        out.pid = spawned.pid

        # --- 4. record -----------------------------------------------------
        fp_note = "none"
        if gate.fingerprint and gate.fingerprint_path is not None:
            try:
                record_fingerprint(gate.fingerprint_path, gate.fingerprint)
                out.fingerprint_recorded = True
                fp_note = gate.fingerprint_path.name
            except OSError as exc:
                fp_note = f"unrecorded ({exc})"
        value = (f"pid={spawned.pid}; budget=${budget:.2f}; mode=inherit; "
                 f"gate={'; '.join(gate.reasons)}; fingerprint={fp_note}; "
                 f"prompt={prompt_bytes}B sha256 {digest}")
        if spawned.returncode is not None:
            value += f"; returncode={spawned.returncode}"
        line = log_line("gate-launch", stem, value, now=clock())
        append_own_log(own_log, line, stem=stem, now=clock())
        out.logged = line
        out.printed.append(line)
        print(line)
        if spawned.returncode not in (None, 0):
            out.exit_code = 1
        return out
    finally:
        eal.release_launch_lock(lock_file, guard)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Gated cron launcher for the note-kit scheduled agents: "
                    "lock, lease, deterministic gate, then spawn `claude -p` "
                    "against the deployed SKILL — or exit silently when the "
                    "gate proves there is nothing to do.")
    parser.add_argument("--vault-root", type=Path, default=None,
                        help="The vault root. Falls back to "
                             "NOTE_KIT_VAULT_ROOT; a scheduled registration "
                             "passes it explicitly.")
    parser.add_argument("--agent", required=True,
                        choices=sorted(agent_gate.AGENTS))
    parser.add_argument("--dry-run", action="store_true",
                        help="Print the launch argv and write nothing.")
    parser.add_argument("--claude-bin", default=None)
    parser.add_argument("--model", default=None,
                        help="Optional model override; default is the CLI's "
                             "own (top line) until CONFIG § Model tiers "
                             "assigns lanes.")
    parser.add_argument("--max-budget-usd", type=float, default=None,
                        help="Spend ceiling; default per agent: " +
                             ", ".join(f"{k} ${v:.2f}" for k, v in
                                       sorted(DEFAULT_BUDGETS_USD.items())))
    parser.add_argument("--skill", type=Path, default=None,
                        help="SKILL.md override; default: the deployed copy "
                             "under <user-home>/.claude/scheduled-tasks/.")
    parser.add_argument("--capture", type=Path, default=None,
                        help="Route the spawned run's output to this file.")
    parser.add_argument("--wait", action="store_true",
                        help="Block until the spawned pass exits.")
    parser.add_argument("--expiry-hours", type=float, default=None,
                        help="Lease expiry override (default: run_lease's 2).")
    parser.add_argument("--scripts-dir", type=Path, default=None,
                        help="Directory holding event_action_launcher.py "
                             "(default: NOTE_KIT_SCRIPTS_DIR, this script's "
                             "own directory, <vault-root>/.claude/scripts).")
    args = parser.parse_args(argv)

    vault_root = args.vault_root
    if vault_root is None and os.environ.get("NOTE_KIT_VAULT_ROOT"):
        vault_root = Path(os.environ["NOTE_KIT_VAULT_ROOT"])
    if vault_root is None or not Path(vault_root).is_dir():
        print(f"error: vault root is not a directory: {vault_root} "
              f"(pass --vault-root or set NOTE_KIT_VAULT_ROOT)",
              file=sys.stderr)
        return 2

    outcome = run(
        Path(vault_root), args.agent,
        dry_run=args.dry_run,
        claude_bin=args.claude_bin,
        model=args.model,
        max_budget_usd=args.max_budget_usd,
        skill_path=args.skill,
        capture=args.capture,
        wait=args.wait,
        expiry_hours=args.expiry_hours,
        scripts_dir=args.scripts_dir,
    )
    return outcome.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
