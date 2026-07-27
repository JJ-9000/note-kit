"""Vault-search daemon entry point.

Runs as a FastMCP server on Streamable HTTP. Boots the indexer asynchronously
so the HTTP listener is up immediately; `/health` returns 503 until the
initial walk completes, then 200.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

from indexer import Embedder, Indexer, IndexerThread, start_watcher
from store import Store
from topology import TopologyCache
from tools import (
    detect_workflow,
    list_projects,
    list_reference_domains,
    session_brief,
    topology_status,
    vault_find_related,
    vault_find_similar_projects,
    vault_get_bridge_references,
    vault_get_projects_using,
    vault_get_references_for_path,
    vault_index_status,
    vault_recall,
    vault_search,
)
from workflow import build_workflow_clusters

DAEMON_NAME = "vault-search"

# Periodic-thread cadence defaults (overridden by config when present).
DEFAULT_RECLUSTER_HOURS = 24
DEFAULT_FULL_RESCAN_HOURS = 6

# Idle-shutdown bookkeeping: the daemon starts with use (the session-start hook
# launches it if down) and leaves with idleness, approximating launch-and-quit
# with the app without a Windows service. Any HTTP request except /health marks
# activity; /health is excluded so liveness polls never keep an unused daemon up.
LAST_ACTIVITY = {"t": time.time()}


class _ActivityMiddleware:
    """Pure-ASGI middleware marking request activity for the idle watchdog."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") != "/health":
            LAST_ACTIVITY["t"] = time.time()
        await self.app(scope, receive, send)


def _idle_watchdog_thread() -> None:
    """Exit cleanly after `lifecycle.idle_shutdown_minutes` without a request.

    0 (or absent) disables. Never exits mid-warmup or with indexing pending, so
    a long first build is not cut short.
    """
    cfg = (STATE.config or {}).get("lifecycle", {})
    idle_min = float(cfg.get("idle_shutdown_minutes", 0) or 0)
    if idle_min <= 0:
        return
    idle_sec = idle_min * 60.0
    while not STATE._stop.wait(60.0):
        if not STATE.warmed_up:
            continue
        if STATE.indexer_thread and STATE.indexer_thread.pending_count():
            continue
        if time.time() - LAST_ACTIVITY["t"] > idle_sec:
            log.info("idle_shutdown", extra={"ctx_idle_minutes": idle_min})
            _graceful_shutdown(reason="idle_shutdown", exit_code=0)


# ---- config + logging -------------------------------------------------------


def load_config(path: Path) -> dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for k, v in record.__dict__.items():
            if k.startswith("ctx_"):
                payload[k[4:]] = v
        return json.dumps(payload, ensure_ascii=False)


def setup_logging(log_path: str, level: str) -> None:
    Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=10 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(_JsonFormatter())
    root.addHandler(file_handler)

    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setFormatter(_JsonFormatter())
    root.addHandler(stdout_handler)


# ---- daemon state -----------------------------------------------------------


class DaemonState:
    def __init__(self) -> None:
        self.start_time: float = time.time()
        self.config: dict[str, Any] = {}
        self.store: Store | None = None
        self.embedder: Embedder | None = None
        self.indexer: Indexer | None = None
        self.indexer_thread: IndexerThread | None = None
        self.observer: Any = None
        self.topology: TopologyCache | None = None
        self.warmed_up: bool = False
        self.warmup_error: str | None = None
        self.warmup_started_at: float | None = None
        self._stop = threading.Event()
        self._shutting_down: bool = False
        self.last_recluster_at: float | None = None
        self.last_full_rescan_at: float | None = None


STATE = DaemonState()
log = logging.getLogger("vault.server")


def _graceful_shutdown(reason: str, exit_code: int = 0) -> None:
    """Close the store and stop background threads, then exit the process.

    The single teardown path shared by the SIGTERM/SIGINT handlers, the
    /shutdown endpoint, and the idle watchdog — so the SQLite store is always
    checkpointed and closed before exit rather than left to an abrupt kill.
    Guarded so overlapping triggers (a signal racing the idle timer) run it once.
    """
    if STATE._shutting_down:
        return
    STATE._shutting_down = True
    STATE._stop.set()
    try:
        if STATE.indexer_thread is not None:
            STATE.indexer_thread.stop()
    except Exception:
        pass
    try:
        if STATE.observer is not None:
            STATE.observer.stop()
    except Exception:
        pass
    try:
        if STATE.store is not None:
            STATE.store.close()
    except Exception:
        pass
    log.info("shutdown", extra={"ctx_reason": reason})
    logging.shutdown()
    os._exit(exit_code)


# ---- warmup -----------------------------------------------------------------


def _run_clustering() -> int:
    """Build + persist workflow clusters; refresh topology. Returns cluster count."""
    if STATE.store is None or STATE.topology is None:
        return 0
    wf_cfg = STATE.config.get("workflow_clustering", {}) if STATE.config else {}
    if not wf_cfg.get("enabled", True):
        return 0
    clusters = build_workflow_clusters(
        STATE.store,
        jaccard_threshold=wf_cfg.get("jaccard_threshold", 0.15),
        min_cluster_size=wf_cfg.get("min_cluster_size", 2),
    )
    STATE.store.replace_workflow_clusters(clusters)
    STATE.topology.rebuild()
    STATE.last_recluster_at = time.time()
    return len(clusters)


def _periodic_recluster_thread() -> None:
    interval_hours = (
        STATE.config.get("workflow_clustering", {}).get(
            "recluster_interval_hours", DEFAULT_RECLUSTER_HOURS
        )
    )
    interval_sec = max(int(interval_hours * 3600), 60)
    log.info(
        "periodic_recluster_started",
        extra={"ctx_interval_hours": interval_hours},
    )
    while not STATE._stop.wait(interval_sec):
        try:
            n = _run_clustering()
            log.info("periodic_recluster_done", extra={"ctx_clusters": n})
        except Exception:
            log.exception("periodic_recluster_failed")


def _periodic_full_rescan_thread() -> None:
    interval_hours = (
        STATE.config.get("watch", {}).get(
            "full_rescan_interval_hours", DEFAULT_FULL_RESCAN_HOURS
        )
    )
    interval_sec = max(int(interval_hours * 3600), 60)
    log.info(
        "periodic_full_rescan_started",
        extra={"ctx_interval_hours": interval_hours},
    )
    while not STATE._stop.wait(interval_sec):
        try:
            if STATE.indexer is None:
                continue
            indexed, removed = STATE.indexer.full_walk()
            STATE.last_full_rescan_at = time.time()
            log.info(
                "periodic_full_rescan_done",
                extra={"ctx_indexed": indexed, "ctx_removed": removed},
            )
            if indexed > 0 and STATE.topology is not None:
                STATE.topology.invalidate()
        except Exception:
            log.exception("periodic_full_rescan_failed")


def _warmup_thread() -> None:
    try:
        STATE.warmup_started_at = time.time()
        log.info("warmup_start")
        _ = STATE.embedder._ensure()  # type: ignore[union-attr]
        log.info("model_loaded")
        indexed, removed = STATE.indexer.full_walk(  # type: ignore[union-attr]
            progress_cb=lambda done, total: log.info(
                "index_progress",
                extra={"ctx_done": done, "ctx_total": total},
            )
        )
        log.info(
            "initial_index_complete",
            extra={"ctx_indexed": indexed, "ctx_removed": removed},
        )
        STATE.topology.rebuild()  # type: ignore[union-attr]
        log.info("topology_built")
        try:
            n = _run_clustering()
            log.info(f"workflow_clusters_persisted: count={n}")
        except Exception:
            log.exception("workflow clustering failed")
        STATE.observer, STATE.indexer_thread, _ = start_watcher(
            STATE.indexer,  # type: ignore[arg-type]
            STATE.config["watch"]["debounce_seconds"],
            on_batch=lambda: STATE.topology.invalidate() if STATE.topology else None,  # type: ignore[union-attr]
        )
        threading.Thread(
            target=_periodic_recluster_thread, daemon=True, name="recluster"
        ).start()
        threading.Thread(
            target=_periodic_full_rescan_thread, daemon=True, name="full-rescan"
        ).start()
        STATE.warmed_up = True
        log.info("warmup_complete")
    except Exception as e:
        log.exception(f"warmup_failed: {e}")
        STATE.warmup_error = str(e)


def init_daemon(config_path: Path) -> None:
    STATE.config = load_config(config_path)
    setup_logging(
        STATE.config["logging"]["log_path"],
        STATE.config["logging"]["level"],
    )
    log.info(
        "daemon_init",
        extra={"ctx_config_path": str(config_path)},
    )
    STATE.store = Store(
        STATE.config["index"]["path"],
        rebuild_on_schema_change=STATE.config["index"].get("rebuild_on_schema_change", True),
    )
    STATE.embedder = Embedder(
        model_name=STATE.config["embedding"]["model"],
        device=STATE.config["embedding"]["device"],
        cache_dir=STATE.config["embedding"]["cache_dir"],
        batch_size=STATE.config["embedding"].get("batch_size", 32),
    )
    STATE.indexer = Indexer(
        store=STATE.store,
        embedder=STATE.embedder,
        vault_path=Path(STATE.config["vault_path"]),
        chunk_size=STATE.config["index"]["chunk_size_tokens"],
        overlap=STATE.config["index"]["chunk_overlap_tokens"],
        exclude_paths=STATE.config["watch"]["exclude_paths"],
    )
    STATE.topology = TopologyCache(STATE.store)
    threading.Thread(target=_warmup_thread, daemon=True, name="warmup").start()


# ---- FastMCP server setup ---------------------------------------------------


mcp = FastMCP(DAEMON_NAME, version="0.1.0")


@mcp.tool(name="vault_search")
def vault_search_tool(
    query: str,
    top_k: int = 8,
    para_filter: list[str] | None = None,
    para_position: str | None = None,
    boost_linked_to: str | None = None,
) -> list[dict[str, Any]]:
    """Hybrid BM25+vector search over the Obsidian vault.

    Returns top-K most relevant chunks with paths, snippets, scores, and PARA
    metadata. Call this BEFORE writing code or detailed prose to find prior
    solutions, reference notes, or related session logs.

    Args:
        query: Natural language description of what you're looking for.
        top_k: Number of results to return (default 8).
        para_filter: Optional list of PARA types to include
            (project, area, reference, snippet, session, index).
        para_position: Reserved for Phase 1 — currently a no-op.
        boost_linked_to: Reserved for Phase 1 — currently a no-op.
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_search(
        STATE.store,         # type: ignore[arg-type]
        STATE.embedder,      # type: ignore[arg-type]
        query=query,
        top_k=top_k,
        para_filter=para_filter,
        para_position=para_position,
        boost_linked_to=boost_linked_to,
        topology=STATE.topology,
    )


# Re-expose under the canonical spec name as well so MCP clients see the same
# tool name listed in §5.13.


@mcp.tool(name="vault_index_status")
def vault_index_status_tool() -> dict[str, Any]:
    """Index health: file/chunk counts, last update, pending changes, uptime."""
    return vault_index_status(
        STATE.store,           # type: ignore[arg-type]
        STATE.embedder,        # type: ignore[arg-type]
        STATE.indexer_thread,
        STATE.start_time,
    )



@mcp.tool(name="vault_get_references_for_path")
def vault_get_references_for_path_tool(path: str) -> list[dict[str, Any]]:
    """Walk wikilink graph forward from `path`. Return Reference/Snippet/Index
    notes it cites, with citation counts and citing chunk IDs.

    Args:
        path: Vault-relative path (e.g. "Projects/Example-Project/Example-Project.md").
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_get_references_for_path(STATE.store, path)  # type: ignore[arg-type]



@mcp.tool(name="vault_get_projects_using")
def vault_get_projects_using_tool(ref_path: str) -> list[dict[str, Any]]:
    """Walk wikilink graph backward from a Reference. Return projects that cite
    it (rolled up from sessions + project files), with last-cited date.

    Args:
        ref_path: Vault-relative path of a Reference / Snippet / Index note.
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_get_projects_using(STATE.store, ref_path)  # type: ignore[arg-type]



@mcp.tool(name="list_projects")
def list_projects_tool() -> list[dict[str, Any]]:
    """All projects in the vault, with cited references, sessions, and modification time."""
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return list_projects(STATE.topology)  # type: ignore[arg-type]



@mcp.tool(name="list_reference_domains")
def list_reference_domains_tool() -> list[dict[str, Any]]:
    """All Reference and Snippet domains, with Index pointer, child files, and citing projects."""
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return list_reference_domains(STATE.topology)  # type: ignore[arg-type]



@mcp.tool(name="topology_status")
def topology_status_tool() -> dict[str, Any]:
    """Vault topology summary + quality gaps (missing Project.md, missing Indexes, orphans)."""
    if not STATE.warmed_up:
        return {"error": "indexer warming up — try again in a moment"}
    return topology_status(STATE.topology)  # type: ignore[arg-type]



@mcp.tool(name="vault_find_similar_projects")
def vault_find_similar_projects_tool(
    project: str, top_k: int = 5
) -> list[dict[str, Any]]:
    """Projects most similar to the given one by Jaccard over shared References.

    Args:
        project: Project name (folder name under the projects root), e.g. "Example-Project".
        top_k: Number of results to return.
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_find_similar_projects(STATE.topology, project, top_k=top_k)  # type: ignore[arg-type]



@mcp.tool(name="vault_get_bridge_references")
def vault_get_bridge_references_tool(
    project_a: str, project_b: str
) -> list[dict[str, Any]]:
    """References cited by both projects — the shared knowledge surface.

    Args:
        project_a: First project name.
        project_b: Second project name.
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_get_bridge_references(  # type: ignore[arg-type]
        STATE.store, STATE.topology, project_a, project_b
    )



@mcp.tool(name="vault_recall")
def vault_recall_tool(
    topic: str,
    since_date: str | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Search session logs by topic, ranked by similarity + recency (60-day decay).

    Args:
        topic: Natural-language description of the work to recall.
        since_date: Optional ISO date floor (YYYY-MM-DD).
        top_k: Number of session logs to return.
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_recall(  # type: ignore[arg-type]
        STATE.store, STATE.embedder, topic=topic, since_date=since_date, top_k=top_k,
    )



@mcp.tool(name="detect_workflow")
def detect_workflow_tool(
    context: str, threshold: float = 0.5
) -> dict[str, Any] | None:
    """Match a context string against persisted workflow clusters. Returns the
    best match above threshold, or null. Use to discover what workflow shape
    a piece of work belongs to (e.g. "auth refactor" cluster).

    Args:
        context: Free-text or vault-path context (cwd, task description, etc.).
        threshold: Cosine similarity floor (default 0.5).
    """
    if not STATE.warmed_up:
        return {"error": "indexer warming up — try again in a moment"}
    return detect_workflow(  # type: ignore[arg-type]
        STATE.store, STATE.embedder, context=context, threshold=threshold,
    )



@mcp.tool(name="session_brief")
def session_brief_tool(
    cwd: str, recent_files: list[str] | None = None
) -> dict[str, Any]:
    """Session-start brief: PARA position, active project state, similar
    projects, workflow cluster, recent cross-project sessions, quality gaps.

    Composes lower-level tools into a structured response (~1000-1500 tokens).
    Output is pointers + summaries — caller follows wikilinks for detail.

    Args:
        cwd: Vault-relative path indicating where the user is working.
        recent_files: Optional list of recently-touched files for context.
    """
    if not STATE.warmed_up:
        return {"error": "indexer warming up — try again in a moment"}
    return session_brief(  # type: ignore[arg-type]
        STATE.store, STATE.embedder, STATE.topology,
        cwd=cwd, recent_files=recent_files,
    )



@mcp.tool(name="vault_find_related")
def vault_find_related_tool(
    path: str, top_k: int = 5
) -> list[dict[str, Any]]:
    """Given a vault file, return semantically and structurally related files.

    Combines embedding similarity over the file's first chunks with the wikilink
    graph (files that link to or from it). Each result has a relationship_hint
    of "linked", "semantic", or "linked-only".

    Args:
        path: Vault-relative path of the seed file.
        top_k: Number of related files to return.
    """
    if not STATE.warmed_up:
        return [{"error": "indexer warming up — try again in a moment"}]
    return vault_find_related(  # type: ignore[arg-type]
        STATE.store, STATE.embedder, path=path, top_k=top_k,
    )



@mcp.custom_route("/api/session_brief", methods=["GET"])
async def api_session_brief(request: Request) -> JSONResponse:
    """REST shim for hooks: GET /api/session_brief?cwd=<vault-relative-path>"""
    if not STATE.warmed_up:
        return JSONResponse(
            {"error": "indexer warming up"}, status_code=503,
        )
    cwd = request.query_params.get("cwd", "")
    if not cwd:
        return JSONResponse({"error": "cwd query param required"}, status_code=400)
    try:
        result = session_brief(  # type: ignore[arg-type]
            STATE.store, STATE.embedder, STATE.topology, cwd=cwd,
        )
        return JSONResponse(result)
    except Exception as e:
        log.exception("api_session_brief failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/topology_status", methods=["GET"])
async def api_topology_status(request: Request) -> JSONResponse:
    if not STATE.warmed_up:
        return JSONResponse({"error": "indexer warming up"}, status_code=503)
    try:
        return JSONResponse(topology_status(STATE.topology))  # type: ignore[arg-type]
    except Exception as e:
        log.exception("api_topology_status failed")
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/shutdown", methods=["POST"])
async def shutdown(request: Request) -> JSONResponse:
    """Graceful-stop trigger for the control helper.

    The daemon binds 127.0.0.1 only, so this is a localhost-local control. It
    runs the shared teardown (store close) in a background thread so this
    response flushes before the process exits — the cross-platform graceful path
    daemonctl.py uses before falling back to a force kill (Windows has no
    deliverable SIGTERM for a detached, windowless console process).
    """
    def _do() -> None:
        time.sleep(0.3)
        _graceful_shutdown(reason="shutdown_endpoint", exit_code=0)

    threading.Thread(target=_do, daemon=True, name="shutdown").start()
    return JSONResponse({"status": "shutting down", "pid": os.getpid()}, status_code=200)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    payload = {
        "status": "ok" if STATE.warmed_up else ("error" if STATE.warmup_error else "warming"),
        "pid": os.getpid(),
        "uptime_sec": int(time.time() - STATE.start_time),
        "warming_started_sec_ago": (
            int(time.time() - STATE.warmup_started_at)
            if STATE.warmup_started_at else None
        ),
        "warmup_error": STATE.warmup_error,
        "pending_changes": (
            STATE.indexer_thread.pending_count() if STATE.indexer_thread else 0
        ),
        "model_loaded": STATE.embedder is not None and STATE.embedder._model is not None,  # type: ignore[union-attr]
        "db_size_mb": round(
            (STATE.store.db_size_bytes() / 1_000_000.0) if STATE.store else 0.0, 2
        ),
    }
    status_code = 200 if STATE.warmed_up else 503
    return JSONResponse(payload, status_code=status_code)


# ---- main -------------------------------------------------------------------


def main() -> None:
    here = Path(__file__).resolve().parent
    config_path = here / "config.yaml"
    init_daemon(config_path)
    threading.Thread(target=_idle_watchdog_thread, daemon=True, name="idle-watchdog").start()

    # Close the store on SIGTERM/SIGINT. uvicorn installs its own signal
    # handlers when it runs (and may override these), so the try/finally below is
    # the reliable teardown: when uvicorn returns after catching a signal, the
    # store still closes. On POSIX these handlers also cover the case uvicorn
    # does not; on Windows a detached daemon receives no deliverable signal, so
    # the /shutdown endpoint is the graceful path there.
    def _signal_handler(signum, _frame):  # pragma: no cover - signal path
        _graceful_shutdown(reason=f"signal_{signum}", exit_code=0)

    for _sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(_sig, _signal_handler)
        except (ValueError, OSError):
            pass

    log.info(
        "starting_mcp_server",
        extra={
            "ctx_host": STATE.config["host"],
            "ctx_port": STATE.config["port"],
            "ctx_transport": STATE.config["mcp"]["transport"],
        },
    )
    import uvicorn

    app = _ActivityMiddleware(mcp.http_app())
    try:
        uvicorn.run(app, host=STATE.config["host"], port=STATE.config["port"], log_config=None)
    finally:
        _graceful_shutdown(reason="server_exit", exit_code=0)


if __name__ == "__main__":
    main()
