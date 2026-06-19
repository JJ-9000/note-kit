"""PostToolUse hook: run sync_config when CONFIG.md is edited.

After an Edit/Write/MultiEdit tool modifies CONFIG.md, run
`scripts/sync_config.py` so the generated tables in CLAUDE.md, AGENTS.md, and
RULES.md stay in sync with the canon (CONFIG.md is the only editable home).

Silent: prints nothing and exits 0 on every path, so nothing reaches the model.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

# This hook lives at <vault>/.claude/hooks/, so its parent's parent is the kit
# root (<vault>/.claude/) and the vault root is one level above that.
_KIT_ROOT = Path(__file__).resolve().parent.parent
_VAULT_ROOT = _KIT_ROOT.parent
_SYNC_CONFIG = _KIT_ROOT / "scripts" / "sync_config.py"

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit"}


def _vault_root() -> Path:
    """Vault root for sync_config's --vault-root.

    Honors NOTE_KIT_VAULT_ROOT when set (explicit override for non-standard
    installs); otherwise derives it as the parent of the installed `.claude/`
    directory. Without this, sync_config falls back to its kit-source dev paths
    and writes the wrong files on a real `.claude/` install.
    """
    env = os.environ.get("NOTE_KIT_VAULT_ROOT")
    return Path(env).resolve() if env else _VAULT_ROOT


def _is_config_edit(data: dict) -> bool:
    """True when an Edit/Write/MultiEdit tool modified CONFIG.md."""
    if data.get("tool_name") not in _EDIT_TOOLS:
        return False
    fp = (data.get("tool_input") or {}).get("file_path", "")
    if not isinstance(fp, str) or not fp:
        return False
    norm = fp.replace("\\", "/")
    return norm.endswith("/CONFIG.md") or norm == "CONFIG.md"


def _run_sync_config() -> None:
    """Run sync_config.py silently; swallow all output and errors.

    The subprocess timeout sits just under the hook's own settings.json timeout
    so the inner timeout wins gracefully (the Exception is swallowed) rather
    than the harness hard-killing the hook.
    """
    if not _SYNC_CONFIG.exists():
        return
    try:
        subprocess.run(
            [sys.executable, str(_SYNC_CONFIG), "--vault-root", str(_vault_root())],
            capture_output=True,
            text=True,
            timeout=55,
        )
    except Exception:
        pass


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    if _is_config_edit(data):
        _run_sync_config()

    # Always silent: no stdout, exit 0, so nothing is shown to the model.
    return


if __name__ == "__main__":
    main()
