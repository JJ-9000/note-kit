"""SessionStart hook: fetch session_brief from vault-search daemon and inject.

Daemon lifecycle (start with use): when the daemon is down, this hook launches
it detached via daemonctl and moves on without waiting for warm-up — the first
session of the day brings the engine up; the daemon's own idle timeout takes it
back down (vault-search/config.yaml `lifecycle.idle_shutdown_minutes`).
On a reactivation of its owning project or area — cwd inside `Projects/<X>` or
`Areas/<X>` — surfaces that owner's deferred work so the session picks up what an
unattended pass left for a live session. Scoped to the active owner and fired
only on such a reactivation, never on a random session (CONFIG § Queue protocol).
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import urllib.parse
import urllib.request
import urllib.error

_DEFAULT_DAEMON_BASE = "http://127.0.0.1:8765"
TIMEOUT_SEC = 2.0
# Fallback queue paths (the kit defaults) when CONFIG.md is unreadable.
_DEFAULT_MACHINE_QUEUE_REL = os.path.join("Inbox", "Machine-Queue.md")
_DEFAULT_USER_QUEUE_REL = os.path.join("Inbox", "User-Queue.md")


def _daemon_base(cwd: str) -> str:
    """Daemon base URL (http://host:port) from the vault's vault-search
    config.yaml, read via daemonctl's own host/port parse so a port change is
    followed instead of probing a stale hardcoded address. daemonctl is
    stdlib-only (no vault-search venv dependency); it is loaded by path under the
    system python. Falls back to 127.0.0.1:8765 when unavailable (fresh install,
    missing config)."""
    ctl = os.path.join(cwd, ".claude", "vault-search", "daemonctl.py")
    if not os.path.isfile(ctl):
        return _DEFAULT_DAEMON_BASE          # fresh install / no daemon — silent fallback
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_vs_daemonctl", ctl)
        mod = importlib.util.module_from_spec(spec)
        # Register before exec_module: a dynamically loaded module must be in
        # sys.modules so any module-level machinery that resolves its own module
        # by name (e.g. dataclasses on Py 3.14, which reads
        # sys.modules[cls.__module__].__dict__) works. daemonctl has no such
        # construct today, but the registration is the correct, safe pattern.
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        host, port = mod._read_host_port()
        return f"http://{host}:{port}"
    except Exception as exc:
        sys.modules.pop("_vs_daemonctl", None)
        # A present-but-broken daemonctl must leave a trace, not fail silent.
        # stderr only — the hook's stdout JSON contract stays clean.
        print(f"session-start-context: _daemon_base load failed "
              f"({type(exc).__name__}: {exc}); using {_DEFAULT_DAEMON_BASE}",
              file=sys.stderr)
        return _DEFAULT_DAEMON_BASE


def _queue_paths(cwd: str) -> tuple[str, str]:
    """(machine-queue, user-queue) vault-relative paths from config_variables'
    parsed § Folders token table, so the hook follows a rename (or a legacy
    prefixed install) instead of re-deriving the token-table regex here.
    config_variables is stdlib-only (no vault-search venv dependency); it is
    imported by path from <cwd>/.claude/scripts under the system python. Falls
    back to the kit defaults when CONFIG is absent or unparseable (fresh
    installs)."""
    cv = os.path.join(cwd, ".claude", "scripts", "config_variables.py")
    if not os.path.isfile(cv):
        return _DEFAULT_MACHINE_QUEUE_REL, _DEFAULT_USER_QUEUE_REL  # fresh install
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location("_vs_config_variables", cv)
        mod = importlib.util.module_from_spec(spec)
        # Register in sys.modules BEFORE exec_module. config_variables defines
        # module-level @dataclasses; on Python 3.14 the dataclass machinery reads
        # sys.modules[cls.__module__].__dict__ during class creation, so an
        # unregistered module raises AttributeError('NoneType' has no __dict__)
        # mid-exec — which the except below would swallow, silently returning the
        # kit defaults and turning this whole resolver into a no-op on any
        # renamed install. Registering the module first is the fix.
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        mq = mod.token_path("machine-queue", "Inbox/Machine-Queue.md")
        uq = mod.token_path("user-queue", "Inbox/User-Queue.md")
        return os.path.join(*mq.split("/")), os.path.join(*uq.split("/"))
    except Exception as exc:
        sys.modules.pop("_vs_config_variables", None)
        # A present-but-unparseable CONFIG must leave a trace, not fail silent.
        # stderr only — the hook's stdout JSON contract stays clean.
        print(f"session-start-context: _queue_paths load failed "
              f"({type(exc).__name__}: {exc}); using kit defaults",
              file=sys.stderr)
        return _DEFAULT_MACHINE_QUEUE_REL, _DEFAULT_USER_QUEUE_REL


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


def _active_owner(cwd: str) -> str | None:
    """Project/area name when cwd sits inside `Projects/<X>` or `Areas/<X>` (legacy
    numeric prefix tolerated), else None — the reactivation signal: deferred work
    re-presents only when the session resolves to its owning project or area,
    never on a random session. Daemon-independent so the gate holds when the
    brief is unavailable."""
    parts = cwd.replace("\\", "/").rstrip("/").split("/")
    for i in range(len(parts) - 1):
        root = re.sub(r"^\d+-", "", parts[i])
        if root in ("Projects", "Areas"):
            return parts[i + 1] or None
    return None


def open_holds(cwd: str, owner: str) -> list[str]:
    """Open resume records for `owner`, read from the generated holds surface.

    The surface is `<logs>/Holds.md`, regenerated each action-agent pass by
    `build_holds_surface.py` from every plan's `- [ ] HOLD (date) — prompt`
    lines. Reading the generated table rather than re-scanning the plans keeps
    one producer for the fact: the surface is the index, the plan line is the
    record, and this hook is only a reader.

    Matching is on the plan link, so a hold surfaces on reactivation of the
    project or area whose plan carries it. Returns `date — prompt` strings.
    """
    logs_rel = "Archive/Logs"
    cv = os.path.join(cwd, ".claude", "scripts", "config_variables.py")
    if os.path.isfile(cv):
        try:
            import importlib.util
            spec = importlib.util.spec_from_file_location("_vs_cv_holds", cv)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = mod
            spec.loader.exec_module(mod)
            logs_rel = mod.token_path("logs", logs_rel)
        except Exception:
            sys.modules.pop("_vs_cv_holds", None)
    path = os.path.join(cwd, *logs_rel.split("/"), "Holds.md")
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    owner_l = owner.lower()
    out: list[str] = []
    for ln in lines:
        if not ln.startswith("| 2"):          # data rows only; skips header/separator
            continue
        cells = [c.strip() for c in ln.strip().strip("|").split("|")]
        if len(cells) < 3:
            continue
        date, prompt, plan = cells[0], cells[1], cells[2]
        if owner_l in plan.lower() or owner_l in prompt.lower():
            out.append(f"{date} — {prompt}")
    return out[:5]


# DORMANT (2026-07-25): the needs-live-session marker is retired (CONFIG § Retired tokens);
# no producer writes it, and `open_holds` above is the live replacement. Kept only so a
# legacy machine-queue line written before the retirement still surfaces once; delete when
# no vault in service carries the marker.
def pending_live_session_items(cwd: str, owner: str) -> list[str]:
    """Open machine-queue items annotated needs-live-session that name `owner`."""
    machine_queue_rel, _ = _queue_paths(cwd)
    path = os.path.join(cwd, machine_queue_rel)
    try:
        with open(path, "r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    owner_l = owner.lower()
    return [
        ln.strip()
        for ln in lines
        if "needs-live-session" in ln and "[ ]" in ln and owner_l in ln.lower()
    ][:5]


def pending_user_queue_clusters(cwd: str, owner: str) -> list[str]:
    """User-queue clusters that name `owner` and await a live session.

    A blocked cluster lives as one `###` item per CONFIG § Queue protocol; it is
    surfaced only when its body references the active owner, so deferred work
    returns on a reactivation of its own project/area and nowhere else.
    """
    _, user_queue_rel = _queue_paths(cwd)
    path = os.path.join(cwd, user_queue_rel)
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return []
    owner_l = owner.lower()
    titles: list[str] = []
    for section in re.split(r"^### ", text, flags=re.MULTILINE)[1:]:
        heading, _, _body = section.partition("\n")
        blob = section.lower()
        if owner_l not in blob:
            continue
        if "needs-live-session" in blob or "needs live session" in blob:
            titles.append(heading.strip())
    return titles[:5]


def _listener_present(base: str, timeout: float = TIMEOUT_SEC) -> bool:
    """True when a TCP handshake to the daemon's host:port completes — a process
    is holding the port.

    This is the port-bind-conflict discriminator, chosen over sniffing urllib
    exception types because a genuinely-down port does not present uniformly: on
    some hosts an unbound local port is actively refused (RST), on others the
    firewall silently drops the SYN and the connect TIMES OUT. Both mean "nothing
    is listening" and both must read as down, so the hook may spawn. Only a
    completed handshake means a daemon already holds the port — spawning a second
    one then is the port-bind failure this guards against.
    """
    parts = urllib.parse.urlsplit(base)
    host, port = parts.hostname or "127.0.0.1", parts.port or 8765
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def fetch_brief(cwd: str, base: str) -> tuple[str, dict | None]:
    """Probe the daemon's session_brief. Returns (state, brief):

      'ok'        — a listener answered HTTP 200 with a JSON body (the brief).
      'down'      — no listener holds the port (refused or unreachable); the hook
                    MAY spawn one.
      'unhealthy' — a listener holds the port but answered badly (error status,
                    garbage, non-JSON, hang, reset). The hook must NOT spawn a
                    second instance into a port-bind failure — it surfaces the
                    state instead.
    """
    if not cwd:
        return "down", None
    if not _listener_present(base):
        return "down", None
    url = f"{base}/api/session_brief?cwd={urllib.parse.quote(cwd)}"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            if resp.status != 200:
                return "unhealthy", None
            raw = resp.read().decode("utf-8")
    except Exception:
        # A listener is present (the handshake completed) but the HTTP exchange
        # failed — error status, reset, garbage, hang, decode error. A live
        # daemon holds the port: unhealthy, never a second spawn.
        return "unhealthy", None
    try:
        return "ok", json.loads(raw)
    except (ValueError, json.JSONDecodeError):
        return "unhealthy", None


def format_brief(brief: dict, base: str) -> str:
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
                 "vault_find_related. Daemon at " + base + ".")
    return "\n".join(lines)


def main() -> None:
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except Exception:
        data = {}
    cwd = data.get("cwd") or ""
    base = _daemon_base(cwd)
    state, brief = fetch_brief(cwd, base)

    if state == "ok" and brief and not brief.get("error"):
        ctx = format_brief(brief, base)
        if not ctx:
            ctx = ("Vault retrieval available via mcp__vault__vault_search. "
                   "No active project detected.")
    elif state == "down":
        # Genuinely down (connection refused): launch it detached (start with
        # use) and say so — the brief is skipped this session; search tools come
        # up shortly. If no kit daemon exists here, skills/agents fall back to
        # Glob/Grep on their own.
        launched = _start_daemon_if_down(cwd)
        ctx = (
            "Vault-search daemon was down; launched in the background — "
            "search tools become available shortly."
        ) if launched else ""
    else:
        # Unhealthy — a live daemon answered badly (or returned an error field).
        # Do NOT spawn a second instance into a port-bind failure; surface the
        # state and let the session proceed without the brief.
        ctx = (
            "Vault-search daemon is running but returned an unhealthy response; "
            "the session brief is skipped this session."
        )

    # Re-present deferred work ONLY on a reactivation of its owning project/area —
    # never on a random session (CONFIG § Queue protocol). A fresh, reference, or
    # vault-root session resolves to no owner and surfaces nothing.
    owner = _active_owner(cwd)
    if owner:
        holds = open_holds(cwd, owner)
        live = pending_live_session_items(cwd, owner)
        clusters = pending_user_queue_clusters(cwd, owner)
        if holds or live or clusters:
            block_lines = [f"## {owner} — waiting on a live session"]
            # Resume records first: each is already written as the opening move
            # of the session picking it up, so it reads as the next action.
            block_lines += [f"- hold: {h}" for h in holds]
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
