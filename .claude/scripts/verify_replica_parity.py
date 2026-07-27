#!/usr/bin/env python3
"""verify_replica_parity.py
============================

Check THIS machine's markdown against the synced state snapshot — the instrument
for an Obsidian-Sync partition (Note-Kit-Structural-Integrity-Plan, Stream A).

`<logs>/Vault-State-Index.md` carries a `## Content hashes` table: one
`path | body-md5` row per vault markdown file, written by `build_state_index`
on the machine that built it. That table syncs like any other note, so every
replica holds the builder's account of what the vault contains. This script
walks THAT ACCOUNT — every snapshot row, one at a time — and re-derives each row
from the LOCAL disk: a path the snapshot lists but this machine lacks is a
surface Sync never delivered; a path whose local body hash differs is a surface
Sync delivered stale. Both emit `replica-divergence`.

The walk runs one direction only, snapshot to disk, so what it measures is
missing and stale LOCAL surfaces. A local file the snapshot does not list is out
of scope here: it may be a note authored on this replica since the build, so
reading it as divergence would report every fresh local edit. The reverse
direction — surfaces this machine holds and the builder never saw — is the
builder's own inventory to report on its next pass.

Stdlib only, read-only, and no dependence on the kit's other modules or on the
vault-search daemon — so it runs on any machine holding a replica, including one
where `.claude/` itself never synced (copy this one file across and point it at
the vault).

Usage
-----
    # From anywhere, naming the replica explicitly:
    python verify_replica_parity.py --vault "/path/to/Vault"

    # From inside a replica that carries the kit (vault root inferred from the
    # script's own location, two levels above .claude/scripts/):
    python verify_replica_parity.py

    # Honouring the janitor's environment, as a scheduled pass would:
    JANITOR_VAULT_ROOT=/path/to/Vault python verify_replica_parity.py

    # Feeding the findings back into the snapshot on the next build:
    python verify_replica_parity.py --findings-out /tmp/replica.txt
    JANITOR_VAULT_ROOT=/path/to/Vault python build_state_index.py \
        --findings /tmp/replica.txt

Vault root resolution, first hit wins: `--vault`, `$JANITOR_VAULT_ROOT`, the
script's own grandparent (the `<vault>/.claude/scripts/` install), the current
directory. A `--vault` that names no directory ENDS THE RUN — an explicit
argument states which tree the caller means, and falling through to the next
candidate would answer about a different tree under the caller's own label. The
candidate chain stays in force when `--vault` goes unnamed.

The snapshot is `--snapshot` when given, else
`<vault>/Archive/Logs/Vault-State-Index.md`, else the first `Vault-State-Index.md`
found under the vault outside dot-directories — an install that renamed its
`<logs>` folder still resolves.

Exit status: 0 parity, 1 divergence found, 2 the check could not run (a `--vault`
naming no directory, no vault, no snapshot, or no `## Content hashes` section to
compare against). A run that cannot compare exits 2 rather than reporting parity
— silence is not evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ACTOR = "verify-replica-parity"
CODE = "replica-divergence"

_FM_FENCE = re.compile(r"^---\s*$", re.MULTILINE)
_HASH_ROW = re.compile(r"^\|\s*`([^`]+)`\s*\|\s*([0-9a-f]{32})\s*\|$")
_SECTION = re.compile(r"^## Content hashes\s*$", re.MULTILINE)
_NEXT_H2 = re.compile(r"^## ", re.MULTILINE)


def body_md5(text: str) -> str:
    """md5 of a note's BODY, frontmatter excluded.

    Byte-for-byte the rule `build_state_index._body_md5` applies when it writes
    the snapshot: strip only a leading frontmatter block (the file opens with
    `---` and carries a closing fence), hash the remainder as UTF-8 with
    replacement. A frontmatter-only difference between replicas — a `reviewed`
    flip mid-sync — is therefore not a divergence, exactly as on the builder.
    """
    body = text
    if text.startswith("---"):
        fences = list(_FM_FENCE.finditer(text))
        if len(fences) >= 2 and fences[0].start() == 0:
            body = text[fences[1].end():]
    return hashlib.md5(body.encode("utf-8", errors="replace")).hexdigest()


def resolve_vault(arg_vault: str | None) -> Path | None:
    """Resolve the vault root from the candidate chain.

    `arg_vault` arrives here only after main() has confirmed it names a
    directory, so an explicit `--vault` is either honoured or has already ended
    the run — it never falls through to a later candidate.
    """
    candidates: list[Path] = []
    if arg_vault:
        candidates.append(Path(arg_vault))
    env = os.environ.get("JANITOR_VAULT_ROOT")
    if env:
        candidates.append(Path(env))
    here = Path(__file__).resolve()
    if here.parent.name == "scripts" and here.parent.parent.name == ".claude":
        candidates.append(here.parent.parent.parent)
    candidates.append(Path.cwd())
    for c in candidates:
        try:
            if c.is_dir():
                return c.resolve()
        except OSError:
            continue
    return None


def resolve_snapshot(vault: Path, arg_snapshot: str | None) -> Path | None:
    if arg_snapshot:
        p = Path(arg_snapshot)
        return p if p.is_file() else None
    default = vault / "Archive" / "Logs" / "Vault-State-Index.md"
    if default.is_file():
        return default
    for dirpath, dirnames, filenames in os.walk(vault):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        if "Vault-State-Index.md" in filenames:
            return Path(dirpath) / "Vault-State-Index.md"
    return None


def read_snapshot_hashes(text: str) -> dict[str, str]:
    m = _SECTION.search(text)
    if not m:
        return {}
    after = text[m.end():]
    nxt = _NEXT_H2.search(after)
    section = after[: nxt.start()] if nxt else after
    out: dict[str, str] = {}
    for line in section.splitlines():
        mm = _HASH_ROW.match(line.strip())
        if mm:
            out[mm.group(1)] = mm.group(2)
    return out


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Check every row of the synced Vault-State-Index.md snapshot "
                    "against this replica's disk, reporting the surfaces that "
                    "are missing or stale locally.",
    )
    parser.add_argument("--vault", default=None,
                        help="Vault root; naming a path that is not a directory "
                             "ends the run (exit 2) rather than falling back. "
                             "Unnamed, defaults to $JANITOR_VAULT_ROOT, then "
                             "this script's install root, then the cwd.")
    parser.add_argument("--snapshot", default=None,
                        help="Path to Vault-State-Index.md. Defaults to "
                             "<vault>/Archive/Logs/Vault-State-Index.md.")
    parser.add_argument("--findings-out", default=None,
                        help="Write `code | target | value` lines here, the "
                             "format build_state_index --findings reads.")
    parser.add_argument("--max-rows", type=int, default=200,
                        help="Cap on reported divergence rows (default 200); "
                             "the total is always stated.")
    args = parser.parse_args()

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # An explicit --vault is the tree the caller means. When it names no
    # directory, the run ends: falling through to $JANITOR_VAULT_ROOT, the
    # install root, or the cwd would compare some OTHER tree and print parity
    # under the path the caller typed — a typo answered with a clean bill.
    if args.vault is not None and not Path(args.vault).is_dir():
        print(f"{ts} | {ACTOR} | replica-check-blocked | {args.vault} | "
              f"--vault names no directory — cannot compare, and no fallback "
              f"tree is substituted for an explicitly named one")
        return 2

    vault = resolve_vault(args.vault)
    if vault is None:
        print(f"{ts} | {ACTOR} | replica-check-blocked | - | no vault root resolved")
        return 2

    snapshot = resolve_snapshot(vault, args.snapshot)
    if snapshot is None:
        print(f"{ts} | {ACTOR} | replica-check-blocked | {vault} | "
              f"no Vault-State-Index.md found — this replica never received the "
              f"snapshot, which is itself a partition symptom")
        return 2

    try:
        snap_text = snapshot.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        print(f"{ts} | {ACTOR} | replica-check-blocked | {snapshot} | "
              f"snapshot unreadable ({exc})")
        return 2

    expected = read_snapshot_hashes(snap_text)
    if not expected:
        print(f"{ts} | {ACTOR} | replica-check-blocked | {snapshot} | "
              f"no `## Content hashes` rows to compare against")
        return 2

    built = re.search(r"Generated by [^ ]+ at (\S+)\. Run #(\d+)", snap_text)
    origin = (f"built {built.group(1)} run #{built.group(2)}"
              if built else "build stamp not found")

    missing: list[tuple[str, str]] = []
    stale: list[tuple[str, str, str]] = []
    for rel in sorted(expected):
        local = vault / rel
        if not local.is_file():
            missing.append((rel, expected[rel]))
            continue
        try:
            local_hash = body_md5(local.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            stale.append((rel, expected[rel], f"unreadable ({exc})"))
            continue
        if local_hash != expected[rel]:
            stale.append((rel, expected[rel], local_hash))

    rows: list[tuple[str, str]] = []
    for rel, want in missing:
        rows.append((rel, f"missing locally — snapshot body-md5 {want[:8]} "
                          f"({origin})"))
    for rel, want, got in stale:
        rows.append((rel, f"stale locally — local {got[:8] if len(got) == 32 else got} "
                          f"vs snapshot {want[:8]} ({origin})"))

    total = len(rows)
    shown = rows[: max(args.max_rows, 0)]

    print(f"{ts} | {ACTOR} | replica-check | {vault} | "
          f"{len(expected)} snapshot rows, {origin}; "
          f"{len(missing)} missing, {len(stale)} stale")
    for rel, value in shown:
        print(f"{ts} | {ACTOR} | {CODE} | {rel} | {value}")
    if total > len(shown):
        print(f"{ts} | {ACTOR} | replica-divergence-truncated | {snapshot} | "
              f"{total} divergences, {len(shown)} reported (--max-rows)")

    if args.findings_out:
        out = Path(args.findings_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        lines = [f"{CODE} | {rel} | {value}" for rel, value in shown]
        if total > len(shown):
            lines.append(f"replica-divergence-truncated | {snapshot} | "
                         f"{total} divergences, {len(shown)} reported")
        out.write_text("\n".join(lines) + ("\n" if lines else ""),
                       encoding="utf-8", newline="\n")
        print(f"{ts} | {ACTOR} | findings-written | {out} | {len(lines)} line(s)")

    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
