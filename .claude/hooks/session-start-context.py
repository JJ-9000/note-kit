"""SessionStart hook: fetch session_brief from vault-search daemon and inject.

Daemon lifecycle (start with use): when the daemon is down, this hook launches
it detached via daemonctl and moves on without waiting for warm-up — the first
session of the day brings the engine up; the daemon's own idle timeout takes it
back down (vault-search/config.yaml `lifecycle.idle_shutdown_minutes`).
Also surfaces pending `needs-live-session` items (CONFIG § Queue protocol) so
an interactive session picks up work an unattended pass deferred: open
machine-queue lines, and the user-queue's standing blocked clusters.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error

DAEMON_BASE = "http://127.0.0.1:8765"
TIMEOUT_SEC = 2.0
# Fallback queue paths (the kit defaults) when CONFIG.md is unreadable.
_DEFAULT_MACHINE_QUEUE_REL = os.path.join("Outbox", "Machine-Queue.md")
_DEFAULT_USER_QUEUE_REL = os.path.join("Inbox", "User-Queue.md")


def _queue_paths(cwd: str) -> tuple[str, str]:
    """(machine-queue, user-queue) vault-relative paths from the vault's own
    `.claude/CONFIG.md` § Folders tables, so the hook follows a rename (or a
    legacy prefixed install) instead of hardcoding the kit defaults. Reads the
    `| `<token>` | `value` …` rows the same way the UI plugin does; embedded
    `<wildcard>` references expand against the parsed map. Falls back to the
    kit defaults when CONFIG is absent or a token is missing."""
    tokens: dict[str, str] = {}
    try:
        with open(os.path.join(cwd, ".claude", "CONFIG.md"), encoding="utf-8") as f:
            text = f.read()
        for m in re.finditer(r"^\|\s*`<([\w-]+)>`\s*\|\s*[^`|\n]*`([^`]+)`", text, re.MULTILINE):
            if m.group(1) not in tokens:
                tokens[m.group(1)] = m.group(2).strip().rstrip("/")
    except OSError:
        pass

    def resolve(token: str, fallback: str) -> str:
        raw = tokens.get(token)
        if not raw:
            return fallback
        resolved = re.sub(r"<([\w-]+)>", lambda m: tokens.get(m.group(1), m.group(0)), raw)
        if "<" in resolved:
            return fallback
        return os.path.join(*resolved.split("/"))

    return (
        resolve("machine-queue", _DEFAULT_MACHINE_QUEUE_REL),
        resolve("user-queue", _DEFAULT_USER_QUEUE_REL),
    )


def _start_daemon_if_down(cwd: str) -> bool:
    """Fire-and-forget daemon launch via daemonctl; True when a launch was kicked off."""
    ctl = os.path.join(cwd, ".claude", "vault-search", "daemonctl.py")
    if not os.path.isfile(ctl):
        return False
    try:
        kwargs: dict = {
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "stdin": subprocess.DEVNULL,
            "cwd": cwd,
        }
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, ctl, "start"], **kwargs)
        return True
    except Exception:
        return False


def pending_live_session_items(cwd: str) -> list[str]:
    """Open machine-queue items annotated needs-live-session — work for THIS session."""
    machine_queue_rel, _ = _queue_paths(cwd)
    path = os.path.join(cwd, machine_queue_rel)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    return [
        ln.strip()
        for ln in lines
        if "needs-live-session" in ln and "[ ]" in ln
    ][:5]


def pending_user_queue_clusters(cwd: str) -> list[str]:
    """User-queue standing clusters annotated needs-live-session.

    A blocked cluster lives as one `###` item per CONFIG § Queue protocol;
    the annotation appears in its body (agents write `needs-live-session`,
    the user sometimes writes it with spaces). Returns the item headings.
    """
    _, user_queue_rel = _queue_paths(cwd)
    path = os.path.join(cwd, user_queue_rel)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    titles: list[str] = []
    for section in re.split(r"^### ", text, flags=re.MULTILINE)[1:]:
        heading, _, _body = section.partition("\n")
        blob = section.lower()
        if "needs-live-session" in blob or "needs live session" in blob:
            titles.append(heading.strip())
    return titles[:5]


def fetch_brief(cwd: str) -> dict | None:
    if not cwd:
        return None
    url = f"{DAEMON_BASE}/api/session_brief?cwd={urllib.parse.quote(cwd)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status != 200:
                return None
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ConnectionError, OSError):
        return None
    except Exception:
        return None


def format_brief(brief: dict) -> str:
    lines: list[str] = []
    para = brief.get("para_position") or {}

    if para.get("project"):
        lines.append(f"# Vault context — active project: {para['project']}")
        active = brief.get("active_project_brief") or {}
        if active.get("open_issues_summary"):
            lines.append("")
            lines.append("## Open issues")
            lines.append(active["open_issues_summary"])
        sessions = active.get("latest_sessions") or []
        if sessions:
            lines.append("")
            lines.append("## Recent sessions")
            for s in sessions[:3]:
                title = s.get("title") or s.get("path", "")
                date = s.get("date") or "?"
                lines.append(f"- [[{s.get('path')}]] — {title} ({date})")
    elif para.get("domain"):
        lines.append(f"# Vault context — in {para['domain']} domain")
    elif para.get("type") == "fresh":
        lines.append("# Vault context — no active project detected at cwd")

    refs = brief.get("cited_references") or []
    if refs:
        lines.append("")
        lines.append("## References this project uses")
        for r in refs[:5]:
            count = r.get("citation_count", 1)
            lines.append(f"- [[{r.get('ref_path')}]] ({count}x)")

    similar = brief.get("similar_projects") or []
    if similar:
        lines.append("")
        lines.append("## Similar projects (Jaccard over shared References)")
        for p in similar[:3]:
            score = p.get("similarity_score", 0)
            name = p.get("project_name", "")
            lines.append(f"- {name} ({score:.2f})")

    workflow = brief.get("workflow_cluster")
    if workflow:
        label = workflow.get("label") or "unlabeled"
        lines.append("")
        lines.append(f"## Detected workflow cluster: {label}")
        for ref in (workflow.get("centroid_refs") or [])[:3]:
            lines.append(f"- [[{ref}]]")

    cross = brief.get("cross_project_recent_sessions") or []
    if cross:
        lines.append("")
        lines.append("## Recent cross-project sessions sharing context")
        for s in cross[:3]:
            project = s.get("project") or "?"
            score = s.get("score", 0)
            lines.append(f"- [[{s.get('path')}]] ({project}, {score:.2f})")

    gaps = brief.get("quality_gaps") or {}
    if gaps:
        lines.append("")
        lines.append("## Vault quality gaps")
        for k, items in gaps.items():
            for it in (items or [])[:2]:
                lines.append(f"- {k}: {it}")

    if not lines:
        return ""
    lines.append("")
    lines.append("Vault search: mcp__vault__vault_search / vault_recall / "
                 "vault_find_related. Daemon at " + DAEMON_BASE + ".")
    return "\n".join(lines)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    cwd = data.get("cwd") or ""
    brief = fetch_brief(cwd)

    if brief and not brief.get("error"):
        ctx = format_brief(brief)
        if not ctx:
            ctx = ("Vault retrieval available via mcp__vault__vault_search. "
                   "No active project detected.")
    else:
        # Daemon down: launch it detached (start with use) and say so — the brief
        # itself is skipped this session; search tools come up shortly. If no kit
        # daemon exists here, skills/agents fall back to Glob/Grep on their own.
        launched = _start_daemon_if_down(cwd)
        ctx = (
            "Vault-search daemon was down; launched in the background — "
            "search tools become available shortly."
        ) if launched else ""

    live = pending_live_session_items(cwd)
    clusters = pending_user_queue_clusters(cwd)
    if live or clusters:
        block_lines = ["## Waiting for a live session"]
        block_lines += [f"- machine-queue: {ln}" for ln in live]
        block_lines += [f"- user-queue: {t}" for t in clusters]
        block = "\n".join(block_lines)
        ctx = (ctx + "\n\n" + block) if ctx else block

    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": ctx,
        }
    }))


if __name__ == "__main__":
    main()
