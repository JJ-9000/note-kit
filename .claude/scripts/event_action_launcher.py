#!/usr/bin/env python3
"""
event_action_launcher.py
========================

Start a headless action-agent pass from a queue event — the launch wrapper the
queue watcher (or any caller) invokes when `<user-queue>`, `<machine-queue>`, or
an `<inbox>`-root drop changes.

Trigger: invoked by the queue watcher on a queue event (the watcher itself is a
[[Note-Kit-UI-Quality-Plan]] change request); `--dry-run` is the default until
that watcher lands. The contract and every number below come from
`2026-07-26-event-driven-action-agent-research` § launch, § settle, and
§ collision.

Five steps, in order
--------------------
0. **Coalesce.** A single-instance guard at `<logs>/event-launcher.lock`, taken
   before anything else. The settle debounces the surfaces, not the launcher:
   four watcher events firing together start four invocations, each measuring
   quiet on its own, and all four reach the launch. The lockfile carries the
   holder's pid and start, so the first arrival walks settle → lease → launch
   and every later one prints a single `coalesce-defer` line and exits 0 — the
   fifteen-minute span now coalesces processes as well as events. A stale guard
   (dead pid, or age past the bounded wait) is displaced through a rename, which
   picks exactly one winner among simultaneous arrivals, and the takeover prints.
   The guard releases from a `finally`, so deferral, failure, and exception all
   free it.
1. **Settle.** Five minutes of mtime quiet on the watched surfaces, measured
   from the newest mtime across `<user-queue>`, `<machine-queue>`, and every
   frontmatter-less file at the `<inbox>` root (research § settle: the measured
   decision burst carried a 2m25s internal gap, so a shorter window hands the
   agent a half-answered queue). A surface still moving is waited out, bounded
   by the 15-minute coalescing span — every event inside that span belongs to
   one run, so the wait never outlives it. Reaching the bound proceeds and says
   so (`settle-timeout`).
2. **Lease.** Read `<logs>/run-lease.md` under CONFIG § Concurrency rule 1. A
   fresh lease (younger than 2 hours) held by any run means this launch exits 0
   with a printed deferral line and spawns nothing — the watcher or the sweep
   retries later, and no launch ever queue-jumps a live holder. An expired lease
   is left alone for the spawned run's own `RunLease` to take over and log. The
   launcher reads the lease; it never takes it.
3. **Launch.** `claude -p` against the deployed action-agent SKILL, with
   `--output-format json`, `--permission-mode`, and `--max-budget-usd` as the
   per-run ceiling, spawned with `CREATE_NO_WINDOW` so no console flashes (the
   vault's background-services convention). The prompt travels on stdin, so the
   SKILL body never meets a command-line length limit.
4. **Log.** One fixed-field line per launch appended to
   `<logs>/action-agent/action-agent.md` (CONFIG § Log files:
   `timestamp | actor | code | target | value`) — written only when a spawn
   actually happens.

Dry run is the default
----------------------
Spawning is gated behind `--spawn`. Without it the pass prints exactly what it
would run — argv, working directory, creation flags, prompt provenance and
digest — and writes nothing at all. The live wiring belongs to the watcher
build; a half-wired spawner that fires on its own would double-run agents beside
the hourly cron.

Usage
-----
    python event_action_launcher.py --vault-root <vault>            # dry run
    python event_action_launcher.py --event Inbox/User-Queue.md     # dry run
    python event_action_launcher.py --spawn                         # launches
    python event_action_launcher.py --spawn --wait --capture out.json
    python event_action_launcher.py --self-test

Run with no vault root resolvable and no args to execute the self-tests.

Every line this script prints is the § Log files shape, so a watcher capturing
stdout parses it with the same reader the event log uses.
"""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional, Sequence

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import resolve_vault_root, token_path  # noqa: E402
# The launcher reads the lease it never takes, so it reuses run_lease's own
# parse and expiry rather than restating the line format in a second place.
from run_lease import (  # noqa: E402
    DEFAULT_EXPIRY_HOURS,
    LeaseLine,
    _parse_lease_line,
)

ACTOR = "event-action-launcher"

# Research § settle: five minutes of mtime quiet, every event inside a
# fifteen-minute span coalescing into one run.
SETTLE_WINDOW_SECONDS = 300
COALESCE_SPAN_SECONDS = 900
_MIN_NAP_SECONDS = 1.0

# Research § economics: an acting pass runs $6.48 median / $6.89 mean, so a $10
# ceiling clears a normal acting pass and still stops a runaway.
DEFAULT_MAX_BUDGET_USD = 10.00

# The pass edits vault files unattended; the mode that grants the agent its full
# tool set is a deployment decision the watcher build makes with the owner, so
# the default here is the conservative one and `--permission-mode` overrides it.
DEFAULT_PERMISSION_MODE = "acceptEdits"

DEFAULT_AGENT = "note-kit-action-agent"
_LEASE_FILENAME = "run-lease.md"
_LOCK_FILENAME = "event-launcher.lock"
_LOCK_TAKEOVER_PROBES = 4
_WINGET_CLAUDE = Path.home() / "AppData/Local/Microsoft/WinGet/Links/claude.exe"

_LOG_HEAD = """---
type: log
tags:
  - action-agent
  - log
date: {date}
---

# action-agent event log

Append-only, one pipe-line per action: `timestamp | actor | code | target | value`.
"""


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_ts(dt: datetime) -> str:
    """UTC, minute precision, `Z` suffix — the stamp every kit log line opens with."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _rel(path: Path, root: Path) -> str:
    """`path` as a vault-relative POSIX string, or its full form when outside."""
    try:
        return str(path.relative_to(root)).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


# ---------------------------------------------------------------------------
# The watched surfaces
# ---------------------------------------------------------------------------

def has_frontmatter(path: Path) -> bool:
    """True when the file head opens `---` — the SKILL §1 drop discriminator.

    An AI draft always carries frontmatter, so its absence marks a user drop. A
    file that will not open reads as frontmattered, keeping an unreadable
    fragment out of the trigger set.

    Six bytes are read so a UTF-8 BOM can be stripped and three real bytes still
    remain to test — an editor that stamps the BOM writes `EF BB BF 2D 2D 2D`,
    and a shorter read leaves too little behind to ever match.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(6)
    except OSError:
        return True
    if head.startswith(b"\xef\xbb\xbf"):     # UTF-8 BOM
        head = head[3:]
    return head.startswith(b"---")


def watched_surfaces(vault_root: Path) -> list[Path]:
    """Every surface a queue event can arrive on (research § triggers).

    `<user-queue>`, `<machine-queue>`, and each frontmatter-less file at the
    `<inbox>` root. Subfolders are the filing-agent's and stay out; dotfiles are
    host litter, never a drop.
    """
    surfaces: list[Path] = []
    uq = vault_root / token_path("user-queue", "Inbox/User-Queue.md")
    mq = vault_root / token_path("machine-queue", "Inbox/Machine-Queue.md")
    for queue in (uq, mq):
        if queue.is_file():
            surfaces.append(queue)

    inbox = vault_root / token_path("inbox", "Inbox")
    if inbox.is_dir():
        queue_names = {uq.name.lower(), mq.name.lower()}
        for child in sorted(inbox.iterdir()):
            if not child.is_file() or child.name.startswith("."):
                continue
            if child.name.lower() in queue_names:
                continue
            if has_frontmatter(child):
                continue
            surfaces.append(child)
    return surfaces


def newest_surface(vault_root: Path) -> tuple[Optional[Path], Optional[float]]:
    """The most recently modified watched surface and its mtime, or (None, None)."""
    newest: Optional[Path] = None
    newest_mtime: Optional[float] = None
    for path in watched_surfaces(vault_root):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        if newest_mtime is None or mtime > newest_mtime:
            newest, newest_mtime = path, mtime
    return newest, newest_mtime


# ---------------------------------------------------------------------------
# Step 0 — the coalescing guard
#
# The settle debounces the surfaces; it does not debounce the launcher. Four
# watcher events firing at once start four invocations, each of which measures
# quiet on its own and reaches the launch — so the coalescing span holds only
# for one process at a time. A lockfile beside the lease carries the pid and
# the start of whichever invocation is already walking settle → lease → launch:
# a live holder turns every later arrival into one printed defer line at exit 0,
# and the invocations collapse into the single run the span promises.
# ---------------------------------------------------------------------------

_LOCK_RE = re.compile(r"pid=(\d+)\s+epoch=([0-9.]+)")
_STILL_ACTIVE = 259
_ERROR_INVALID_PARAMETER = 87


def pid_alive(pid: int) -> bool:
    """True while `pid` names a running process.

    Windows takes the `OpenProcess` route rather than `os.kill(pid, 0)`, because
    CPython maps that call to `TerminateProcess` there — the liveness probe would
    kill the very holder it asked about. An access-denied handle means the
    process exists under another owner, which counts as alive.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return True
        return True
    try:
        kernel32 = ctypes.windll.kernel32           # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() != _ERROR_INVALID_PARAMETER
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                # a probe that cannot run
        return True                                  # reads the holder as alive


@dataclass
class LockGuard:
    """What the coalescing guard decided, and what release owes the filesystem."""
    acquired: bool                   # True to walk settle → lease → launch
    pid: int = 0                     # this invocation's pid
    owns_file: bool = False          # True when release deletes the lockfile
    holder_pid: Optional[int] = None  # the live holder that turned this away
    holder_age: Optional[float] = None
    took_over: str = ""              # a stale holder this invocation displaced
    unguarded: str = ""              # why the guard could not be written

    def describe(self) -> str:
        age = "unknown" if self.holder_age is None else f"{int(self.holder_age)}s"
        return f"holder pid={self.holder_pid or 'unknown'}; age={age}"


def lock_path_for(vault_root: Path) -> Path:
    return vault_root / token_path("logs", "Archive/Logs") / _LOCK_FILENAME


def _lock_text(pid: int, now: datetime, target: str) -> str:
    """One log-shaped line: the fixed five fields, with pid and epoch parsable."""
    return (f"{_format_ts(now)} | {ACTOR} | lock-held | "
            f"pid={pid} epoch={now.timestamp():.3f} | {target}\n")


def _read_lock(lock_path: Path) -> tuple[Optional[int], Optional[float]]:
    """The holder's pid and start epoch, or (None, None) for absent or garbled."""
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except OSError:
        return None, None
    match = _LOCK_RE.search(raw)
    if match is None:
        return None, None
    try:
        return int(match.group(1)), float(match.group(2))
    except ValueError:
        return None, None


def acquire_launch_lock(
    lock_path: Path,
    *,
    now: Optional[datetime] = None,
    stale_after_seconds: float = COALESCE_SPAN_SECONDS,
    target: str = "",
    pid: Optional[int] = None,
) -> LockGuard:
    """Take the single-instance guard, defer to a live holder, or take over a stale one.

    A holder is live while its pid runs and its age sits inside the bounded wait
    — the longest an honest invocation can spend before it launches. A dead pid,
    an age past that bound, or a line that will not parse is stale: this
    invocation displaces it and says so. The displacement goes through a rename,
    so exactly one of several simultaneous arrivals claims a stale lock.

    A lockfile that cannot be written at all (an unwritable `<logs>`) returns
    acquired with `unguarded` set: the pass proceeds as it did before the guard
    existed rather than blocking every launch on a filesystem fault.
    """
    now = now or _now_utc()
    pid = os.getpid() if pid is None else pid
    took_over = ""
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return LockGuard(True, pid, unguarded=f"cannot create {lock_path.parent}: {exc}")

    for _ in range(_LOCK_TAKEOVER_PROBES):
        try:
            fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            pass
        except OSError as exc:
            return LockGuard(True, pid, unguarded=f"cannot write {lock_path}: {exc}")
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(_lock_text(pid, now, target))
            return LockGuard(True, pid, owns_file=True, took_over=took_over)

        holder_pid, holder_epoch = _read_lock(lock_path)
        age = None if holder_epoch is None else now.timestamp() - holder_epoch
        live = (holder_pid is not None and age is not None
                and age < stale_after_seconds and pid_alive(holder_pid))
        if live:
            return LockGuard(False, pid, holder_pid=holder_pid, holder_age=age)

        # Stale — displace it by moving it aside; the rename is the race winner.
        aside = lock_path.with_name(f"{lock_path.name}.stale-{pid}")
        why = ("unparsable holder line" if holder_pid is None
               else f"pid {holder_pid} gone" if not pid_alive(holder_pid)
               else f"pid {holder_pid} held {int(age or 0)}s past the "
                    f"{int(stale_after_seconds)}s bound")
        try:
            os.replace(lock_path, aside)
        except OSError:
            continue                     # another arrival displaced it first
        took_over = why
        try:
            aside.unlink()
        except OSError:
            pass

    return LockGuard(False, pid, holder_pid=_read_lock(lock_path)[0],
                     holder_age=None)


def release_launch_lock(lock_path: Path, guard: LockGuard) -> None:
    """Drop the guard this invocation wrote, and nothing else.

    Called from a `finally`, so every exit path — deferral, failure, exception —
    frees the lock. A lock some other pid now holds is left alone: a stale
    takeover already moved this invocation's file aside.
    """
    if not guard.owns_file:
        return
    holder_pid, _ = _read_lock(lock_path)
    if holder_pid not in (None, guard.pid):
        return
    try:
        lock_path.unlink()
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Step 1 — settle
# ---------------------------------------------------------------------------

@dataclass
class SettleResult:
    """What the debounce did before handing control on."""
    settled: bool                    # True on quiet reached, False at the wait bound
    waited_seconds: float = 0.0
    quiet_seconds: float = 0.0
    checks: int = 0
    newest: Optional[Path] = None

    @property
    def code(self) -> str:
        return "settle-quiet" if self.settled else "settle-timeout"


def settle(
    vault_root: Path,
    *,
    window_seconds: float = SETTLE_WINDOW_SECONDS,
    max_wait_seconds: float = COALESCE_SPAN_SECONDS,
    clock: Callable[[], datetime] = _now_utc,
    sleeper: Callable[[float], None] = time.sleep,
) -> SettleResult:
    """Wait until the watched surfaces hold still, bounded by the coalescing span.

    Quiet is measured from the newest mtime across every watched surface, so a
    burst that moves from the queue to a drop and back keeps the window open.
    No surface at all reads as quiet — there is nothing left to wait for.
    """
    start = clock()
    checks = 0
    while True:
        newest, mtime = newest_surface(vault_root)
        now = clock()
        checks += 1
        quiet = float("inf") if mtime is None else now.timestamp() - mtime
        waited = (now - start).total_seconds()
        if quiet >= window_seconds:
            return SettleResult(True, waited, quiet, checks, newest)
        remaining = max_wait_seconds - waited
        if remaining <= 0:
            return SettleResult(False, waited, quiet, checks, newest)
        nap = max(min(window_seconds - quiet, remaining), _MIN_NAP_SECONDS)
        sleeper(nap)


# ---------------------------------------------------------------------------
# Step 2 — lease
# ---------------------------------------------------------------------------

@dataclass
class LeaseStatus:
    """The run lease as this launch found it, without touching the file."""
    state: str                       # "free" | "held" | "expired"
    holder: str = ""
    started: Optional[datetime] = None
    age_seconds: Optional[float] = None
    raw: str = ""

    @property
    def held(self) -> bool:
        return self.state == "held"

    def describe(self) -> str:
        if self.state == "free":
            return "lease=free"
        age = "unknown" if self.age_seconds is None else f"{int(self.age_seconds // 60)}m"
        return f"lease={self.state}; holder={self.holder or 'unknown'}; age={age}"


def lease_path_for(vault_root: Path) -> Path:
    return vault_root / token_path("logs", "Archive/Logs") / _LEASE_FILENAME


def read_lease_status(
    lease_path: Path,
    *,
    now: Optional[datetime] = None,
    expiry_hours: float = DEFAULT_EXPIRY_HOURS,
) -> LeaseStatus:
    """Read the lease under CONFIG § Concurrency rule 1 — read-only, never taken.

    An empty or absent file is free. An active line younger than the expiry
    window is held. An older line, or one whose stamp will not parse, is expired
    — the spawned run's own `RunLease` takes it over and logs the takeover, so
    the launcher leaves it exactly as it found it.
    """
    now = now or _now_utc()
    if not lease_path.is_file():
        return LeaseStatus("free")
    try:
        raw = lease_path.read_text(encoding="utf-8")
    except OSError:
        return LeaseStatus("free")

    active: Optional[LeaseLine] = None
    for line in raw.splitlines():
        if line.strip():
            active = _parse_lease_line(line)
            break
    if active is None:
        return LeaseStatus("free")

    if active.timestamp is None:
        return LeaseStatus("expired", active.agent, None, None, active.raw)
    age = (now - active.timestamp).total_seconds()
    state = "held" if age < expiry_hours * 3600 else "expired"
    return LeaseStatus(state, active.agent, active.timestamp, age, active.raw)


# ---------------------------------------------------------------------------
# Step 3 — the launch
# ---------------------------------------------------------------------------

def deployed_skill_path(agent: str = DEFAULT_AGENT) -> Path:
    """`<user-home>/.claude/scheduled-tasks/<agent>/SKILL.md`.

    Scheduled agents run only from the deployed copy (CONFIG § Self-modification),
    so an event-fired run reads the same file the cron pass does.
    """
    return Path.home() / ".claude" / "scheduled-tasks" / agent / "SKILL.md"


def resolve_claude_bin(explicit: Optional[str] = None) -> Optional[str]:
    """The headless launch binary: an explicit path, PATH, then the WinGet shim."""
    if explicit:
        return explicit
    found = shutil.which("claude")
    if found:
        return found
    return str(_WINGET_CLAUDE) if _WINGET_CLAUDE.exists() else None


def build_prompt(
    skill_path: Path,
    vault_root: Path,
    *,
    trigger: Optional[Path] = None,
    settle_result: Optional[SettleResult] = None,
) -> str:
    """The stdin prompt: the deployed SKILL body under a fixed launch header.

    The header names the vault, the triggering surface, and the settle state, so
    the run knows it was event-fired rather than scheduled and can log the
    difference. The SKILL travels whole — the run reads its instructions from the
    prompt instead of depending on a lookup.
    """
    body = skill_path.read_text(encoding="utf-8")
    trigger_line = _rel(trigger, vault_root) if trigger else "unspecified"
    quiet = "unknown"
    if settle_result is not None:
        quiet = ("no watched surface" if settle_result.quiet_seconds == float("inf")
                 else f"{int(settle_result.quiet_seconds)}s quiet")
    return (
        f"Run the {DEFAULT_AGENT} pass now, unattended, following the SKILL below verbatim.\n"
        f"vault-root: {vault_root}\n"
        f"launch: event-driven ({ACTOR}); trigger: {trigger_line}; settle: {quiet}\n"
        f"skill-source: {skill_path}\n\n"
        f"{body}"
    )


def build_command(
    *,
    claude_bin: str,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    model: Optional[str] = None,
    extra: Sequence[str] = (),
) -> list[str]:
    """The headless argv (research § launch). The prompt arrives on stdin."""
    cmd = [
        claude_bin,
        "-p",
        "--output-format", "json",
        "--permission-mode", permission_mode,
        "--max-budget-usd", f"{max_budget_usd:.2f}",
    ]
    if model:
        cmd += ["--model", model]
    cmd += list(extra)
    return cmd


@dataclass
class SpawnResult:
    """What the spawn produced."""
    pid: Optional[int] = None
    returncode: Optional[int] = None
    error: str = ""


def creation_flags() -> int:
    """`CREATE_NO_WINDOW` on Windows, 0 elsewhere — no console ever flashes."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def spawn_detached(
    cmd: Sequence[str],
    cwd: Path,
    prompt: str,
    *,
    capture: Optional[Path] = None,
    wait: bool = False,
) -> SpawnResult:
    """Start the headless run and hand its prompt over on stdin.

    Detached by default: the launcher writes the prompt, closes stdin, and
    returns the pid, so a watcher never blocks for the length of a pass.
    `--wait` blocks and reports the exit code instead. Output goes nowhere
    unless `capture` names a file.
    """
    sink = None
    try:
        if capture is not None:
            capture.parent.mkdir(parents=True, exist_ok=True)
            sink = open(capture, "wb")
            stdout = sink
        else:
            stdout = subprocess.DEVNULL
        proc = subprocess.Popen(
            list(cmd),
            cwd=str(cwd),
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=subprocess.STDOUT,
            creationflags=creation_flags(),
            close_fds=True,
        )
        assert proc.stdin is not None
        proc.stdin.write(prompt.encode("utf-8"))
        proc.stdin.close()
        if wait:
            return SpawnResult(proc.pid, proc.wait())
        return SpawnResult(proc.pid, None)
    except OSError as exc:
        return SpawnResult(None, None, str(exc))
    finally:
        if sink is not None and not wait:
            sink.close()
        elif sink is not None:
            sink.close()


# ---------------------------------------------------------------------------
# Step 4 — the log line
# ---------------------------------------------------------------------------

def action_log_path(vault_root: Path, agent: str = DEFAULT_AGENT) -> Path:
    """`<logs>/action-agent/action-agent.md` — the agent's own append-only head."""
    stem = agent[len("note-kit-"):] if agent.startswith("note-kit-") else agent
    return vault_root / token_path("logs", "Archive/Logs") / stem / f"{stem}.md"


def append_log_line(log_path: Path, line: str, *, now: Optional[datetime] = None) -> None:
    """Append one line to the event-log head, creating the head when absent.

    Append-only: existing bytes are never rewritten, and the file's own dominant
    line ending is reused so a CRLF log stays CRLF.
    """
    now = now or _now_utc()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if not log_path.exists():
        head = _LOG_HEAD.format(date=now.strftime("%Y-%m-%d"))
        with open(log_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(head)
    data = log_path.read_bytes()
    crlf = data.count(b"\r\n")
    newline = b"\r\n" if crlf > (data.count(b"\n") - crlf) else b"\n"
    prefix = b"" if (not data or data.endswith(b"\n")) else newline
    with open(log_path, "ab") as fh:
        fh.write(prefix + line.encode("utf-8") + newline)


def log_line(code: str, target: str, value: str, *, now: Optional[datetime] = None) -> str:
    """One fixed-field line: `timestamp | actor | code | target | value`."""
    stamp = _format_ts(now or _now_utc())
    return f"{stamp} | {ACTOR} | {code} | {target} | {value}"


# ---------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------

@dataclass
class LaunchOutcome:
    """Everything one invocation decided, printed, and wrote."""
    exit_code: int = 0
    printed: list[str] = field(default_factory=list)
    settle: Optional[SettleResult] = None
    lease: Optional[LeaseStatus] = None
    command: list[str] = field(default_factory=list)
    prompt_bytes: int = 0
    prompt_digest: str = ""
    spawned: bool = False
    pid: Optional[int] = None
    logged: str = ""
    lock: Optional[LockGuard] = None


def run(
    vault_root: Path,
    *,
    event: Optional[Path] = None,
    spawn: bool = False,
    wait: bool = False,
    capture: Optional[Path] = None,
    window_seconds: float = SETTLE_WINDOW_SECONDS,
    max_wait_seconds: float = COALESCE_SPAN_SECONDS,
    expiry_hours: float = DEFAULT_EXPIRY_HOURS,
    agent: str = DEFAULT_AGENT,
    skill_path: Optional[Path] = None,
    claude_bin: Optional[str] = None,
    permission_mode: str = DEFAULT_PERMISSION_MODE,
    max_budget_usd: float = DEFAULT_MAX_BUDGET_USD,
    model: Optional[str] = None,
    show_prompt: bool = False,
    lock_path: Optional[Path] = None,
    clock: Callable[[], datetime] = _now_utc,
    sleeper: Callable[[float], None] = time.sleep,
    spawn_fn: Callable[..., SpawnResult] = spawn_detached,
) -> LaunchOutcome:
    """Coalesce, settle, check the lease, then launch (or print the launch and stop)."""
    # Absolute from here down: the spawned run's working directory and every
    # path this pass prints or logs read the same whatever the caller passed.
    vault_root = Path(vault_root).resolve()
    out = LaunchOutcome()
    skill = skill_path or deployed_skill_path(agent)
    lock_file = Path(lock_path) if lock_path else lock_path_for(vault_root)

    def emit(code: str, target: str, value: str) -> None:
        line = log_line(code, target, value, now=clock())
        out.printed.append(line)
        print(line)

    # --- 0. coalesce ------------------------------------------------------
    # Ahead of the settle, so simultaneous invocations collapse before any of
    # them starts measuring quiet on its own.
    early_target = (_rel(Path(event), vault_root) if event
                    else token_path("inbox", "Inbox"))
    guard = acquire_launch_lock(
        lock_file, now=clock(), stale_after_seconds=max_wait_seconds,
        target=early_target)
    out.lock = guard
    if not guard.acquired:
        emit("coalesce-defer", early_target,
             f"another invocation is already settling ({guard.describe()}); "
             f"no launch; this event coalesces into that run")
        out.exit_code = 0
        return out
    if guard.took_over:
        emit("lock-takeover", _rel(lock_file, vault_root),
             f"stale guard displaced ({guard.took_over}); this invocation proceeds")
    if guard.unguarded:
        emit("lock-unguarded", _rel(lock_file, vault_root),
             f"{guard.unguarded}; proceeding without the coalescing guard")

    try:
        return _run_guarded(
            vault_root, out=out, emit=emit, skill=skill, event=event, spawn=spawn,
            wait=wait, capture=capture, window_seconds=window_seconds,
            max_wait_seconds=max_wait_seconds, expiry_hours=expiry_hours,
            agent=agent, claude_bin=claude_bin, permission_mode=permission_mode,
            max_budget_usd=max_budget_usd, model=model, show_prompt=show_prompt,
            clock=clock, sleeper=sleeper, spawn_fn=spawn_fn,
        )
    finally:
        release_launch_lock(lock_file, guard)


def _run_guarded(
    vault_root: Path,
    *,
    out: LaunchOutcome,
    emit: Callable[[str, str, str], None],
    skill: Path,
    event: Optional[Path],
    spawn: bool,
    wait: bool,
    capture: Optional[Path],
    window_seconds: float,
    max_wait_seconds: float,
    expiry_hours: float,
    agent: str,
    claude_bin: Optional[str],
    permission_mode: str,
    max_budget_usd: float,
    model: Optional[str],
    show_prompt: bool,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    spawn_fn: Callable[..., SpawnResult],
) -> LaunchOutcome:
    """Settle, lease, launch — the three steps the coalescing guard is held across."""
    # --- 1. settle --------------------------------------------------------
    result = settle(
        vault_root,
        window_seconds=window_seconds,
        max_wait_seconds=max_wait_seconds,
        clock=clock,
        sleeper=sleeper,
    )
    out.settle = result
    trigger = event or result.newest
    target = _rel(trigger, vault_root) if trigger else token_path("inbox", "Inbox")
    quiet = ("no watched surface" if result.quiet_seconds == float("inf")
             else f"{int(result.quiet_seconds)}s")
    settle_note = (f"quiet={quiet}; waited={int(result.waited_seconds)}s; "
                   f"window={int(window_seconds)}s; checks={result.checks}")
    if not result.settled:
        emit("settle-timeout", target,
             f"{settle_note}; proceeding at the {int(max_wait_seconds)}s coalescing bound")
    elif result.checks > 1:
        # A pass that found the surfaces already quiet says nothing about the
        # settle — the check count, not the wall-clock delta, is what marks a
        # real wait, since a single-check pass still spends a few milliseconds.
        emit("settle-wait", target, settle_note)

    # --- 2. lease ---------------------------------------------------------
    lease = read_lease_status(
        lease_path_for(vault_root), now=clock(), expiry_hours=expiry_hours)
    out.lease = lease
    if lease.held:
        emit("lease-defer", target,
             f"{lease.describe()}; no launch; the watcher or the sweep retries")
        out.exit_code = 0
        return out

    # --- 3. the command ---------------------------------------------------
    resolved_bin = resolve_claude_bin(claude_bin)
    if resolved_bin is None:
        emit("launch-failed", target,
             "claude CLI not found on PATH or at the WinGet shim")
        out.exit_code = 2
        return out
    if not skill.is_file():
        emit("launch-failed", _rel(skill, vault_root),
             f"deployed SKILL missing; {agent} runs only from its deployed copy")
        out.exit_code = 2
        return out

    prompt = build_prompt(skill, vault_root, trigger=trigger, settle_result=result)
    cmd = build_command(
        claude_bin=resolved_bin,
        permission_mode=permission_mode,
        max_budget_usd=max_budget_usd,
        model=model,
    )
    out.command = cmd
    out.prompt_bytes = len(prompt.encode("utf-8"))
    out.prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    flags = creation_flags()
    shape = (f"cmd={subprocess.list2cmdline(cmd)}; cwd={vault_root}; "
             f"creationflags=0x{flags:08x}"
             f"{' (CREATE_NO_WINDOW)' if flags else ' (posix)'}; "
             f"stdin={_rel(skill, vault_root)} "
             f"({out.prompt_bytes}B, sha256 {out.prompt_digest}); "
             f"{lease.describe()}")

    if show_prompt:
        print(prompt)

    # --- 4. launch or print ----------------------------------------------
    if not spawn:
        emit("dry-run", target, f"would launch: {shape}; nothing written")
        return out

    spawned = spawn_fn(cmd, vault_root, prompt, capture=capture, wait=wait)
    if spawned.error or spawned.pid is None:
        emit("launch-failed", target, f"{shape}; error={spawned.error or 'no pid'}")
        out.exit_code = 2
        return out

    out.spawned = True
    out.pid = spawned.pid
    value = (f"pid={spawned.pid}; budget=${max_budget_usd:.2f}; "
             f"mode={permission_mode}; {settle_note}; {lease.describe()}; "
             f"prompt={out.prompt_bytes}B sha256 {out.prompt_digest}")
    if spawned.returncode is not None:
        value += f"; returncode={spawned.returncode}"
    line = log_line("event-launch", target, value, now=clock())
    append_log_line(action_log_path(vault_root, agent), line, now=clock())
    out.logged = line
    out.printed.append(line)
    print(line)
    if spawned.returncode not in (None, 0):
        out.exit_code = 1
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch a headless action-agent pass on a queue event "
                    "(settle, lease, launch, log). Dry run by default.")
    parser.add_argument("--vault-root", type=Path, default=None)
    parser.add_argument("--event", type=Path, default=None, metavar="PATH",
                        help="The surface whose change triggered this launch. "
                             "Default: the newest watched surface.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--spawn", action="store_true",
                      help="Actually launch the pass. Without it, dry-run.")
    mode.add_argument("--dry-run", action="store_true",
                      help="Print the launch and write nothing (the default).")
    parser.add_argument("--wait", action="store_true",
                        help="Block until the spawned pass exits and report its code.")
    parser.add_argument("--capture", type=Path, default=None, metavar="PATH",
                        help="Route the spawned run's output to this file.")
    parser.add_argument("--settle-seconds", type=float, default=SETTLE_WINDOW_SECONDS)
    parser.add_argument("--max-wait-seconds", type=float, default=COALESCE_SPAN_SECONDS)
    parser.add_argument("--expiry-hours", type=float, default=DEFAULT_EXPIRY_HOURS)
    parser.add_argument("--agent", default=DEFAULT_AGENT)
    parser.add_argument("--skill", type=Path, default=None,
                        help="Deployed SKILL.md. Default: the agent's deployed copy.")
    parser.add_argument("--claude-bin", default=None)
    parser.add_argument("--permission-mode", default=DEFAULT_PERMISSION_MODE)
    parser.add_argument("--max-budget-usd", type=float, default=DEFAULT_MAX_BUDGET_USD)
    parser.add_argument("--model", default=None)
    parser.add_argument("--show-prompt", action="store_true",
                        help="Print the full stdin prompt before the verdict line.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_tests()

    vault_root = resolve_vault_root(args.vault_root)
    if vault_root is None or not Path(vault_root).is_dir():
        if args.vault_root is None:
            return _run_self_tests()
        sys.exit(f"Error: vault root is not a directory: {vault_root}")

    outcome = run(
        Path(vault_root),
        event=args.event,
        spawn=args.spawn,
        wait=args.wait,
        capture=args.capture,
        window_seconds=args.settle_seconds,
        max_wait_seconds=args.max_wait_seconds,
        expiry_hours=args.expiry_hours,
        agent=args.agent,
        skill_path=args.skill,
        claude_bin=args.claude_bin,
        permission_mode=args.permission_mode,
        max_budget_usd=args.max_budget_usd,
        model=args.model,
        show_prompt=args.show_prompt,
    )
    return outcome.exit_code


# ---------------------------------------------------------------------------
# Self-tests — settle, lease, and dry-run paths, on a scratch tree.
# No test spawns a real agent: every launch runs through an injected spawner.
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    import tempfile

    failures = 0

    def fail(msg: str) -> None:
        nonlocal failures
        print(f"FAIL {msg}")
        failures += 1

    base_now = datetime(2026, 7, 26, 21, 0, tzinfo=timezone.utc)
    logs_rel = token_path("logs", "Archive/Logs")
    inbox_rel = token_path("inbox", "Inbox")

    class FakeClock:
        """A clock the tests advance by hand; the sleeper moves it."""

        def __init__(self, start: datetime) -> None:
            self.now = start

        def __call__(self) -> datetime:
            return self.now

        def advance(self, seconds: float) -> None:
            self.now += timedelta(seconds=seconds)

    def build_vault(tmp: Path, *, ages: dict[str, float], lease: Optional[str] = None,
                    now: datetime = base_now) -> Path:
        """A scratch vault whose surfaces carry the given ages, in seconds."""
        vault = tmp
        (vault / inbox_rel).mkdir(parents=True, exist_ok=True)
        (vault / logs_rel).mkdir(parents=True, exist_ok=True)
        for name, age in ages.items():
            path = vault / inbox_rel / name
            path.write_text("- [x] a decision\n", encoding="utf-8")
            stamp = now.timestamp() - age
            os.utime(path, (stamp, stamp))
        if lease is not None:
            (vault / logs_rel / _LEASE_FILENAME).write_text(lease, encoding="utf-8")
        return vault

    fake_skill_body = "---\nname: note-kit-action-agent\n---\n\n# note-kit-action-agent\n"

    def fake_skill(vault: Path) -> Path:
        path = vault / "skill" / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(fake_skill_body, encoding="utf-8")
        return path

    class RecordingSpawner:
        """Stands in for the real spawn; records the call, never starts a process."""

        def __init__(self) -> None:
            self.calls: list[dict] = []

        def __call__(self, cmd, cwd, prompt, *, capture=None, wait=False) -> SpawnResult:
            self.calls.append({"cmd": list(cmd), "cwd": Path(cwd), "prompt": prompt,
                               "capture": capture, "wait": wait})
            return SpawnResult(pid=4242, returncode=0 if wait else None)

    # --- A: the surface set is the queues plus frontmatter-less root files ---
    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        inbox = vault / inbox_rel
        inbox.mkdir(parents=True)
        (inbox / "User-Queue.md").write_text("- [x] decided\n", encoding="utf-8")
        (inbox / "Machine-Queue.md").write_text("- [ ] do it\n", encoding="utf-8")
        (inbox / "drop.md").write_text("bare user drop\n", encoding="utf-8")
        (inbox / "Draft.md").write_text("---\ntype: note\n---\nbody\n", encoding="utf-8")
        (inbox / ".DS_Store").write_text("litter", encoding="utf-8")
        (inbox / "Container").mkdir()
        (inbox / "Container" / "member.md").write_text("member\n", encoding="utf-8")

        names = sorted(p.name for p in watched_surfaces(vault))
        if names != ["Machine-Queue.md", "User-Queue.md", "drop.md"]:
            fail(f"A watched surfaces {names}")
        elif has_frontmatter(inbox / "Draft.md") is not True or \
                has_frontmatter(inbox / "drop.md") is not False:
            fail("A the frontmatter discriminator misread a file")
        else:
            print("OK   case A: the queues and the bare drop are watched; a "
                  "frontmattered draft, a subfolder, and a dotfile are not")

    # --- B: a hot surface is waited out, then proceeds on quiet -------------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 60.0})
        naps: list[float] = []

        def sleeper(seconds: float) -> None:
            naps.append(seconds)
            clock.advance(seconds)

        res = settle(vault, clock=clock, sleeper=sleeper)
        if not res.settled:
            fail("B a settling surface reported timeout")
        if not naps or abs(sum(naps) - 240.0) > 1.0:
            fail(f"B waited {sum(naps)}s, expected the 240s remainder of the window")
        if res.checks != 2:
            fail(f"B checked {res.checks} times, expected 2")
        if abs(res.quiet_seconds - 300.0) > 1.0:
            fail(f"B proceeded at {res.quiet_seconds}s quiet")
        if not failures:
            print("OK   case B: a surface touched 60s ago waits the remaining "
                  "240s of the 5-minute window, then proceeds")

    # --- C: a surface that never settles proceeds at the coalescing bound ---
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 5.0})
        queue = vault / inbox_rel / "User-Queue.md"

        def restless(seconds: float) -> None:
            clock.advance(seconds)
            stamp = clock.now.timestamp() - 5.0   # the user keeps typing
            os.utime(queue, (stamp, stamp))

        res = settle(vault, clock=clock, sleeper=restless)
        if res.settled:
            fail("C a never-quiet surface reported settled")
        if res.waited_seconds < COALESCE_SPAN_SECONDS:
            fail(f"C bailed early at {res.waited_seconds}s")
        if res.waited_seconds > COALESCE_SPAN_SECONDS + _MIN_NAP_SECONDS:
            fail(f"C overran the bound at {res.waited_seconds}s")
        if res.code != "settle-timeout":
            fail(f"C code {res.code}")
        if not failures:
            print("OK   case C: a surface that never quiets proceeds at the "
                  f"{COALESCE_SPAN_SECONDS}s coalescing bound, flagged settle-timeout")

    # --- D: a fresh lease defers — exit 0, nothing spawned, nothing logged --
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        fresh = "2026-07-26T20:30Z | orchestrator-session | lease-taken | live work\n"
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0}, lease=fresh)
        spawner = RecordingSpawner()
        out = run(vault, spawn=True, skill_path=fake_skill(vault),
                  claude_bin="claude", clock=clock, sleeper=lambda s: clock.advance(s),
                  spawn_fn=spawner)
        log = action_log_path(vault)
        if out.exit_code != 0:
            fail(f"D deferral exited {out.exit_code}, expected 0")
        if not out.lease or not out.lease.held:
            fail(f"D lease read {out.lease}")
        if spawner.calls:
            fail("D a launch happened while another run held a fresh lease")
        if log.exists():
            fail("D a deferral wrote to the event log")
        if not any("lease-defer" in line for line in out.printed):
            fail(f"D no deferral line printed: {out.printed}")
        if not failures:
            print("OK   case D: a fresh lease held by another run defers: "
                  "exit 0, one printed line, no spawn, no log write")

    # --- E: an expired lease does not block the launch ---------------------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        stale = "2026-07-26T18:00Z | note-kit-janitor-agent | lease-taken | daily pass\n"
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0}, lease=stale)
        spawner = RecordingSpawner()
        out = run(vault, spawn=True, skill_path=fake_skill(vault),
                  claude_bin="claude", clock=clock, sleeper=lambda s: clock.advance(s),
                  spawn_fn=spawner)
        lease_file = vault / logs_rel / _LEASE_FILENAME
        if not out.lease or out.lease.state != "expired":
            fail(f"E lease state {out.lease}")
        if len(spawner.calls) != 1:
            fail(f"E {len(spawner.calls)} launches on an expired lease, expected 1")
        if lease_file.read_text(encoding="utf-8") != stale:
            fail("E the launcher rewrote the lease it only reads")
        if not failures:
            print("OK   case E: a lease past the 2-hour expiry launches, and the "
                  "launcher leaves the lease file untouched for the run to take over")

    # --- F: the dry run prints the command and writes nothing --------------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0})
        spawner = RecordingSpawner()
        out = run(vault, skill_path=fake_skill(vault), claude_bin="claude",
                  clock=clock, sleeper=lambda s: clock.advance(s), spawn_fn=spawner)
        line = out.printed[-1] if out.printed else ""
        if spawner.calls:
            fail("F the default mode spawned a process")
        if action_log_path(vault).exists():
            fail("F the dry run wrote to the event log")
        if "dry-run" not in line:
            fail(f"F no dry-run line: {out.printed}")
        for token in ("-p", "--output-format", "--permission-mode", "--max-budget-usd",
                      "CREATE_NO_WINDOW" if os.name == "nt" else "posix"):
            if token not in line:
                fail(f"F the printed command omits {token}: {line}")
        if f"{len(out.printed)}" and len(out.printed) != 1:
            fail(f"F printed {len(out.printed)} lines, expected 1")
        if not failures:
            print("OK   case F: the default is a dry run: the full argv, cwd, "
                  "creation flags, and prompt digest print; nothing is spawned "
                  "or written")

    # --- G: --spawn launches once and appends exactly one log line ---------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0, "drop.md": 7200.0})
        skill = fake_skill(vault)
        log = action_log_path(vault)
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("---\ntype: log\n---\n\n# action-agent event log\n",
                       encoding="utf-8")
        before = log.read_text(encoding="utf-8")
        spawner = RecordingSpawner()
        out = run(vault, spawn=True, skill_path=skill, claude_bin="claude",
                  max_budget_usd=10.0, clock=clock,
                  sleeper=lambda s: clock.advance(s), spawn_fn=spawner)

        added = log.read_text(encoding="utf-8")[len(before):].strip().splitlines()
        if len(spawner.calls) != 1:
            fail(f"G {len(spawner.calls)} spawns, expected 1")
        if len(added) != 1:
            fail(f"G appended {len(added)} log lines, expected 1: {added}")
        elif len(added[0].split(" | ")) != 5:
            fail(f"G the log line is not five fixed fields: {added[0]}")
        elif " | ".join(added[0].split(" | ")[1:3]) != f"{ACTOR} | event-launch":
            fail(f"G actor/code fields wrong: {added[0]}")
        elif "pid=4242" not in added[0] or "budget=$10.00" not in added[0]:
            fail(f"G the log line lost the launch facts: {added[0]}")
        if not added or not log.read_text(encoding="utf-8").startswith(before):
            fail("G the append rewrote existing log bytes")
        call = spawner.calls[0] if spawner.calls else {}
        if call.get("cwd") != vault.resolve():
            fail(f"G spawned in {call.get('cwd')}, expected the vault root")
        if fake_skill_body not in call.get("prompt", ""):
            fail("G the deployed SKILL body did not reach the prompt")
        if "--max-budget-usd" not in call.get("cmd", []):
            fail(f"G the spend ceiling left the command: {call.get('cmd')}")
        if out.pid != 4242 or not out.spawned:
            fail(f"G outcome {out}")
        if not failures:
            print("OK   case G: --spawn launches once at the vault root with the "
                  "SKILL body on stdin, and appends exactly one five-field line")

        # --- H: a dry run after a launch appends nothing -------------------
        after_launch = log.read_text(encoding="utf-8")
        run(vault, skill_path=skill, claude_bin="claude", clock=clock,
            sleeper=lambda s: clock.advance(s), spawn_fn=spawner)
        if log.read_text(encoding="utf-8") != after_launch:
            fail("H a dry run appended to the event log")
        elif len(spawner.calls) != 1:
            fail(f"H the dry run spawned again: {len(spawner.calls)} calls")
        else:
            print("OK   case H: a dry run following a real launch writes nothing "
                  "and spawns nothing")

    # --- I: a missing deployed SKILL fails loudly instead of launching -----
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0})
        spawner = RecordingSpawner()
        out = run(vault, spawn=True, skill_path=vault / "absent" / "SKILL.md",
                  claude_bin="claude", clock=clock,
                  sleeper=lambda s: clock.advance(s), spawn_fn=spawner)
        if out.exit_code != 2 or spawner.calls:
            fail(f"I a missing SKILL still launched (exit {out.exit_code})")
        elif not any("launch-failed" in line for line in out.printed):
            fail(f"I no launch-failed line: {out.printed}")
        else:
            print("OK   case I: a missing deployed SKILL exits 2 without spawning")

    # --- J: the lease reader agrees with run_lease's own format ------------
    with tempfile.TemporaryDirectory() as tmp:
        from run_lease import RunLease

        lease_file = Path(tmp) / _LEASE_FILENAME
        held = RunLease("note-kit-action-agent", "event pass",
                        lease_path=lease_file, clock=lambda: base_now)
        held.acquire()
        status = read_lease_status(lease_file, now=base_now)
        if not status.held or status.holder != "note-kit-action-agent":
            fail(f"J a lease RunLease just took reads as {status}")
        held.release()
        released = read_lease_status(lease_file, now=base_now)
        if released.state != "free":
            fail(f"J a released lease reads as {released}")
        garbled = Path(tmp) / "garbled.md"
        garbled.write_text("not-a-timestamp | ghost | lease-taken | corrupt\n",
                           encoding="utf-8")
        if read_lease_status(garbled, now=base_now).state != "expired":
            fail("J a corrupt lease line did not read as expired")
        if not failures:
            print("OK   case J: the read-only lease check matches RunLease's own "
                  "taken/released states, and a corrupt line reads expired")

    # --- K: the settle is reported only when the pass actually waited -------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 60.0})
        spawner = RecordingSpawner()
        hot = run(vault, skill_path=fake_skill(vault), claude_bin="claude",
                  clock=clock, sleeper=lambda s: clock.advance(s), spawn_fn=spawner)
        codes = [line.split(" | ")[2] for line in hot.printed]
        if codes != ["settle-wait", "dry-run"]:
            fail(f"K a waiting pass printed {codes}")
        clock2 = FakeClock(base_now)
        quiet_vault = build_vault(Path(tmp) / "quiet", ages={"User-Queue.md": 3600.0})
        cold = run(quiet_vault, skill_path=fake_skill(quiet_vault),
                   claude_bin="claude", clock=clock2,
                   sleeper=lambda s: clock2.advance(s), spawn_fn=spawner)
        cold_codes = [line.split(" | ")[2] for line in cold.printed]
        if cold_codes != ["dry-run"]:
            fail(f"K an already-quiet pass printed {cold_codes}")
        if not failures:
            print("OK   case K: a pass that waits reports settle-wait; a pass "
                  "that found the surfaces quiet reports only its verdict")

    # --- L: the spawn primitive itself, against a harmless stand-in --------
    # No agent runs here: the child is a python one-liner that copies its stdin
    # to a file, which proves the prompt reaches the process, the working
    # directory is honoured, and the no-window flags start a real child.
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        landed = work / "stdin.txt"
        child = [sys.executable, "-c",
                 "import sys,pathlib;"
                 "pathlib.Path('stdin.txt').write_text(sys.stdin.read(),"
                 "encoding='utf-8')"]
        spawned = spawn_detached(child, work, "the prompt the agent would read",
                                 wait=True)
        if spawned.returncode != 0 or spawned.pid is None:
            fail(f"L the child did not run cleanly: {spawned}")
        elif not landed.is_file():
            fail("L the child wrote nothing — the working directory was not honoured")
        elif landed.read_text(encoding="utf-8") != "the prompt the agent would read":
            fail(f"L stdin arrived as {landed.read_text(encoding='utf-8')!r}")
        else:
            print("OK   case L: spawn_detached starts a real child with the "
                  f"prompt on stdin at the given cwd, flags 0x{creation_flags():08x}")

    # --- M: a live guard turns a second invocation into one defer line ------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0})
        lock_file = lock_path_for(vault.resolve())
        first = acquire_launch_lock(lock_file, now=base_now, target="User-Queue.md")
        spawner = RecordingSpawner()
        second = run(vault, spawn=True, skill_path=fake_skill(vault),
                     claude_bin="claude", clock=clock,
                     sleeper=lambda s: clock.advance(s), spawn_fn=spawner)
        if not first.acquired or not first.owns_file:
            fail(f"M the first arrival did not take the guard: {first}")
        if second.exit_code != 0:
            fail(f"M the coalesced arrival exited {second.exit_code}, expected 0")
        if spawner.calls:
            fail("M a second invocation launched while the guard was live")
        if action_log_path(vault).exists():
            fail("M a coalesced arrival wrote to the event log")
        codes = [line.split(" | ")[2] for line in second.printed]
        if codes != ["coalesce-defer"]:
            fail(f"M the coalesced arrival printed {codes}")
        if not lock_file.is_file():
            fail("M the coalesced arrival deleted the live holder's guard")
        release_launch_lock(lock_file, first)
        if lock_file.exists():
            fail("M the holder's release left the guard behind")
        if not failures:
            print("OK   case M: a live guard collapses a second invocation to one "
                  "coalesce-defer line at exit 0, and release frees the lock")

    # --- N: a dead holder's guard is taken over, with a printed note --------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0})
        lock_file = lock_path_for(vault.resolve())
        # A pid that certainly no longer runs: a child started and reaped here.
        finished = subprocess.Popen([sys.executable, "-c", "pass"],
                                    creationflags=creation_flags())
        finished.wait()
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.write_text(_lock_text(finished.pid, base_now, "User-Queue.md"),
                             encoding="utf-8")
        if pid_alive(os.getpid()) is not True:
            fail("N the liveness probe called this very process dead")
        spawner = RecordingSpawner()
        out = run(vault, spawn=True, skill_path=fake_skill(vault),
                  claude_bin="claude", clock=clock,
                  sleeper=lambda s: clock.advance(s), spawn_fn=spawner)
        codes = [line.split(" | ")[2] for line in out.printed]
        if "lock-takeover" not in codes:
            fail(f"N a dead holder's guard was not taken over: {codes}")
        if len(spawner.calls) != 1:
            fail(f"N {len(spawner.calls)} launches after the takeover, expected 1")
        if lock_file.exists():
            fail("N the launch left its guard behind")

        # An aged-out live holder is stale too — the bounded wait is the limit.
        lock_file.write_text(
            _lock_text(os.getpid(), base_now - timedelta(seconds=COALESCE_SPAN_SECONDS + 60),
                       "User-Queue.md"),
            encoding="utf-8")
        aged = acquire_launch_lock(lock_file, now=base_now, target="x")
        if not aged.acquired or not aged.took_over:
            fail(f"N a holder past the coalescing bound still blocked: {aged}")
        release_launch_lock(lock_file, aged)
        if not failures:
            print("OK   case N: a dead pid and a holder aged past the "
                  f"{COALESCE_SPAN_SECONDS}s bound are both displaced with a "
                  "printed note, and the launch proceeds once")

    # --- O: the guard releases even when the pass defers or fails -----------
    with tempfile.TemporaryDirectory() as tmp:
        clock = FakeClock(base_now)
        fresh = "2026-07-26T20:30Z | orchestrator-session | lease-taken | live work\n"
        vault = build_vault(Path(tmp), ages={"User-Queue.md": 3600.0}, lease=fresh)
        lock_file = lock_path_for(vault.resolve())
        deferred = run(vault, spawn=True, skill_path=fake_skill(vault),
                       claude_bin="claude", clock=clock,
                       sleeper=lambda s: clock.advance(s), spawn_fn=RecordingSpawner())
        if not any("lease-defer" in line for line in deferred.printed):
            fail(f"O the deferral sub-case did not defer: {deferred.printed}")
        if lock_file.exists():
            fail("O a lease deferral left the guard held")

        # The failing paths need a free lease, or they defer before reaching one.
        free = build_vault(Path(tmp) / "free", ages={"User-Queue.md": 3600.0})
        free_lock = lock_path_for(free.resolve())
        failed = run(free, spawn=True, skill_path=free / "absent" / "SKILL.md",
                     claude_bin="claude", clock=clock,
                     sleeper=lambda s: clock.advance(s), spawn_fn=RecordingSpawner())
        if failed.exit_code != 2:
            fail(f"O the failure sub-case exited {failed.exit_code}, expected 2")
        if free_lock.exists():
            fail("O a launch failure left the guard held")
        raised = False
        try:
            run(free, spawn=True, skill_path=fake_skill(free), claude_bin="claude",
                clock=clock, sleeper=lambda s: clock.advance(s),
                spawn_fn=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        except RuntimeError:
            raised = True
        if not raised:
            fail("O the exception sub-case never reached the spawner")
        if free_lock.exists():
            fail("O an exception left the guard held")
        if not failures:
            print("OK   case O: the guard releases on every exit path: lease "
                  "deferral, launch failure, and a raised exception")

    # --- P: the frontmatter discriminator reads through a UTF-8 BOM ---------
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        cases = {
            "bom-front.md": b"\xef\xbb\xbf---\ntype: note\n---\nbody\n",
            "bom-drop.md": b"\xef\xbb\xbfa bare user drop\n",
            "plain-front.md": b"---\ntype: note\n---\nbody\n",
            "plain-drop.md": b"a bare user drop\n",
            "empty.md": b"",
            "dashes-only.md": b"---",
        }
        for name, data in cases.items():
            (work / name).write_bytes(data)
        expected = {"bom-front.md": True, "bom-drop.md": False,
                    "plain-front.md": True, "plain-drop.md": False,
                    "empty.md": False, "dashes-only.md": True}
        got = {name: has_frontmatter(work / name) for name in cases}
        if got != expected:
            fail(f"P the discriminator read {got}, expected {expected}")
        elif has_frontmatter(work / "absent.md") is not True:
            fail("P an unreadable path did not read as frontmattered")
        else:
            print("OK   case P: a BOM'd frontmattered draft reads as a draft and "
                  "a BOM'd bare drop reads as a drop; plain files are unchanged")

    if failures:
        print(f"\n{failures} self-test failure(s).")
        return 1
    print("\nAll self-tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
