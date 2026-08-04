#!/usr/bin/env python3
"""
agent_gate.py
=============

The deterministic no-op gate for the four note-kit scheduled agents
(Note-Kit-Model-Tiering-and-Skill-Queue-Plan § D). Given an agent's scope,
answer one question from cheap disk signals — does provable work exist? — so
the deployment wrapper (`scheduled_gate_launcher.py`) can decline to spawn a
model on an empty pass.

Pure by contract: this script reads and prints; it never writes, locks, or
spawns. The launcher owns every side effect (the spawn, the log line, the
fingerprint record), so the gate can run any number of times without changing
what the next run sees.

Verdicts
--------
    gate: work      something in the agent's scope needs a pass
    gate: quiet     provably nothing to do

Both verdicts exit 0; the verdict is the printed line, not the exit code
(exit 2 marks a usage error — unknown agent, missing vault root). Alongside
the verdict the gate prints one `reason:` line per firing clause and one
`fingerprint:` line (JSON) carrying the scope fingerprint the launcher
records at spawn time.

Predicates (the plan § D, adjusted to live-vault evidence — DESIGN.md carries
each deviation and its reason)
------------------------------
action   - an unswept decision line in `<user-queue>` (a `- [x]` or `- [-]`
           option whose own line and governing heading both lack an
           `(EXECUTED`/`(RECORDED` annotation); any open `- [ ]` line in
           `<machine-queue>`; a change in the frontmatter-less drop set at
           the `<inbox>` root (fingerprint delta, so a standing mobile-review
           bundle does not hold the gate open forever).
filing   - any `reviewed: true` `.md` under `<inbox>` (recursive, the two
           queue files excluded); a change in the staged-asset surface (the
           non-md files under `<inbox>` plus empty folders — husk and asset
           candidates).
janitor  - any change in the corpus fingerprint: every vault `.md`, dot-dirs
           pruned, `<logs>` (Archive/Logs) excluded so the agents' own log
           writes never self-trigger; the rest of Archive stays in scope.
analyst  - the same corpus fingerprint, tracked in its own state file.

Staleness override (every agent): when the agent's own event log
(`<logs>/<stem>/<stem>.md`) shows no timestamped line within the agent's
window — action 24 h, filing/janitor 48 h, analyst 192 h — the gate returns
work regardless, so time-based duties (log rotation, holds regeneration,
lifecycle retirements, dwell reporting) and slow drift never starve. The
launcher's `gate-launch` line refreshes the log head at each spawn, which
makes the override self-limiting: it fires at most once per window.

Fingerprint semantics
---------------------
A fingerprint is `{count, total_size, max_mtime_ns, empty_dirs, paths_hash}`
over the clause's file set; `paths_hash` (sha256 of the sorted relative path
list) makes a pure rename a delta. The gate compares the current fingerprint
against `<logs>/<agent>-gate-fingerprint.json` and reports work on any
difference, a missing record, or an unreadable one — bias to work. The gate
never writes that file: the launcher records the gate's fingerprint only when
a spawn actually happens, so a quiet verdict changes nothing on disk and a
work verdict keeps re-asserting itself until a run is actually launched.

Stdlib only, deliberately: `config_variables.py` parses CONFIG.md at import
time and hard-fails on a parse error, and the gate must keep producing
verdicts through exactly that kind of incident. The folder literals below
mirror the fallback literals `event_action_launcher.py` already carries for
the same tokens; the launcher cross-checks them against `token_path` at
spawn time and forces work on drift (`gate-literal-drift`).

Usage
-----
    python agent_gate.py --vault-root <vault> --agent {action|filing|janitor|analyst}
    python agent_gate.py --vault-root <vault> --agent janitor --now 2026-08-03T21:00Z
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

# ---------------------------------------------------------------------------
# Config literals — the same fallback literals event_action_launcher.py passes
# to token_path() for these tokens. The launcher verifies them against the
# live CONFIG at spawn time; a CONFIG folder rename therefore surfaces as
# `gate-literal-drift` (forced work) rather than a silent wrong-folder watch.
# ---------------------------------------------------------------------------

INBOX_REL = "Inbox"                          # <inbox>
USER_QUEUE_REL = "Inbox/User-Queue.md"       # <user-queue>
MACHINE_QUEUE_REL = "Inbox/Machine-Queue.md" # <machine-queue>
LOGS_REL = "Archive/Logs"                    # <logs>
_ARCHIVE_DIRNAME = "Archive"
_LOGS_DIRNAME = "Logs"

# A `- [x]`/`- [-]` user-queue option is swept when its own line or its
# governing heading carries an executed/recorded annotation (live-queue forms:
# `_(EXECUTED: ...)_`, `_(EXECUTED 2026-08-01: ...)_`, `(RECORDED ...)`).
_SWEPT_RE = re.compile(r"\((EXECUTED|RECORDED)", re.IGNORECASE)
_HEADING_RE = re.compile(r"^#{1,6}\s")
_OPTION_RE = re.compile(r"^\s*-\s*\[( |x|X|-)\]")
_OPEN_MQ_RE = re.compile(r"^\s*-\s*\[ \]")
_REVIEWED_TRUE_RE = re.compile(r"^reviewed:\s*['\"]?true['\"]?\s*$",
                               re.IGNORECASE | re.MULTILINE)
# Log lines open `YYYY-MM-DDTHH:MM(:SS)?Z | ...` (CONFIG § Log files).
_LOG_TS_RE = re.compile(rb"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::\d{2})?Z\s*\|")

_FRONTMATTER_READ_CAP = 8192
_LOG_TAIL_BYTES = 16384


@dataclass(frozen=True)
class AgentSpec:
    key: str            # CLI name: action | filing | janitor | analyst
    stem: str           # log-directory stem: action-agent, ...
    stale_hours: float  # staleness-override window


AGENTS: dict[str, AgentSpec] = {
    "action":  AgentSpec("action",  "action-agent",  24.0),
    "filing":  AgentSpec("filing",  "filing-agent",  48.0),
    "janitor": AgentSpec("janitor", "janitor-agent", 48.0),
    "analyst": AgentSpec("analyst", "analyst-agent", 192.0),
}


@dataclass
class GateResult:
    """One evaluation: the verdict, why, and the state the launcher records."""
    agent: str
    verdict: str                      # "work" | "quiet"
    reasons: list[str] = field(default_factory=list)
    fingerprint: dict = field(default_factory=dict)
    fingerprint_path: Optional[Path] = None
    log_path: Optional[Path] = None
    stale_hours: float = 0.0

    @property
    def work(self) -> bool:
        return self.verdict == "work"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Cheap file probes
# ---------------------------------------------------------------------------

def has_frontmatter(path: Path) -> bool:
    """True when the file head opens `---` — the drop discriminator.

    Mirrors event_action_launcher.has_frontmatter: six bytes read so a UTF-8
    BOM can be stripped with three real bytes left to test, and an unreadable
    file reads as frontmattered so a transient lock never fires the gate.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(6)
    except OSError:
        return True
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    return head.startswith(b"---")


def frontmatter_is_reviewed_true(path: Path) -> bool:
    """True when the file's frontmatter block carries `reviewed: true`.

    Reads at most the first 8 KB; the block is the lines between the opening
    `---` and the next bare `---` line (or the cap, when the close is not in
    reach — `reviewed:` sits near the top of every kit draft). An unreadable
    file reads False, the same conservative side has_frontmatter takes.
    """
    try:
        with open(path, "rb") as fh:
            head = fh.read(_FRONTMATTER_READ_CAP)
    except OSError:
        return False
    if head.startswith(b"\xef\xbb\xbf"):
        head = head[3:]
    if not head.startswith(b"---"):
        return False
    text = head.decode("utf-8", errors="replace")
    lines = text.splitlines()
    block: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        block.append(line)
    return bool(_REVIEWED_TRUE_RE.search("\n".join(block)))


def newest_log_stamp(log_path: Path) -> Optional[datetime]:
    """The newest `timestamp | ...` stamp in the log's tail, or None.

    Reads only the final 16 KB (the head file is append-only, so the newest
    line is at the end) and keeps the last line that parses. Minute precision
    matches the kit stamp `YYYY-MM-DDTHH:MMZ`; a seconds suffix is tolerated.
    """
    try:
        size = log_path.stat().st_size
        with open(log_path, "rb") as fh:
            if size > _LOG_TAIL_BYTES:
                fh.seek(size - _LOG_TAIL_BYTES)
            data = fh.read()
    except OSError:
        return None
    stamp: Optional[datetime] = None
    for line in data.splitlines():
        m = _LOG_TS_RE.match(line)
        if m is None:
            continue
        try:
            stamp = datetime.strptime(
                m.group(1).decode("ascii"), "%Y-%m-%dT%H:%M"
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return stamp


# ---------------------------------------------------------------------------
# Queue scans (action)
# ---------------------------------------------------------------------------

def unswept_user_queue_count(uq_path: Path) -> int:
    """Decision lines the action-agent still owes an execution or a sweep.

    A `- [x]` (approve) or `- [-]` (reject) option line counts unless its own
    line or its governing heading (the nearest preceding `#`-heading) carries
    an `(EXECUTED`/`(RECORDED` annotation. A `- [ ]` line is the user's open
    decision and never counts. A queue that exists but will not read counts
    as one item — bias to work on an anomalous surface.
    """
    if not uq_path.is_file():
        return 0
    try:
        text = uq_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    heading_swept = False
    count = 0
    for line in text.splitlines():
        if _HEADING_RE.match(line):
            heading_swept = bool(_SWEPT_RE.search(line))
            continue
        m = _OPTION_RE.match(line)
        if m is None:
            continue
        if m.group(1) == " ":
            continue
        if heading_swept or _SWEPT_RE.search(line):
            continue
        count += 1
    return count


def open_machine_queue_count(mq_path: Path) -> int:
    """Open `- [ ]` machine-queue lines — the user's pending instructions."""
    if not mq_path.is_file():
        return 0
    try:
        text = mq_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 1
    return sum(1 for line in text.splitlines() if _OPEN_MQ_RE.match(line))


# ---------------------------------------------------------------------------
# Fingerprints
# ---------------------------------------------------------------------------

def _fingerprint_from(entries: Iterable[tuple[str, int, int]],
                      empty_dirs: Iterable[str] = ()) -> dict:
    """Fold (relpath, size, mtime_ns) rows into the comparable fingerprint.

    `paths_hash` covers the sorted path list (empty dirs carried with a
    trailing `/`), so a pure rename or a same-size same-mtime swap still
    reads as a delta.
    """
    count = 0
    total_size = 0
    max_mtime_ns = 0
    paths: list[str] = []
    for rel, size, mtime_ns in entries:
        count += 1
        total_size += size
        if mtime_ns > max_mtime_ns:
            max_mtime_ns = mtime_ns
        paths.append(rel)
    empties = sorted(empty_dirs)
    paths.extend(e + "/" for e in empties)
    digest = hashlib.sha256("\n".join(sorted(paths)).encode("utf-8")).hexdigest()
    return {
        "count": count,
        "total_size": total_size,
        "max_mtime_ns": max_mtime_ns,
        "empty_dirs": len(empties),
        "paths_hash": digest,
    }


def _stat_row(path: Path, vault_root: Path) -> Optional[tuple[str, int, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    rel = path.relative_to(vault_root).as_posix()
    return rel, st.st_size, st.st_mtime_ns


def inbox_drop_fingerprint(vault_root: Path) -> dict:
    """action — the frontmatter-less drop set at the `<inbox>` root.

    Files only (a container is the filing-agent's), dotfiles skipped, the two
    queue files excluded by name; non-md files included — a drop needs no
    extension, and a binary never opens `---`.
    """
    inbox = vault_root / INBOX_REL
    queue_names = {Path(USER_QUEUE_REL).name.lower(),
                   Path(MACHINE_QUEUE_REL).name.lower()}
    rows: list[tuple[str, int, int]] = []
    if inbox.is_dir():
        for child in sorted(inbox.iterdir()):
            if not child.is_file() or child.name.startswith("."):
                continue
            if child.name.lower() in queue_names:
                continue
            if has_frontmatter(child):
                continue
            row = _stat_row(child, vault_root)
            if row is not None:
                rows.append(row)
    return _fingerprint_from(rows)


def inbox_staged_fingerprint(vault_root: Path) -> dict:
    """filing — the staged-asset surface under `<inbox>`.

    Every visible non-md file at any depth (a root loose asset, the
    `<inbox-assets>` staging tree, an asset folder's contents) plus every
    visible-empty directory (a husk or fresh asset-folder shell). Markdown
    members are excluded: their actionability is the `reviewed: true` clause,
    so an unreviewed container never holds this clause hot.
    """
    inbox = vault_root / INBOX_REL
    rows: list[tuple[str, int, int]] = []
    empties: list[str] = []
    if inbox.is_dir():
        for dirpath, dirnames, filenames in os.walk(inbox):
            dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
            visible = [f for f in filenames if not f.startswith(".")]
            here = Path(dirpath)
            if not dirnames and not visible and here != inbox:
                empties.append(here.relative_to(vault_root).as_posix())
                continue
            for name in sorted(visible):
                if name.lower().endswith(".md"):
                    continue
                row = _stat_row(here / name, vault_root)
                if row is not None:
                    rows.append(row)
    return _fingerprint_from(rows, empties)


def corpus_fingerprint(vault_root: Path) -> dict:
    """janitor/analyst — every vault `.md` outside dot-dirs and `<logs>`.

    Dot-directories (`.claude`, `.obsidian`, `.git`, `.trash`, `.history`,
    any future dot-dir) are pruned at every depth — the CONFIG § Scan
    exclusions convention. `Archive/Logs` is pruned so the log lines the
    agents themselves append never re-trigger the agents; the rest of
    Archive stays in scope because the janitor walks it.
    """
    rows: list[tuple[str, int, int]] = []
    root = vault_root
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        rel_parts = here.relative_to(root).parts
        dirnames[:] = sorted(d for d in dirnames if not d.startswith("."))
        if rel_parts == (_ARCHIVE_DIRNAME,):
            dirnames[:] = [d for d in dirnames if d != _LOGS_DIRNAME]
        for name in sorted(filenames):
            if not name.lower().endswith(".md"):
                continue
            row = _stat_row(here / name, root)
            if row is not None:
                rows.append(row)
    return _fingerprint_from(rows)


def fingerprint_path_for(vault_root: Path, agent: str) -> Path:
    return vault_root / LOGS_REL / f"{agent}-gate-fingerprint.json"


def load_recorded_fingerprint(fp_path: Path) -> Optional[dict]:
    """The fingerprint the launcher recorded at the last spawn, or None.

    None (absent or unreadable or non-dict) reads as "no recorded state", and
    the caller biases to work.
    """
    try:
        data = json.loads(fp_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _fingerprint_delta(current: dict, recorded: Optional[dict]) -> Optional[str]:
    """A short reason string when current differs from recorded, else None."""
    if recorded is None:
        return "no-recorded-fingerprint"
    keys = ("count", "total_size", "max_mtime_ns", "empty_dirs", "paths_hash")
    diffs = [k for k in keys if current.get(k) != recorded.get(k)]
    if not diffs:
        return None
    return "changed=" + ",".join(diffs)


# ---------------------------------------------------------------------------
# The evaluation
# ---------------------------------------------------------------------------

def evaluate(vault_root: Path, agent: str,
             now: Optional[datetime] = None) -> GateResult:
    """Evaluate one agent's gate. Read-only; raises only on a caller error."""
    if agent not in AGENTS:
        raise ValueError(f"unknown agent {agent!r}; expected one of "
                         f"{sorted(AGENTS)}")
    spec = AGENTS[agent]
    vault_root = Path(vault_root)
    now = now or _now_utc()
    result = GateResult(agent=agent, verdict="quiet",
                        stale_hours=spec.stale_hours)
    result.fingerprint_path = fingerprint_path_for(vault_root, agent)
    result.log_path = (vault_root / LOGS_REL / spec.stem / f"{spec.stem}.md")

    def fire(reason: str) -> None:
        result.verdict = "work"
        result.reasons.append(reason)

    # --- stateless clauses -------------------------------------------------
    if agent == "action":
        uq = unswept_user_queue_count(vault_root / USER_QUEUE_REL)
        if uq:
            fire(f"user-queue-unswept count={uq}")
        mq = open_machine_queue_count(vault_root / MACHINE_QUEUE_REL)
        if mq:
            fire(f"machine-queue-open count={mq}")
    elif agent == "filing":
        inbox = vault_root / INBOX_REL
        queue_paths = {(vault_root / USER_QUEUE_REL).resolve(),
                       (vault_root / MACHINE_QUEUE_REL).resolve()}
        reviewed = 0
        if inbox.is_dir():
            for dirpath, dirnames, filenames in os.walk(inbox):
                dirnames[:] = sorted(d for d in dirnames
                                     if not d.startswith("."))
                here = Path(dirpath)
                for name in sorted(filenames):
                    if name.startswith(".") or not name.lower().endswith(".md"):
                        continue
                    path = here / name
                    if path.resolve() in queue_paths:
                        continue
                    if frontmatter_is_reviewed_true(path):
                        reviewed += 1
        if reviewed:
            fire(f"inbox-reviewed-true count={reviewed}")

    # --- fingerprint clause ------------------------------------------------
    if agent == "action":
        current = inbox_drop_fingerprint(vault_root)
        clause = "inbox-drop-set"
    elif agent == "filing":
        current = inbox_staged_fingerprint(vault_root)
        clause = "staged-asset-set"
    else:
        current = corpus_fingerprint(vault_root)
        clause = "corpus"
    result.fingerprint = current
    delta = _fingerprint_delta(
        current, load_recorded_fingerprint(result.fingerprint_path))
    if delta is not None:
        fire(f"{clause}-delta {delta}")

    # --- staleness override ------------------------------------------------
    stamp = newest_log_stamp(result.log_path)
    if stamp is None:
        fire(f"log-head-absent threshold-hours={spec.stale_hours:g}")
    else:
        age_hours = (now - stamp).total_seconds() / 3600.0
        if age_hours >= spec.stale_hours:
            fire(f"log-stale age-hours={age_hours:.1f} "
                 f"threshold-hours={spec.stale_hours:g}")

    return result


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _parse_now(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic no-op gate for the note-kit scheduled "
                    "agents: print `gate: work` or `gate: quiet` and exit 0. "
                    "Read-only — the launcher owns every side effect.")
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--agent", required=True, choices=sorted(AGENTS))
    parser.add_argument("--now", type=_parse_now, default=None,
                        help="ISO-8601Z instant to evaluate against "
                             "(rehearsal; default: the current UTC time).")
    args = parser.parse_args(argv)

    vault_root = Path(args.vault_root)
    if not vault_root.is_dir():
        print(f"error: vault root is not a directory: {vault_root}",
              file=sys.stderr)
        return 2

    result = evaluate(vault_root, args.agent, now=args.now)
    print(f"gate: {result.verdict}")
    for reason in result.reasons:
        print(f"reason: {reason}")
    print(f"fingerprint: {json.dumps(result.fingerprint, sort_keys=True)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
