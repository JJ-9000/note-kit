#!/usr/bin/env python3
"""
install_daemon.py
=================

One-command installer for the note-kit vault-search daemon. Cross-platform,
standard-library only — no third-party imports, so it runs on a bare Python
before any dependency is installed.

The daemon ships inside the kit at ``<vault>/.claude/vault-search/``. This
installer makes a fresh checkout runnable:

  1. Creates a virtualenv at ``<vault>/.claude/vault-search/.venv``.
  2. ``pip install -r requirements.txt`` into that venv (FastMCP, sqlite-vec,
     sentence-transformers, watchdog, ...).
  3. Copies ``config.yaml`` and substitutes the three placeholders with real,
     absolute paths:
       <VAULT_ROOT> -> the vault root (parent of .claude/)
       <HOME>       -> the user's home directory
       <DATA_DIR>   -> <vault>/.claude/vault-search/data   (self-contained,
                       no admin rights needed, already excluded from the
                       vault scan because it lives under .claude/)
     and creates the data dir.
  4. Prints the exact command to START the daemon and a one-line health check.

It does NOT start the server. The daemon binds 127.0.0.1:8765; on a machine
already running a vault-search daemon that port is taken, and starting a second
copy would fail. Starting is a deliberate, separate step the installer only
prints.

Re-running is safe: an existing venv is reused (pass --recreate-venv to rebuild
it), and config.yaml is re-substituted from the template each run.

Usage:
    python install_daemon.py [--vault-root <vault>] [--skip-deps]
                             [--recreate-venv] [--data-dir <dir>]

    --vault-root    Vault root. Defaults to the parent of the .claude/ dir this
                    script lives in (auto-detected), so no argument is needed for
                    a normal install.
    --data-dir      Override where index.db + daemon.log go. Defaults to
                    <vault>/.claude/vault-search/data.
    --skip-deps     Skip the pip install step (venv is still created/checked).
    --recreate-venv Delete and rebuild the venv even if one exists.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

# This script lives at <vault>/.claude/vault-search/install_daemon.py.
_DAEMON_DIR = Path(__file__).resolve().parent          # .claude/vault-search
_CLAUDE_DIR = _DAEMON_DIR.parent                        # .claude
_DEFAULT_VAULT_ROOT = _CLAUDE_DIR.parent                # vault root

_CONFIG_TEMPLATE = _DAEMON_DIR / "config.yaml"
_REQUIREMENTS = _DAEMON_DIR / "requirements.txt"
_VENV_DIR = _DAEMON_DIR / ".venv"


def _venv_python(venv_dir: Path) -> Path:
    """Path to the python executable inside a venv, per OS layout."""
    if os.name == "nt":
        return venv_dir / "Scripts" / "python.exe"
    return venv_dir / "bin" / "python"


def _create_venv(venv_dir: Path, recreate: bool) -> Path:
    """Create (or reuse) a venv; return the path to its python interpreter."""
    py = _venv_python(venv_dir)
    if venv_dir.exists():
        if recreate:
            print(f"[install] Removing existing venv at {venv_dir} (--recreate-venv).")
            shutil.rmtree(venv_dir)
        elif py.exists():
            print(f"[install] Reusing existing venv at {venv_dir}.")
            return py
        else:
            # Directory exists but no interpreter — a half-built venv. Rebuild.
            print(f"[install] venv at {venv_dir} is incomplete; rebuilding.")
            shutil.rmtree(venv_dir)

    print(f"[install] Creating venv at {venv_dir} ...")
    venv.EnvBuilder(with_pip=True, clear=False, upgrade=False).create(str(venv_dir))
    if not py.exists():
        raise RuntimeError(f"venv created but interpreter missing at {py}")
    return py


def _pip_install(py: Path, requirements: Path) -> bool:
    """Install requirements into the venv. Returns True on success.

    Network is required (sqlite-vec, sentence-transformers, ... come from PyPI).
    On failure we DON'T abort the whole install — config substitution is still
    useful offline — but we report the exact command to retry.
    """
    if not requirements.exists():
        print(f"[install] WARNING: requirements.txt not found at {requirements}; "
              "skipping dependency install.")
        return False

    print(f"[install] Upgrading pip in the venv ...")
    up = subprocess.run(
        [str(py), "-m", "pip", "install", "--upgrade", "pip"],
        capture_output=True, text=True,
    )
    if up.returncode != 0:
        # Non-fatal: an older pip can still install the pinned deps.
        print("[install] (pip self-upgrade skipped — continuing with bundled pip.)")

    print(f"[install] Installing dependencies from {requirements.name} "
          "(this can take a few minutes the first time) ...")
    proc = subprocess.run(
        [str(py), "-m", "pip", "install", "-r", str(requirements)],
        text=True,
    )
    if proc.returncode != 0:
        print(
            "\n[install] WARNING: dependency install FAILED "
            f"(pip exited {proc.returncode}).\n"
            "          The most common cause is no network access. "
            "The rest of the install (config + data dir) still completed.\n"
            "          Retry the dependency step manually with:\n"
            f"            {_quote(py)} -m pip install -r {_quote(requirements)}\n"
        )
        return False
    print("[install] Dependencies installed.")
    return True


def _substitute_config(
    template: Path, dest: Path, vault_root: Path, home: Path, data_dir: Path
) -> None:
    """Copy config.yaml and replace <VAULT_ROOT>, <HOME>, <DATA_DIR>.

    Paths are written with forward slashes — PyYAML reads them fine on every OS
    and they avoid backslash-escaping surprises in the YAML string values.
    """
    text = template.read_text(encoding="utf-8")
    replacements = {
        "<VAULT_ROOT>": vault_root.as_posix(),
        "<HOME>": home.as_posix(),
        "<DATA_DIR>": data_dir.as_posix(),
    }
    for placeholder, value in replacements.items():
        text = text.replace(placeholder, value)

    leftovers = [p for p in replacements if p in text]
    if leftovers:
        raise RuntimeError(
            f"config substitution left unresolved placeholders: {leftovers}"
        )

    dest.write_text(text, encoding="utf-8")
    print(f"[install] Wrote config: {dest}")
    print(f"            vault_path -> {replacements['<VAULT_ROOT>']}")
    print(f"            data dir   -> {replacements['<DATA_DIR>']}")


def _quote(p: Path | str) -> str:
    """Quote a path for display in a copy-pasteable shell command if it has spaces."""
    s = str(p)
    return f'"{s}"' if " " in s else s


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Install the note-kit vault-search daemon (venv + deps + config). "
                    "Does NOT start the server."
    )
    parser.add_argument(
        "--vault-root", type=Path, default=None,
        help="Vault root. Defaults to the parent of this .claude/ directory.",
    )
    parser.add_argument(
        "--data-dir", type=Path, default=None,
        help="Where index.db and daemon.log live. "
             "Defaults to <vault>/.claude/vault-search/data.",
    )
    parser.add_argument(
        "--skip-deps", action="store_true",
        help="Skip the pip install step (still creates/checks the venv).",
    )
    parser.add_argument(
        "--recreate-venv", action="store_true",
        help="Delete and rebuild the venv even if one exists.",
    )
    args = parser.parse_args()

    vault_root = (args.vault_root or _DEFAULT_VAULT_ROOT).resolve()
    home = Path(os.path.expanduser("~")).resolve()
    data_dir = (args.data_dir or (_DAEMON_DIR / "data")).resolve()

    print("note-kit vault-search installer")
    print(f"  daemon dir : {_DAEMON_DIR}")
    print(f"  vault root : {vault_root}")
    print(f"  data dir   : {data_dir}")
    print()

    if not _CONFIG_TEMPLATE.exists():
        print(f"[install] ERROR: config template not found at {_CONFIG_TEMPLATE}.",
              file=sys.stderr)
        return 1
    if not vault_root.is_dir():
        print(f"[install] ERROR: vault root {vault_root} is not a directory.",
              file=sys.stderr)
        return 1

    # 1 + 2. venv and dependencies.
    try:
        py = _create_venv(_VENV_DIR, recreate=args.recreate_venv)
    except Exception as exc:
        print(f"[install] ERROR creating venv: {exc}", file=sys.stderr)
        return 1

    deps_ok = True
    if args.skip_deps:
        print("[install] --skip-deps: not installing dependencies.")
    else:
        deps_ok = _pip_install(py, _REQUIREMENTS)

    # 3. Data dir + config substitution.
    data_dir.mkdir(parents=True, exist_ok=True)
    print(f"[install] Data dir ready: {data_dir}")
    try:
        _substitute_config(_CONFIG_TEMPLATE, _CONFIG_TEMPLATE, vault_root, home, data_dir)
    except Exception as exc:
        print(f"[install] ERROR writing config: {exc}", file=sys.stderr)
        return 1

    # 4. Print next steps. Never start the server.
    start_cmd = f"{_quote(py)} {_quote(_DAEMON_DIR / 'server.py')}"
    print()
    print("=" * 70)
    print("Install complete." if deps_ok else
          "Install finished WITH WARNINGS (dependencies not fully installed).")
    print("=" * 70)
    print()
    daemonctl = _DAEMON_DIR / "daemonctl.py"
    print("Next steps:")
    print()
    print("  1. Start the daemon in the background with the control helper:")
    print()
    print(f"       python {_quote(daemonctl)} start")
    print()
    print("     First start downloads a small embedding model (~90 MB) and builds")
    print("     the index, so it needs network and takes a minute or two.")
    print("     To run it in the foreground instead, use the raw command:")
    print(f"       {start_cmd}")
    print()
    print("  2. Check it any time (or stop it with daemonctl.py stop):")
    print()
    print(f"       python {_quote(daemonctl)} status")
    print()
    print('     "running and healthy" means it is ready.')
    print()
    if not deps_ok:
        print("  NOTE: dependencies did not install — the daemon will not start until")
        print("        you complete the pip step shown above.")
        print()
    print("  The installer does NOT start the daemon: port 8765 may already be held")
    print("  by another vault-search instance, and a second copy cannot bind it.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
