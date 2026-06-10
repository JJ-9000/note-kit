#!/usr/bin/env python3
"""verify_claims_log.py — append one run-summary line to the verify-claims log.

Trigger: end of each verify-claims run (Phase 5). Registered in
CONFIG.md § Helper-script automation.

Keeps per-run verdict stats OUT of the verified note's frontmatter by
appending a single kit-format pipe line per run to:

    <archive>/99-Logs/verify-claims/verify-claims-runs.md

Line format (the kit's run-log format — parsed, not read):

    <ISO-8601 UTC ts> | verify-claims | run | <source-rel-path> | total=N verified=A disputed=B unresolved=C

The analyst-agent reads this file on its weekly sweep.

Usage:
    python verify_claims_log.py --source <rel-path> \
        --total N --verified A --disputed B --unresolved C [--vault-root PATH]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# --- Path bootstrap (same pattern as build_state_index.py) -------------
_SCRIPTS_DIR = Path(__file__).resolve().parent
_KIT_ROOT = _SCRIPTS_DIR.parent
sys.path.insert(0, str(_SCRIPTS_DIR))

from config_variables import _folder_by_semantic  # noqa: E402


def _resolve_vault_root(cli_root: Path | None, archive: str) -> Path | None:
    """Vault root, in priority order: --vault-root, env var, installed layout.

    Installed, the kit root IS a vault's `.claude/` directory, so the vault
    root is its parent — recognized by the vault archive (the log's
    destination root) sitting there. The kit-source checkout shares the
    `.claude` name but its parent is a repo with no archive, so it is
    deliberately not detected.
    """
    if cli_root is not None:
        return cli_root
    env_root = os.environ.get("NOTE_KIT_VAULT_ROOT")
    if env_root:
        return Path(env_root)
    if _KIT_ROOT.name == ".claude":
        candidate = _KIT_ROOT.parent
        if (candidate / archive).is_dir():
            return candidate
    return None


def main() -> int:
    p = argparse.ArgumentParser(
        description="Append a verify-claims run line to the archive log."
    )
    p.add_argument("--source", required=True,
                   help="Relative path of the verified document.")
    p.add_argument("--total", type=int, required=True)
    p.add_argument("--verified", type=int, required=True)
    p.add_argument("--disputed", type=int, required=True)
    p.add_argument("--unresolved", type=int, required=True)
    p.add_argument("--vault-root", type=Path, default=None,
                   help="Vault root. Falls back to NOTE_KIT_VAULT_ROOT, then to "
                        "detecting the installed layout (this script under "
                        "<vault>/.claude/scripts/); errors out if none resolves.")
    args = p.parse_args()

    archive = _folder_by_semantic("archive")  # e.g. "99-Archive"
    if not archive:
        sys.exit("Error: could not resolve the <archive> folder from CONFIG.md.")

    vault_root = _resolve_vault_root(args.vault_root, archive)
    if vault_root is None:
        sys.exit(
            "Error: no vault root resolved. Pass --vault-root, set "
            "NOTE_KIT_VAULT_ROOT, or run the copy installed at "
            f"<vault>/.claude/scripts/ (kit root: {_KIT_ROOT}). "
            "Refusing to write a log outside a vault."
        )
    vault_root = vault_root.resolve()
    if not vault_root.is_dir():
        sys.exit(f"Error: vault root is not a directory: {vault_root}")

    log_dir = vault_root / archive / "99-Logs" / "verify-claims"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "verify-claims-runs.md"

    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    outcome = (f"total={args.total} verified={args.verified} "
               f"disputed={args.disputed} unresolved={args.unresolved}")
    line = f"{ts} | verify-claims | run | {args.source} | {outcome}\n"

    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(line)

    print(f"verify-claims run logged: {log_path}")
    print(line.strip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
