"""UserPromptSubmit hook: load the vault rules and inject them as context.

Reads .claude/RULES.md and injects it on every prompt. To change a rule, edit
RULES.md — no settings.json change needed.
"""
import json
import sys
from pathlib import Path

# RULES.md lives at the kit root (<vault>/.claude/RULES.md). This hook is at
# <vault>/.claude/hooks/, so its parent's parent is the kit root.
RULES_FILE = Path(__file__).resolve().parent.parent / "RULES.md"


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


def main() -> None:
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
