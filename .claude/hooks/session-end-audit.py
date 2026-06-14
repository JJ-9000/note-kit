"""Stop hook: post-session documentation upkeep.

Three jobs, all driven by what the session actually did (read from the
transcript):

1. If the session edited CONFIG.md, run `scripts/sync_config.py` so the
   `## Session-start defaults` copies in CLAUDE.md and AGENTS.md stay in sync
   with the canon. (CONFIG.md § Helper-script automation: sync_config triggers
   at the end of any session that edited CONFIG.md.)

2. If the session edited hooks, rules, skills, CONFIG.md, or CLAUDE.md, remind
   Claude to re-check the documentation surfaces still reflect reality.

3. If the session edited vault content (outside the kit root) and no handoff
   ran, gate by volume: at or above HANDOFF_BLOCK_THRESHOLD distinct vault
   files, block the stop until note-kit-handoff runs; below it, remind only.
   An unattended scheduled-task run is exempt — its record is its event log,
   never a session (CONFIG § Helper-script automation). The check fails open —
   a transcript that cannot be read never blocks.
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

# Handoff gate (job 3): a session that edited this many distinct vault-content
# files or more blocks at stop until note-kit-handoff runs; below the
# threshold it gets a reminder.
HANDOFF_BLOCK_THRESHOLD = 3


def _is_scheduled_envelope(msg: dict) -> bool:
    """True when a user message is Claude Code's scheduled-task launch envelope.

    The scheduled-task runner injects a plain-text user message beginning with
    `<scheduled-task name="..." file="...">` for every unattended run. This is the
    canonical, name-independent marker of an automated run: it survives agent
    renames and never depends on a hardcoded agent list. Match the envelope only
    at the start of a text block — never a tool_result payload or a mid-message
    quotation — so an interactive session that merely discusses scheduled tasks is
    not mistaken for one.
    """
    content = msg.get("message", {}).get("content")
    blocks = content if isinstance(content, list) else [content]
    for b in blocks:
        text = b if isinstance(b, str) else (b.get("text") if isinstance(b, dict) else None)
        if isinstance(text, str) and text.lstrip().startswith("<scheduled-task"):
            return True
    return False


def _scan_transcript(transcript_path: str) -> tuple[list[str], bool, bool]:
    """One pass over the transcript: (edited file paths, handoff ran, scheduled).

    Edited paths are the file_path of every Edit/Write tool_use. Handoff is
    detected by a Skill tool_use whose skill names a handoff. Scheduled is true
    when the run carries Claude Code's `<scheduled-task>` launch envelope — an
    unattended agent run, whose record is its event log, never a session.
    """
    if not transcript_path:
        return [], False, False
    p = Path(transcript_path)
    if not p.exists():
        return [], False, False
    edited: list[str] = []
    handoff_ran = False
    scheduled = False
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
                mtype = msg.get("type")
                if mtype == "user":
                    if not scheduled and _is_scheduled_envelope(msg):
                        scheduled = True
                    continue
                if mtype != "assistant":
                    continue
                for content in msg.get("message", {}).get("content") or []:
                    if not isinstance(content, dict):
                        continue
                    if content.get("type") != "tool_use":
                        continue
                    name = content.get("name")
                    inp = content.get("input") or {}
                    if name == "Skill":
                        skill = inp.get("skill", "")
                        if isinstance(skill, str) and "handoff" in skill.lower():
                            handoff_ran = True
                        continue
                    if name not in {"Edit", "Write"}:
                        continue
                    fp = inp.get("file_path", "")
                    if isinstance(fp, str) and fp:
                        edited.append(fp.replace("\\", "/"))
    except Exception:
        return edited, handoff_ran, scheduled
    return edited, handoff_ran, scheduled


def _vault_content_files(edited: list[str]) -> list[str]:
    """Distinct edited files that are vault content — inside the vault root
    but outside the kit root (.claude/) and the app/history dirs."""
    root = str(_VAULT_ROOT).replace("\\", "/").rstrip("/").lower()
    seen: list[str] = []
    for fp in edited:
        norm = fp.replace("\\", "/")
        low = norm.lower()
        if not low.startswith(root + "/"):
            continue
        rel = norm[len(root) + 1:]
        first = rel.split("/", 1)[0].lower()
        if first in {".claude", ".obsidian", ".history"}:
            continue
        if norm not in seen:
            seen.append(norm)
    return seen


def _touched_config(edited: list[str]) -> bool:
    return any(fp.endswith("/CONFIG.md") or fp == "CONFIG.md" for fp in edited)


def _touched_critical(edited: list[str]) -> bool:
    return any(
        any(pat in fp for pat in CRITICAL_PATH_PATTERNS)
        for fp in edited
    )


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
            return "sync_config ran: CLAUDE.md and AGENTS.md session-start tables refreshed from CONFIG.md."
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

    edited, handoff_ran, scheduled = _scan_transcript(data.get("transcript_path", ""))

    notes: list[str] = []

    # Job 1 — sync config copies if CONFIG.md changed.
    if _touched_config(edited):
        notes.append(_run_sync_config())

    # Job 3 — handoff gate on vault-content edits.
    vault_files = _vault_content_files(edited)
    if vault_files and not handoff_ran and not scheduled:
        listing = ", ".join(vault_files[:10]) + (
            f" (+{len(vault_files) - 10} more)" if len(vault_files) > 10 else ""
        )
        if len(vault_files) >= HANDOFF_BLOCK_THRESHOLD:
            reason_lines = [
                f"SESSION-END AUDIT: this session edited {len(vault_files)} vault files "
                f"with no handoff: {listing}.",
                "Run the note-kit-handoff skill to log the session, then stop.",
            ]
            if notes:
                reason_lines.append(" ".join(notes))
            print(json.dumps({
                "decision": "block",
                "reason": "\n".join(reason_lines),
            }))
            return
        notes.append(
            f"This session edited vault content ({listing}) without a handoff — "
            "consider note-kit-handoff if the work warrants a session log."
        )

    # Job 2 — documentation-surface reminder.
    if not _touched_critical(edited):
        # Nothing behavior-defining changed. If we still ran sync or noted the
        # handoff, surface that; otherwise stay silent.
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
