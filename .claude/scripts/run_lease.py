"""
run_lease.py
============

Acquire and release the vault-wide run lease — CONFIG § Concurrency rule 1 as
one importable, exception-safe context manager.

> One mutating agent at a time, vault-wide. A run takes the lease file
> `<logs>/run-lease.md` (one line: agent, start time) and releases it at exit; a
> second mutating agent finding a fresh lease waits for its next cadence. A
> stale lease past 2 hours is expired, logged, and taken over.

The lease file holds exactly one active line when a run holds the lease and is
empty when released. The line format matches the live file verbatim:

    <ISO-8601Z> | <agent> | lease-taken | <note>

Timestamps are UTC at minute precision with a ``Z`` suffix
(``2026-07-25T03:30Z``), matching the live ``<logs>/run-lease.md``.

Because the lease file carries only the one active line and is cleared on
release, a takeover of an expired lease is recorded to a companion append-only
log beside it (``run-lease-takeover.md``) so "expired, logged, and taken over"
leaves an audit trail the lease file itself cannot keep:

    <ISO-8601Z> | <agent> | lease-taken-over | took over stale lease from <prev>...

Usage::

    from run_lease import RunLease, LeaseHeldError
    try:
        with RunLease("note-kit-janitor-agent", "daily pass"):
            ...  # mutating work; the lease releases on exit, even on exception
    except LeaseHeldError:
        ...  # a fresh lease is held — defer to the next cadence

Stdlib only, plus config_variables for the vault-root resolver and `<logs>`
token (used only when no explicit ``lease_path`` is given).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

_SCRIPTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import resolve_vault_root, token_path  # noqa: E402


DEFAULT_EXPIRY_HOURS = 2
_ACTION_TAKEN = "lease-taken"
_ACTION_TAKEOVER = "lease-taken-over"
_SEP = " | "
_LEASE_FILENAME = "run-lease.md"
_TAKEOVER_FILENAME = "run-lease-takeover.md"


class LeaseHeldError(RuntimeError):
    """A fresh (non-expired) lease is held by another run.

    Raised on acquire when the lease file carries an active line younger than
    the expiry window; the second mutating agent waits for its next cadence.
    """


# ---------------------------------------------------------------------------
# Lease line format
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LeaseLine:
    timestamp: Optional[datetime]  # None when the stored timestamp is unparseable
    agent: str
    action: str
    note: str
    raw: str


def _format_ts(dt: datetime) -> str:
    """UTC, minute precision, ``Z`` suffix — matches the live lease file."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")


def _parse_ts(value: str) -> Optional[datetime]:
    """Parse an ISO-8601Z stamp (minute or second precision), UTC-aware, or None."""
    text = value.strip()
    if not text:
        return None
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(candidate)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%MZ", "%Y-%m-%dT%H:%M:%SZ"):
            try:
                dt = datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_lease_line(line: str) -> LeaseLine:
    """Parse ``<ts> | <agent> | <action> | <note>`` leniently.

    A missing field yields an empty string; an unparseable timestamp yields a
    None timestamp (treated as expired on acquire, so a corrupt lease never
    deadlocks the vault).
    """
    parts = [p.strip() for p in line.split("|")]
    ts = _parse_ts(parts[0]) if parts else None
    agent = parts[1] if len(parts) > 1 else ""
    action = parts[2] if len(parts) > 2 else ""
    note = parts[3] if len(parts) > 3 else ""
    return LeaseLine(timestamp=ts, agent=agent, action=action, note=note, raw=line.strip())


# ---------------------------------------------------------------------------
# Context manager
# ---------------------------------------------------------------------------

class RunLease:
    """Exception-safe context manager for the vault-wide run lease.

    Parameters
    ----------
    agent : str
        The run's agent name, written into the lease line.
    note : str
        Free-text note for the lease line (the live file uses e.g.
        "note-kit reform P0").
    lease_path : Path | None
        Explicit lease file. When None, resolves to
        ``<vault-root>/<logs>/run-lease.md`` via the shared vault-root resolver.
    vault_root : Path | None
        Vault root for the default lease path; resolved when None.
    expiry_hours : float
        Age past which a held lease is expired and taken over (default 2).
    clock : Callable[[], datetime] | None
        Injection seam for tests; defaults to ``datetime.now(timezone.utc)``.
    takeover_log : Path | None
        Companion takeover log; defaults to ``run-lease-takeover.md`` beside the
        lease file.
    """

    def __init__(
        self,
        agent: str,
        note: str = "",
        *,
        lease_path: Optional[Path] = None,
        vault_root: Optional[Path] = None,
        expiry_hours: float = DEFAULT_EXPIRY_HOURS,
        clock: Optional[Callable[[], datetime]] = None,
        takeover_log: Optional[Path] = None,
    ) -> None:
        self.agent = agent
        self.note = note
        self.expiry = timedelta(hours=expiry_hours)
        self.clock = clock or (lambda: datetime.now(timezone.utc))

        if lease_path is not None:
            self.lease_path = Path(lease_path)
        else:
            root = Path(vault_root) if vault_root is not None else resolve_vault_root()
            if root is None:
                raise RuntimeError(
                    "run_lease: no vault root resolved for the default lease path. "
                    "Pass lease_path or vault_root, or set NOTE_KIT_VAULT_ROOT."
                )
            self.lease_path = (root / token_path("logs") / _LEASE_FILENAME)

        self.takeover_log = (
            Path(takeover_log) if takeover_log is not None
            else self.lease_path.with_name(_TAKEOVER_FILENAME)
        )
        self._our_line: Optional[str] = None

    # -- internals ---------------------------------------------------------

    def _read_active(self) -> Optional[LeaseLine]:
        """The current active lease line, or None when the file is absent/empty."""
        if not self.lease_path.is_file():
            return None
        raw = self.lease_path.read_text(encoding="utf-8")
        for line in raw.splitlines():
            if line.strip():
                return _parse_lease_line(line)
        return None

    def _write_line(self, now: datetime) -> None:
        # Strip the composed line so the stored form matches what the read side
        # produces (`_parse_lease_line` sets `raw = line.strip()`). Without this,
        # an empty or whitespace-padded note leaves a trailing separator space
        # that the strip removes on read, so release()'s `active.raw == _our_line`
        # never matches and a completed run's lease is never cleared.
        line = f"{_format_ts(now)}{_SEP}{self.agent}{_SEP}{_ACTION_TAKEN}{_SEP}{self.note}".strip()
        self.lease_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.lease_path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
        self._our_line = line

    def _log_takeover(self, prev: LeaseLine, now: datetime) -> None:
        prev_started = _format_ts(prev.timestamp) if prev.timestamp else "unknown"
        note = (
            f"took over stale lease from {prev.agent or 'unknown'} "
            f"(started {prev_started})"
        )
        line = f"{_format_ts(now)}{_SEP}{self.agent}{_SEP}{_ACTION_TAKEOVER}{_SEP}{note}"
        self.takeover_log.parent.mkdir(parents=True, exist_ok=True)
        with open(self.takeover_log, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    # -- public ------------------------------------------------------------

    def acquire(self) -> "RunLease":
        """Take the lease, or raise LeaseHeldError when a fresh lease is held.

        An expired lease (older than the expiry window, or with an unparseable
        timestamp) is logged to the takeover log and taken over.
        """
        now = self.clock()
        active = self._read_active()
        if active is not None:
            expired = active.timestamp is None or (now - active.timestamp) >= self.expiry
            if not expired:
                raise LeaseHeldError(
                    f"lease held by {active.agent!r} since {active.raw!r}; "
                    f"not yet expired (< {self.expiry})."
                )
            self._log_takeover(active, now)
        self._write_line(now)
        return self

    def release(self) -> None:
        """Clear the lease file — but only when we hold it and it is still ours.

        A release with nothing held (release-without-acquire, or a second release
        after the first cleared `_our_line`) is a no-op, so it never clobbers a
        lease this run does not own. If a takeover replaced our (expired) lease
        while we ran, the file holds a different active line; clearing it would
        clobber the new holder, so release leaves it intact in that case too.
        """
        if self._our_line is None:
            return  # nothing held — releasing is a no-op, never touch the file
        active = self._read_active()
        if active is None or active.raw == self._our_line:
            if self.lease_path.exists():
                with open(self.lease_path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write("")
        self._our_line = None

    def __enter__(self) -> "RunLease":
        return self.acquire()

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False  # never suppress an exception raised inside the context
