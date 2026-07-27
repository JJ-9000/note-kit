#!/usr/bin/env python3
"""
rotate_logs.py
===================

Rotate an agent's live event log when it outgrows its window (CONFIG § Log
files). The head file `<logs>/<agent>/<agent>.md` is live and protected; once it
exceeds the size limit or its oldest entry passes the age window, the aged
months close as `<agent>-<YYYY-MM>.md` **beside the head**, and the head keeps
the current month. The closed segment — not the head — ages onward to
`<history>` (§ Content lifecycle, `age_to_cold_storage.py`).

Trigger: the start of every scheduled agent pass (CONFIG § Helper-script
automation), so a pass never appends to a head that should already have rotated.

Append-only discipline
----------------------
A surviving head line is never rewritten and never reordered: the rotation is a
byte-exact partition of the head at a line boundary. Everything before the
boundary becomes the segment, everything from the boundary on stays the head,
and the head's preamble (the lines above its first dated entry) stays with the
head. A non-entry line — a blank separator, a wrapped continuation — travels
with the entry it follows, so no line is dropped, duplicated, or moved past its
neighbours.

Concurrency
-----------
An agent may append to the head while this runs. The head is read once, and the
bytes are re-read immediately before the write: an append (the new bytes extend
the old ones) is carried into the new head verbatim; any other change aborts the
rotation untouched. Nothing is written until that check passes.

Safety
------
Every file the rotation rewrites is pre-imaged first, beside itself, as
`<YYYY-MM-DD>-<name>-pre-rotation.md`. After the write the pass verifies line
conservation — head lines + segment lines equal the original line count — and
restores from the pre-image when the count disagrees.

Idempotent: a second run finds no month older than the current one in the head
and rotates nothing.

Unicode and line endings: the partition is performed on bytes, so UTF-8 content
survives untouched and each line keeps its own ending (CRLF, LF, or a mix). Text
this script writes itself uses the file's dominant ending.

Config-driven: the log root, the size limit, and the age window are read from
CONFIG.md — no literals here.

Usage
-----
    python rotate_logs.py --vault-root <vault> [--apply]
    NOTE_KIT_VAULT_ROOT=/path/to/vault python rotate_logs.py --apply

Without --apply it is a dry run: it prints what would rotate and writes nothing.
Run with no vault root resolvable and no args to execute the self-tests.

Prints one fixed-field line per head (CONFIG § Log files):
    timestamp | rotate-logs | <code> | <target> | <value>
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

_SCRIPTS_DIR = Path(__file__).resolve().parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import (  # noqa: E402
    _CONFIG_TEXT,
    resolve_vault_root,
    token_path,
)

# A log entry opens with its ISO date (CONFIG § Log files: `timestamp | actor |
# code | target | value`). Anything else on its own line is a preamble comment,
# a blank separator, or a continuation.
_ENTRY_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})")

_DEFAULT_SIZE_KB = 100
_DEFAULT_AGE_DAYS = 30


# ---------------------------------------------------------------------------
# Policy — read the rotation thresholds from CONFIG
# ---------------------------------------------------------------------------

def _section_text(text: str, heading: str) -> str:
    """The body of an H2 section, empty when the section is absent."""
    m = re.search(r"^## " + re.escape(heading) + r"\s*$", text, re.MULTILINE)
    if m is None:
        return ""
    after = text[m.end():]
    nxt = re.search(r"^## ", after, re.MULTILINE)
    return after[: nxt.start()] if nxt else after


def parse_rotation_policy(text: str = _CONFIG_TEXT) -> tuple[int, int]:
    """Return (size_limit_kb, age_window_days) from CONFIG.

    § Log files states the rule ("when it exceeds 100 KB or its oldest entry
    passes 30 days it rotates"); § Content lifecycle's event-log row repeats it.
    Either phrasing supplies the numbers; the defaults apply when neither does.
    """
    size_kb, age_days = _DEFAULT_SIZE_KB, _DEFAULT_AGE_DAYS
    for heading in ("Log files", "Content lifecycle"):
        body = _section_text(text, heading)
        if not body:
            continue
        sm = re.search(r"exceeds?\s+(\d+)\s*KB", body, re.IGNORECASE)
        am = re.search(r"oldest entry passes\s+(\d+)\s*days?", body, re.IGNORECASE)
        if sm:
            size_kb = int(sm.group(1))
        if am:
            age_days = int(am.group(1))
        if sm and am:
            break
    return size_kb, age_days


# ---------------------------------------------------------------------------
# Byte-exact line handling
# ---------------------------------------------------------------------------

def _line_offsets(data: bytes) -> list[int]:
    """Start offset of every line in `data` (a line ends after its `\\n`)."""
    offsets: list[int] = []
    if not data:
        return offsets
    pos = 0
    n = len(data)
    while pos < n:
        offsets.append(pos)
        nl = data.find(b"\n", pos)
        if nl == -1:
            break
        pos = nl + 1
    return offsets


def count_lines(data: bytes) -> int:
    """Line count: every `\\n` plus a trailing line with no ending."""
    if not data:
        return 0
    n = data.count(b"\n")
    return n if data.endswith(b"\n") else n + 1


def dominant_newline(data: bytes) -> bytes:
    """The line ending most of this file uses — CRLF when it leads, else LF."""
    crlf = data.count(b"\r\n")
    lf = data.count(b"\n") - crlf
    return b"\r\n" if crlf > lf else b"\n"


def _entry_month(line: bytes) -> str | None:
    """`YYYY-MM` for a dated entry line, None for anything else."""
    m = _ENTRY_RE.match(line.decode("utf-8", "replace"))
    return f"{m.group(1)}-{m.group(2)}" if m else None


def _entry_date(line: bytes) -> datetime | None:
    """The UTC date a dated entry opens with, None for a non-entry line."""
    m = _ENTRY_RE.match(line.decode("utf-8", "replace"))
    if not m:
        return None
    try:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)),
                        tzinfo=timezone.utc)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# The rotation plan
# ---------------------------------------------------------------------------

@dataclass
class HeadPlan:
    """What one head file's rotation would do (or did)."""
    head: str                                    # vault-relative path
    size_before: int = 0
    size_after: int = 0
    lines_before: int = 0
    lines_after: int = 0
    triggered: bool = False
    reason: str = ""
    months: list[str] = field(default_factory=list)     # months closing
    segments: dict[str, str] = field(default_factory=dict)   # month -> rel path
    segment_lines: dict[str, int] = field(default_factory=dict)
    preimages: list[str] = field(default_factory=list)
    applied: bool = False
    errors: list[str] = field(default_factory=list)

    @property
    def rotated_lines(self) -> int:
        return sum(self.segment_lines.values())


def plan_rotation(
    data: bytes,
    *,
    now: datetime,
    size_limit_bytes: int,
    age_window_days: int,
) -> tuple[bool, str, int, int, list[tuple[str, int, int]]]:
    """Partition a head's bytes into the head that stays and the months closing.

    Returns (triggered, reason, entries_offset, boundary_offset, groups) where
    `groups` is [(month, start_offset, end_offset)] over the closing block and
    `entries_offset` is where the head's preamble ends. `boundary_offset` is the
    first byte the head keeps after its preamble.
    """
    offsets = _line_offsets(data)
    lines = [data[o: (offsets[i + 1] if i + 1 < len(offsets) else len(data))]
             for i, o in enumerate(offsets)]

    keep_month = f"{now.year:04d}-{now.month:02d}"
    first_entry_idx: int | None = None
    boundary_idx: int | None = None
    oldest: datetime | None = None

    for i, line in enumerate(lines):
        month = _entry_month(line)
        if month is None:
            continue
        if first_entry_idx is None:
            first_entry_idx = i
            oldest = _entry_date(line)
        if month >= keep_month:
            boundary_idx = i
            break

    # Trigger: past the size limit, or an entry older than the age window.
    reasons = []
    if len(data) > size_limit_bytes:
        reasons.append(f"size={len(data)}B>{size_limit_bytes}B")
    if oldest is not None and oldest < now - timedelta(days=age_window_days):
        reasons.append(f"oldest={oldest.date().isoformat()}>{age_window_days}d")
    triggered = bool(reasons)

    if first_entry_idx is None:
        # No dated entry at all — nothing to partition.
        return triggered, ",".join(reasons) or "-", len(data), len(data), []

    entries_off = offsets[first_entry_idx]
    boundary_off = offsets[boundary_idx] if boundary_idx is not None else len(data)
    if boundary_off <= entries_off:
        return triggered, ",".join(reasons) or "-", entries_off, boundary_off, []

    # Group the closing block by month; a non-entry line joins the entry above it.
    groups: list[tuple[str, int, int]] = []
    current: str | None = None
    start = entries_off
    end_idx = boundary_idx if boundary_idx is not None else len(lines)
    for i in range(first_entry_idx, end_idx):
        month = _entry_month(lines[i])
        if month is not None and month != current:
            if current is not None:
                groups.append((current, start, offsets[i]))
                start = offsets[i]
            current = month
    if current is not None:
        groups.append((current, start, boundary_off))
    return triggered, ",".join(reasons) or "-", entries_off, boundary_off, groups


# ---------------------------------------------------------------------------
# Applying it
# ---------------------------------------------------------------------------

def _unique(path: Path) -> Path:
    """`path`, or a numbered sibling when it is taken — never an overwrite."""
    if not path.exists():
        return path
    n = 2
    while True:
        cand = path.with_name(f"{path.stem}-{n}{path.suffix}")
        if not cand.exists():
            return cand
        n += 1


def _write_preimage(src: Path, data: bytes, stamp: str) -> Path:
    """Copy `data` beside `src` as `<stamp>-<name>-pre-rotation<suffix>`."""
    dest = _unique(src.with_name(f"{stamp}-{src.stem}-pre-rotation{src.suffix}"))
    dest.write_bytes(data)
    if dest.read_bytes() != data:
        raise OSError(f"pre-image verify failed: {dest}")
    return dest


def _atomic_write(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".rotate-tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def rotate_head(
    head: Path,
    vault_root: Path,
    *,
    now: datetime,
    size_limit_bytes: int,
    age_window_days: int,
    apply: bool = False,
) -> HeadPlan:
    """Rotate one `<logs>/<agent>/<agent>.md` head. Returns what it did."""
    rel = str(head.relative_to(vault_root)).replace("\\", "/")
    plan = HeadPlan(head=rel)
    try:
        data0 = head.read_bytes()
    except OSError as exc:
        plan.errors.append(f"read failed: {rel} ({exc})")
        return plan

    plan.size_before = len(data0)
    plan.lines_before = count_lines(data0)
    plan.size_after = plan.size_before
    plan.lines_after = plan.lines_before

    triggered, reason, entries_off, boundary_off, groups = plan_rotation(
        data0, now=now, size_limit_bytes=size_limit_bytes,
        age_window_days=age_window_days,
    )
    plan.triggered = triggered
    plan.reason = reason
    if not triggered or not groups:
        return plan

    stamp = now.strftime("%Y-%m-%d")
    plan.months = [g[0] for g in groups]
    for month, start, end in groups:
        seg = head.with_name(f"{head.stem}-{month}{head.suffix}")
        plan.segments[month] = str(seg.relative_to(vault_root)).replace("\\", "/")
        plan.segment_lines[month] = count_lines(data0[start:end])
    plan.lines_after = plan.lines_before - plan.rotated_lines
    plan.size_after = plan.size_before - (boundary_off - entries_off)

    if not apply:
        return plan

    # --- write path -------------------------------------------------------
    written: list[tuple[Path, int]] = []   # (segment, size before we appended)
    head_preimage: Path | None = None
    # The freshest full read of the head. It starts at the pre-rotation read and
    # advances to the concurrency re-read, so a failure anywhere below restores
    # the head INCLUDING an append that landed mid-rotation.
    freshest: bytes = data0
    head_rewritten = False   # True once this run has attempted to write the head

    def _roll_back() -> bool:
        """Undo everything this run wrote: drop the segment bytes it appended
        (and the segment files it created), then put the head back as the
        freshest read saw it. A failed rotation leaves the original state and
        nothing else. Returns True when the rollback fully succeeded."""
        ok = True
        for seg_path, before in written:
            try:
                if before == 0:
                    seg_path.unlink(missing_ok=True)
                else:
                    with open(seg_path, "rb+") as fh:
                        fh.truncate(before)
            except OSError:
                ok = False
        # The head is rewritten only when this run touched it; an untouched head
        # already holds the original bytes plus any concurrent append, and
        # writing over it would be the data loss the rollback exists to prevent.
        if head_rewritten:
            try:
                _atomic_write(head, freshest)
            except OSError:
                ok = False
        return ok

    try:
        head_preimage = _write_preimage(head, data0, stamp)
        plan.preimages.append(
            str(head_preimage.relative_to(vault_root)).replace("\\", "/"))

        # Re-read: an append is carried forward, any other change aborts.
        data1 = head.read_bytes()
        if not data1.startswith(data0):
            head_preimage.unlink(missing_ok=True)
            plan.preimages.clear()
            plan.errors.append(
                f"head changed under the rotation, nothing written: {rel}")
            return plan
        tail_extra = data1[len(data0):]
        freshest = data1

        for month, start, end in groups:
            seg = head.with_name(f"{head.stem}-{month}{head.suffix}")
            block = data0[start:end]
            if seg.exists():
                prior = seg.read_bytes()
                pre = _write_preimage(seg, prior, stamp)
                plan.preimages.append(
                    str(pre.relative_to(vault_root)).replace("\\", "/"))
                if prior and not prior.endswith(b"\n"):
                    block = dominant_newline(prior) + block
                written.append((seg, len(prior)))
                with open(seg, "ab") as fh:
                    fh.write(block)
            else:
                written.append((seg, 0))
                seg.write_bytes(block)

        new_head = data0[:entries_off] + data0[boundary_off:] + tail_extra
        head_rewritten = True
        _atomic_write(head, new_head)

        # --- conservation check ------------------------------------------
        after_head = head.read_bytes()
        head_lines = count_lines(after_head)
        seg_lines = 0
        for seg, before in written:
            seg_now = seg.read_bytes()
            seg_lines += count_lines(seg_now) - count_lines(seg_now[:before])
        expected = count_lines(data1)
        if head_lines + seg_lines != expected:
            restored = _roll_back()
            plan.errors.append(
                f"line conservation failed ({head_lines}+{seg_lines}"
                f"!={expected}); {'restored' if restored else 'ROLLBACK INCOMPLETE, '
                f'pre-image kept'}: {rel}")
            return plan

        plan.applied = True
        plan.size_after = len(after_head)
        plan.lines_after = head_lines
    except OSError as exc:
        # Recovery: roll the segments back and restore the head from the
        # FRESHEST read, so a concurrent append that landed between the two
        # reads survives the failure and no orphaned segment is left holding a
        # block the head also still carries.
        restored = _roll_back()
        plan.errors.append(
            f"rotation failed: {rel} ({exc}); "
            + ("rolled back" if restored else "ROLLBACK INCOMPLETE, pre-image kept"))
    return plan


def iter_heads(logs_root: Path, agents: list[str] | None = None):
    """Every live head: `<logs>/<agent>/<agent>.md` (stem equals its folder).

    `agents` narrows the pass to the named agent folders; without it every head
    under `<logs>` is considered.
    """
    if not logs_root.is_dir():
        return
    wanted = {a.lower() for a in agents} if agents else None
    for child in sorted(logs_root.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if wanted is not None and child.name.lower() not in wanted:
            continue
        head = child / f"{child.name}.md"
        if head.is_file():
            yield head


def rotate_logs(
    vault_root: Path,
    *,
    now: datetime | None = None,
    apply: bool = False,
    agents: list[str] | None = None,
    config_text: str = _CONFIG_TEXT,
) -> list[HeadPlan]:
    """Rotate every aged head under `<logs>`. Returns one plan per head."""
    vault_root = Path(vault_root)
    now = now or datetime.now(timezone.utc)
    size_kb, age_days = parse_rotation_policy(config_text)
    logs_root = vault_root / token_path("logs", "Archive/Logs")
    return [
        rotate_head(head, vault_root, now=now,
                    size_limit_bytes=size_kb * 1024,
                    age_window_days=age_days, apply=apply)
        for head in iter_heads(logs_root, agents)
    ]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _report(plans: list[HeadPlan], now: datetime, apply: bool) -> int:
    stamp = now.strftime("%Y-%m-%dT%H:%MZ")
    errors = 0
    for p in plans:
        if p.errors:
            errors += len(p.errors)
            for e in p.errors:
                print(f"{stamp} | rotate-logs | rotation-failed | {p.head} | {e}")
            continue
        if not p.months:
            code = "rotation-skipped"
            value = (f"{p.size_before}B, {p.lines_before} lines; "
                     f"{'triggered ' + p.reason + ', no closed month' if p.triggered else 'under thresholds'}")
            print(f"{stamp} | rotate-logs | {code} | {p.head} | {value}")
            continue
        segs = ", ".join(f"{p.segments[m]} ({p.segment_lines[m]} lines)"
                         for m in p.months)
        code = "log-rotated" if p.applied else "log-rotation-planned"
        print(f"{stamp} | rotate-logs | {code} | {p.head} | "
              f"{p.reason}; closed {segs}; head {p.size_before}B/"
              f"{p.lines_before} lines -> {p.size_after}B/{p.lines_after} lines")
    if not apply:
        print(f"{stamp} | rotate-logs | dry-run | - | no file written")
    return 1 if errors else 0


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Rotate aged agent event logs (CONFIG § Log files).")
    parser.add_argument("--vault-root", type=Path, default=None)
    parser.add_argument("--apply", action="store_true",
                        help="Perform the rotation. Without it, dry-run.")
    parser.add_argument("--agent", action="append", default=None, metavar="NAME",
                        help="Limit the pass to this agent's log folder "
                             "(repeatable). Default: every head under <logs>.")
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args(argv)

    if args.self_test:
        return _run_self_tests()

    vault_root = resolve_vault_root(args.vault_root)
    if vault_root is None or not Path(vault_root).is_dir():
        if args.vault_root is None:
            return _run_self_tests()
        sys.exit(f"Error: vault root is not a directory: {vault_root}")

    now = datetime.now(timezone.utc)
    size_kb, age_days = parse_rotation_policy()
    mode = "APPLIED" if args.apply else "DRY-RUN"
    print(f"[{mode}] limit={size_kb}KB age-window={age_days}d "
          f"logs={token_path('logs', 'Archive/Logs')}"
          f"{' agents=' + ','.join(args.agent) if args.agent else ''}")
    plans = rotate_logs(Path(vault_root), now=now, apply=args.apply,
                        agents=args.agent)
    return _report(plans, now, args.apply)


# ---------------------------------------------------------------------------
# Self-tests
# ---------------------------------------------------------------------------

def _run_self_tests() -> int:
    import tempfile

    failures = 0

    def fail(msg: str) -> None:
        nonlocal failures
        print(f"FAIL {msg}")
        failures += 1

    now = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)
    logs_rel = token_path("logs", "Archive/Logs")

    def build(vault: Path, name: str, body: bytes) -> Path:
        d = vault / logs_rel / name
        d.mkdir(parents=True, exist_ok=True)
        head = d / f"{name}.md"
        head.write_bytes(body)
        return head

    # CRLF head, UTF-8 arrows and em-dashes, two closing months.
    crlf_body = (
        "<!-- demo-agent event log — append-only -->\r\n"
        "\r\n"
        "2026-05-02T10:00Z | demo-agent | run-start | Inbox | May → first\r\n"
        "2026-06-01T10:00Z | demo-agent | run-start | Inbox | June — second\r\n"
        "\r\n"
        "2026-06-30T23:00Z | demo-agent | run-end | Inbox | June — last\r\n"
        "2026-07-01T09:00Z | demo-agent | run-start | Inbox | July → keeps\r\n"
        "2026-07-26T09:00Z | demo-agent | run-end | Inbox | July — keeps\r\n"
    ).encode("utf-8")

    with tempfile.TemporaryDirectory() as tmp:
        vault = Path(tmp)
        head = build(vault, "demo-agent", crlf_body)
        before_lines = count_lines(crlf_body)

        # --- A: dry run plans the split and writes nothing -----------------
        plans = rotate_logs(vault, now=now, apply=False)
        p = plans[0]
        if p.months != ["2026-05", "2026-06"]:
            fail(f"A months {p.months}")
        if head.read_bytes() != crlf_body:
            fail("A dry run mutated the head")
        if list(vault.glob(f"{logs_rel}/demo-agent/*pre-rotation*")):
            fail("A dry run wrote a pre-image")
        if not failures:
            print("OK   case A: dry run plans both closed months, writes nothing")

        # --- B: apply partitions byte-exactly, conserving lines ------------
        p = rotate_logs(vault, now=now, apply=True)[0]
        may = head.with_name("demo-agent-2026-05.md")
        jun = head.with_name("demo-agent-2026-06.md")
        new_head = head.read_bytes()
        if not (may.exists() and jun.exists()):
            fail("B segments missing")
        else:
            total = count_lines(new_head) + count_lines(may.read_bytes()) + \
                count_lines(jun.read_bytes())
            if total != before_lines:
                fail(f"B conservation {total} != {before_lines}")
            if may.read_bytes() + jun.read_bytes() not in crlf_body:
                fail("B segment bytes are not a verbatim slice of the original")
            if not new_head.startswith(
                    "<!-- demo-agent event log — append-only -->\r\n".encode()):
                fail("B preamble left the head")
            if b"2026-06-30" in new_head or b"2026-05-02" in new_head:
                fail("B a closed month stayed in the head")
            if b"2026-07-01" not in new_head or b"2026-07-26" not in new_head:
                fail("B the current month left the head")
            if b"\r\n" not in jun.read_bytes():
                fail("B CRLF endings were not preserved")
            if "→".encode() not in may.read_bytes():
                fail("B UTF-8 content did not survive")
        if not list(head.parent.glob("2026-07-26-demo-agent-pre-rotation.md")):
            fail("B no pre-image archived")
        if not failures:
            print("OK   case B: apply closes both months byte-exactly, "
                  "conserves lines, keeps the preamble and CRLF")

        # --- C: second run rotates nothing ---------------------------------
        snapshot = head.read_bytes()
        p2 = rotate_logs(vault, now=now, apply=True)[0]
        if p2.months or p2.applied:
            fail(f"C re-run rotated {p2.months}")
        if head.read_bytes() != snapshot:
            fail("C re-run rewrote the head")
        if not failures:
            print("OK   case C: the second run rotates nothing (idempotent)")

    with tempfile.TemporaryDirectory() as tmp:
        # --- D: an untriggered head is untouched ---------------------------
        vault = Path(tmp)
        small = ("2026-07-20T10:00Z | demo-agent | run-start | Inbox | small\r\n"
                 "2026-07-21T10:00Z | demo-agent | run-end | Inbox | small\r\n"
                 ).encode("utf-8")
        head = build(vault, "demo-agent", small)
        p = rotate_logs(vault, now=now, apply=True)[0]
        if p.triggered or p.months or head.read_bytes() != small:
            fail(f"D a small current-month head rotated: {p}")
        else:
            print("OK   case D: a head under both thresholds is left alone")

        # --- E: an aged head with no closed month is a no-op ---------------
        aged = ("2026-07-01T10:00Z | demo-agent | run-start | Inbox | " +
                "x" * 200 + "\r\n") * 600
        head.write_bytes(aged.encode("utf-8"))
        p = rotate_logs(vault, now=now, apply=True)[0]
        if not p.triggered:
            fail("E oversize head did not trigger")
        if p.months or head.read_bytes() != aged.encode("utf-8"):
            fail("E an all-current-month head was partitioned")
        if not failures:
            print("OK   case E: an oversize head holding only the current "
                  "month rotates nothing")

    with tempfile.TemporaryDirectory() as tmp:
        # --- F: a concurrent append is carried into the new head -----------
        vault = Path(tmp)
        head = build(vault, "demo-agent", crlf_body)
        extra = ("2026-07-26T11:59Z | demo-agent | note | Inbox | appended "
                 "mid-rotation\r\n").encode("utf-8")
        real_read = Path.read_bytes
        state = {"n": 0}

        def patched(self):                       # noqa: ANN001
            data = real_read(self)
            if self == head:
                state["n"] += 1
                if state["n"] == 2:              # the re-read before the write
                    with open(self, "ab") as fh:
                        fh.write(extra)
                    return data + extra
            return data

        Path.read_bytes = patched                # noqa: B010
        try:
            p = rotate_logs(vault, now=now, apply=True)[0]
        finally:
            Path.read_bytes = real_read           # noqa: B010
        head_bytes = head.read_bytes()
        if extra not in head_bytes:
            fail("F a concurrent append was lost")
        if p.errors:
            fail(f"F errors {p.errors}")
        jun = head.with_name("demo-agent-2026-06.md")
        total = count_lines(head_bytes) + count_lines(
            head.with_name("demo-agent-2026-05.md").read_bytes()) + \
            count_lines(jun.read_bytes())
        if total != count_lines(crlf_body + extra):
            fail(f"F conservation with the append: {total}")
        if not failures:
            print("OK   case F: an append landing mid-rotation survives, "
                  "lines still conserve")

    with tempfile.TemporaryDirectory() as tmp:
        # --- G: an existing segment is appended to, never overwritten ------
        vault = Path(tmp)
        head = build(vault, "demo-agent", crlf_body)
        prior = "2026-06-01T00:01Z | demo-agent | earlier | Inbox | kept\r\n".encode()
        seg = head.with_name("demo-agent-2026-06.md")
        seg.write_bytes(prior)
        rotate_logs(vault, now=now, apply=True)
        merged = seg.read_bytes()
        if not merged.startswith(prior):
            fail("G an existing segment lost its head lines")
        if "June — last".encode("utf-8") not in merged:
            fail("G the closing block did not append")
        if not list(seg.parent.glob("2026-07-26-demo-agent-2026-06-pre-rotation.md")):
            fail("G no pre-image for the rewritten segment")
        if not failures:
            print("OK   case G: an existing segment is pre-imaged and appended to")

    with tempfile.TemporaryDirectory() as tmp:
        # --- I: a failed rotation leaves the original state and nothing else --
        # A write dies partway through the segments WHILE a concurrent append is
        # landing. The recovery restores the head from the freshest read (the
        # append survives) and removes every segment this run wrote (no closed
        # block lives in both the head and an orphan segment).
        vault = Path(tmp)
        head = build(vault, "demo-agent", crlf_body)
        extra = ("2026-07-26T11:58Z | demo-agent | note | Inbox | appended "
                 "during a failing rotation\r\n").encode("utf-8")
        may = head.with_name("demo-agent-2026-05.md")
        jun = head.with_name("demo-agent-2026-06.md")
        real_read = Path.read_bytes
        real_write = Path.write_bytes
        state = {"n": 0}

        def reading(self):                       # noqa: ANN001
            data = real_read(self)
            if self == head:
                state["n"] += 1
                if state["n"] == 2:              # the re-read before the write
                    with open(self, "ab") as fh:
                        fh.write(extra)
                    return data + extra
            return data

        def writing(self, data):                 # noqa: ANN001
            if self.name == jun.name:            # dies after the May segment
                raise OSError("injected: disk full writing the June segment")
            return real_write(self, data)

        Path.read_bytes = reading                # noqa: B010
        Path.write_bytes = writing               # noqa: B010
        try:
            p = rotate_logs(vault, now=now, apply=True)[0]
        finally:
            Path.read_bytes = real_read           # noqa: B010
            Path.write_bytes = real_write         # noqa: B010

        head_bytes = head.read_bytes()
        if p.applied:
            fail("I a failed rotation reported applied")
        if not any("rotation failed" in e for e in p.errors):
            fail(f"I the failure was not reported: {p.errors}")
        if extra not in head_bytes:
            fail("I the concurrent append was discarded by the recovery")
        if head_bytes != crlf_body + extra:
            fail("I the restored head is not the original plus the append")
        if may.exists() or jun.exists():
            fail("I a segment survived a failed rotation")
        if not failures:
            print("OK   case I: a failed rotation restores the head from the "
                  "freshest read and leaves no segment behind")

    # --- H: policy parse -------------------------------------------------
    kb, days = parse_rotation_policy()
    if kb <= 0 or days <= 0:
        fail(f"H policy parse {kb},{days}")
    else:
        print(f"OK   case H: policy from CONFIG — {kb} KB / {days} days")

    if failures:
        print(f"\n{failures} self-test failure(s).")
        return 1
    print("\nAll self-tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
