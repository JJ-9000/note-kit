"""Stop hook: a session-end reminder, plus CONFIG.md sync.

Two jobs:

1. If the session edited CONFIG.md, run `scripts/sync_config.py` so the
   generated tables in CLAUDE.md and AGENTS.md stay in sync with the canon
   (CONFIG.md § Helper-script automation).

2. Remind the session to search the vault for content related to the task, and
   to update executed items on in-progress plans and the relevant project pages
   for work acted on directly.
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

REMINDER = (
    "SESSION-END: Search the vault for content related to the task. For work "
    "acted on directly, update the executed items on in-progress plans and the "
    "relevant project pages."
)


def _vault_root() -> Path:
    """Vault root for sync_config's --vault-root.

    Honors NOTE_KIT_VAULT_ROOT when set (explicit override for non-standard
    installs); otherwise derives it as the parent of the installed `.claude/`
    directory. Without this, sync_config falls back to its kit-source dev paths
    and writes the wrong files on a real `.claude/` install.
    """
    env = os.environ.get("NOTE_KIT_VAULT_ROOT")
    return Path(env).resolve() if env else _VAULT_ROOT


def _edited_config(transcript_path: str) -> bool:
    """True when the session edited CONFIG.md (any Edit/Write tool_use).

    The only transcript signal this hook needs: whether to refresh the
    generated doc surfaces. Fails open — a transcript that cannot be read
    skips the sync and still emits the reminder.
    """
    if not transcript_path:
        return False
    p = Path(transcript_path)
    if not p.exists():
        return False
    try:
        with open(p, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue
                if msg.get("type") != "assistant":
                    continue
                for content in msg.get("message", {}).get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") != "tool_use":
                        continue
                    if content.get("name") not in {"Edit", "Write"}:
                        continue
                    fp = (content.get("input") or {}).get("file_path", "")
                    if isinstance(fp, str) and fp:
                        norm = fp.replace("\\", "/")
                        if norm.endswith("/CONFIG.md") or norm == "CONFIG.md":
                            return True
    except Exception:
        return False
    return False


def _run_sync_config() -> str:
    """Run sync_config.py. Returns a one-line status for the reminder."""
    if not _SYNC_CONFIG.exists():
        return f"sync_config not found at {_SYNC_CONFIG}; skipped."
    try:
        result = subprocess.run(
            [sys.executable, str(_SYNC_CONFIG), "--vault-root", str(_vault_root())],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode == 0:
            return "sync_config ran: CLAUDE.md and AGENTS.md refreshed from CONFIG.md."
        return f"sync_config exited {result.returncode}: {(result.stderr or '').strip()[:200]}"
    except Exception as exc:
        return f"sync_config failed to run: {exc}"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    # A stop that is itself a hook-driven continuation must exit silently:
    # re-emitting context here re-invokes the session on every stop and the
    # audit loops until the session is closed (observed live 2026-06-10).
    if data.get("stop_hook_active"):
        return

    parts: list[str] = []
    if _edited_config(data.get("transcript_path", "")):
        parts.append(_run_sync_config())
    parts.append(REMINDER)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": "\n".join(parts),
        }
    }))


if __name__ == "__main__":
    main()
