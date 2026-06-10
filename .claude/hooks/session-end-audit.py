"""Stop hook: post-session documentation upkeep.

Two jobs, both driven by what the session actually edited (read from the
transcript):

1. If the session edited CONFIG.md, run `scripts/sync_config_v002.py` so the
   `## Session-start defaults` copies in CLAUDE.md and AGENTS.md stay in sync
   with the canon. (CONFIG.md § Helper-script automation: sync_config triggers
   at the end of any session that edited CONFIG.md.)

2. If the session edited hooks, rules, skills, CONFIG.md, or CLAUDE.md, remind
   Claude to re-check the documentation surfaces still reflect reality.
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
_SYNC_CONFIG = _KIT_ROOT / "scripts" / "sync_config_v002.py"


def _vault_root() -> Path:
    """Vault root for sync_config's --vault-root.

    Honors NOTE_KIT_VAULT_ROOT when set (explicit override for non-standard
    installs); otherwise derives it as the parent of the installed `.claude/`
    directory. Without this, sync_config falls back to its kit-source dev paths
    and writes the wrong files on a real `.claude/` install.
    """
    env = os.environ.get("NOTE_KIT_VAULT_ROOT")
    return Path(env).resolve() if env else _VAULT_ROOT

# Documentation surfaces that should stay in sync with the hooks/skills/rules/
# CONFIG corpus. Claude gets a reminder to re-read these whenever this session
# edited something that defines its behavior or inventory.
SURFACES = [
    "CONFIG.md",
    "CLAUDE.md",
    "AGENTS.md",
]
CRITICAL_PATH_PATTERNS = (
    "/hooks", "/skills", "/rules",
    "scheduled-tasks",
    "CONFIG.md", "CLAUDE.md", "AGENTS.md",
)


def _edited_paths(transcript_path: str) -> list[str]:
    """Return the file_path of every Edit/Write tool_use in the transcript."""
    if not transcript_path:
        return []
    p = Path(transcript_path)
    if not p.exists():
        return []
    edited: list[str] = []
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
                    inp = content.get("input") or {}
                    fp = inp.get("file_path", "")
                    if isinstance(fp, str) and fp:
                        edited.append(fp.replace("\\", "/"))
    except Exception:
        return edited
    return edited


def _touched_config(edited: list[str]) -> bool:
    return any(fp.endswith("/CONFIG.md") or fp == "CONFIG.md" for fp in edited)


def _touched_critical(edited: list[str]) -> bool:
    return any(
        any(pat in fp for pat in CRITICAL_PATH_PATTERNS)
        for fp in edited
    )


def _run_sync_config() -> str:
    """Run sync_config_v002.py. Returns a one-line status for the reminder."""
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
            return "sync_config ran: CLAUDE.md and AGENTS.md session-start tables refreshed from CONFIG.md."
        return f"sync_config exited {result.returncode}: {(result.stderr or '').strip()[:200]}"
    except Exception as exc:
        return f"sync_config failed to run: {exc}"


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}

    edited = _edited_paths(data.get("transcript_path", ""))

    notes: list[str] = []

    # Job 1 — sync config copies if CONFIG.md changed.
    if _touched_config(edited):
        notes.append(_run_sync_config())

    # Job 2 — documentation-surface reminder.
    if not _touched_critical(edited):
        # Nothing behavior-defining changed. If we still ran sync, surface that;
        # otherwise stay silent.
        if notes:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": "SESSION-END: " + " ".join(notes),
                }
            }))
        return

    msg_lines = ["SESSION-END AUDIT: this session edited hooks, rules, skills, CONFIG.md, or CLAUDE.md."]
    if notes:
        msg_lines.append(" ".join(notes))
    msg_lines.append("Verify the documentation surfaces below still reflect reality:")
    msg_lines.extend(f"- {s}" for s in SURFACES)

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "Stop",
            "additionalContext": "\n".join(msg_lines),
        }
    }))


if __name__ == "__main__":
    main()
