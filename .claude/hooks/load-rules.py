"""UserPromptSubmit hook: inject the vault rules on a cadence, not every prompt.

Reads .claude/RULES.md and injects it on the 1st prompt of a session and every
N prompts after that (1, 31, 61, ... at the default of 30). Re-anchors
obligations against long-session drift without repeating the block on every
message. To change a rule, edit RULES.md; to change the cadence, edit
`rules-injection-period` in CONFIG.md § Rules injection. Prompt counting is a
per-session counter file in the OS temp dir, keyed by session_id; if the
counter cannot be read or written, the hook fails open and injects.
"""
import json
import re
import sys
import tempfile
import time
from pathlib import Path

DEFAULT_RULES_EVERY = 30

# RULES.md and CONFIG.md live at the kit root (<vault>/.claude/). This hook is
# at <vault>/.claude/hooks/, so its parent's parent is the kit root.
RULES_FILE = Path(__file__).resolve().parent.parent / "RULES.md"
CONFIG_FILE = Path(__file__).resolve().parent.parent / "CONFIG.md"
STATE_DIR = Path(tempfile.gettempdir()) / "note-kit-rules-cadence"


def rules_every() -> int:
    """Injection period from CONFIG.md § Rules injection; default on any failure."""
    try:
        text = CONFIG_FILE.read_text(encoding="utf-8", errors="replace")
        # Match only the settings-table row: | `rules-injection-period` | N |
        m = re.search(r"`rules-injection-period`\s*\|\s*(\d+)", text)
        if m:
            n = int(m.group(1))
            if n >= 1:
                return n
    except Exception:
        pass
    return DEFAULT_RULES_EVERY


def strip_frontmatter(text: str) -> str:
    if not text.startswith("---"):
        return text
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4:].lstrip("\n")


def load_rules() -> str:
    if not RULES_FILE.exists():
        return ""
    try:
        text = RULES_FILE.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    return strip_frontmatter(text).strip()


def prompt_index(session_id: str) -> int:
    """1-based index of this prompt within the session; 1 on any state failure."""
    try:
        STATE_DIR.mkdir(exist_ok=True)
        # Opportunistic cleanup of counters from long-dead sessions.
        cutoff = time.time() - 3 * 24 * 3600
        for f in STATE_DIR.glob("*.count"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass
        counter = STATE_DIR / f"{session_id}.count"
        n = 0
        if counter.exists():
            try:
                n = int(counter.read_text().strip())
            except (ValueError, OSError):
                n = 0
        n += 1
        counter.write_text(str(n))
        return n
    except Exception:
        return 1  # fail open: behave like the first prompt and inject


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    session_id = str(data.get("session_id") or "unknown")

    period = rules_every()
    if period > 1 and prompt_index(session_id) % period != 1:
        return

    ctx = load_rules()
    if not ctx:
        return
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": ctx,
        }
    }))


if __name__ == "__main__":
    main()
