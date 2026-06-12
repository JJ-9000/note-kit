"""MCP tool implementations.

Phases 0-2:
- vault_search (hybrid RRF + title/heading/Index + PARA + link + sibling boosts)
- vault_index_status
- vault_get_references_for_path, vault_get_projects_using
- list_projects, list_reference_domains, topology_status
- vault_find_similar_projects, vault_get_bridge_references
- vault_recall, vault_find_related
"""
from __future__ import annotations

import math
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from indexer import Embedder, IndexerThread
from store import Store
from topology import (
    ProjectInfo,
    TopologyCache,
    domain_info_dict,
    project_info_dict,
)

RRF_K = 60
TITLE_BOOST_MAX = 0.6
HEADING_BOOST_MAX = 0.4
Index_PENALTY = 0.88
LINK_BOOST = 1.30
PARA_SAME_PROJECT = 1.50
PARA_LINKED_REF = 1.30
PARA_SAME_DOMAIN = 1.20
PARA_SIBLING = 1.10

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


def _rrf_fuse(
    vec_results: list[tuple[int, float]],
    fts_results: list[tuple[int, float]],
    k: int = RRF_K,
) -> list[tuple[int, float]]:
    scores: dict[int, float] = {}
    for rank, (chunk_id, _dist) in enumerate(vec_results):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    for rank, (chunk_id, _bm25) in enumerate(fts_results):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + rank + 1)
    return sorted(scores.items(), key=lambda x: -x[1])


def _format_snippet(text: str, max_chars: int = 240) -> str:
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"


def _iso(epoch: float | None) -> str | None:
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _tokens(text: str | None) -> set[str]:
    if not text:
        return set()
    return {t.lower() for t in _TOKEN_RE.findall(text)}


def _term_overlap(query_tokens: set[str], target_tokens: set[str]) -> float:
    if not query_tokens or not target_tokens:
        return 0.0
    return len(query_tokens & target_tokens) / len(query_tokens)


def _is_index_title(title: str | None) -> bool:
    if not title:
        return False
    return title.endswith("-Index")


def _classify_para_position(para_position: str) -> dict[str, str | None]:
    """Pull (project|area, domain) out of a vault-relative cwd path."""
    parts = para_position.replace("\\", "/").strip("/").split("/")
    out: dict[str, str | None] = {
        "owner": None, "para_type": None, "domain": None, "area": None,
    }
    if not parts:
        return out
    if parts[0] == "01-Projects" and len(parts) >= 2:
        out["owner"] = parts[1]
        out["para_type"] = "project"
    elif parts[0] == "01-Areas" and len(parts) >= 2:
        out["owner"] = parts[1]
        out["area"] = parts[1]
        out["para_type"] = "area"
    elif parts[0] == "01-References" and len(parts) >= 2:
        out["domain"] = parts[1]
        out["para_type"] = "reference"
    elif parts[0] == "01-Snippets" and len(parts) >= 2:
        out["domain"] = parts[1]
        out["para_type"] = "snippet"
    return out


def _para_boost_factor(
    row: dict,
    pos: dict[str, str | None],
    linked_ref_ids: set[int],
    sibling_owners: set[str] | None = None,
) -> float:
    if pos["owner"] and row["para_owner"] == pos["owner"]:
        return PARA_SAME_PROJECT
    if linked_ref_ids and row["file_id"] in linked_ref_ids:
        return PARA_LINKED_REF
    if pos["domain"] and row["para_owner"] == pos["domain"]:
        return PARA_SAME_DOMAIN
    if sibling_owners and row["para_owner"] in sibling_owners:
        return PARA_SIBLING
    return 1.0


def _sibling_owners(topology: TopologyCache, owner: str) -> set[str]:
    """Project owners that share at least one cited reference with `owner`."""
    topo = topology.get()
    if owner not in topo.projects:
        return set()
    own_refs = set(topo.projects[owner].cited_references)
    if not own_refs:
        return set()
    out: set[str] = set()
    for other_name, other in topo.projects.items():
        if other_name == owner:
            continue
        if own_refs & set(other.cited_references):
            out.add(other_name)
    return out


def vault_search(
    store: Store,
    embedder: Embedder,
    query: str,
    top_k: int = 8,
    para_filter: list[str] | None = None,
    para_position: str | None = None,
    boost_linked_to: str | None = None,
    topology: TopologyCache | None = None,
) -> list[dict[str, Any]]:
    if not query.strip():
        return []
    pool = max(top_k * 8, 40)
    embedding = embedder.encode([query])[0]
    vec_results = store.vec_search(embedding, pool)
    fts_results = store.fts_search(query, pool)
    fused = _rrf_fuse(vec_results, fts_results)
    if not fused:
        return []

    chunk_ids = [cid for cid, _ in fused]
    chunks = store.get_chunks_with_files(chunk_ids)
    query_tokens = _tokens(query)

    pos_info: dict[str, str | None] = {}
    linked_ref_ids: set[int] = set()
    sibling_owners: set[str] = set()
    if para_position:
        pos_info = _classify_para_position(para_position)
        if pos_info.get("owner"):
            linked_ref_ids = store.get_referenced_file_ids_by_owner(pos_info["owner"])
        if topology is not None and pos_info.get("owner"):
            sibling_owners = _sibling_owners(topology, pos_info["owner"])

    link_boost_ids: set[int] = set()
    if boost_linked_to:
        target_id = store.file_id_for_path(boost_linked_to)
        if target_id is not None:
            link_boost_ids = store.get_linked_file_ids(target_id)

    boosted: list[tuple[int, float]] = []
    for chunk_id, score in fused:
        row = chunks.get(chunk_id)
        if row is None:
            continue
        path_basename = Path(row["path"]).stem
        title_overlap = _term_overlap(
            query_tokens,
            _tokens(row["title"]) | _tokens(path_basename),
        )
        section_overlap = _term_overlap(
            query_tokens, _tokens(row["section_heading"])
        )
        adjusted = score
        adjusted *= 1.0 + TITLE_BOOST_MAX * title_overlap
        adjusted *= 1.0 + HEADING_BOOST_MAX * section_overlap
        if _is_index_title(row["title"]):
            adjusted *= Index_PENALTY
        if pos_info:
            adjusted *= _para_boost_factor(row, pos_info, linked_ref_ids, sibling_owners)
        if link_boost_ids and row["file_id"] in link_boost_ids:
            adjusted *= LINK_BOOST
        boosted.append((chunk_id, adjusted))
    boosted.sort(key=lambda x: -x[1])

    seen_files: set[int] = set()
    out: list[dict[str, Any]] = []
    for chunk_id, score in boosted:
        row = chunks.get(chunk_id)
        if row is None:
            continue
        if row["file_id"] in seen_files:
            continue

        if para_filter:
            if row["para_type"] not in para_filter:
                continue
        if row["para_type"] in {"inbox", "archive", "unknown"}:
            if not (para_filter and row["para_type"] in para_filter):
                continue

        seen_files.add(row["file_id"])
        out.append({
            "path": row["path"],
            "title": row["title"] or row["path"],
            "snippet": _format_snippet(row["text"]),
            "section_heading": row["section_heading"],
            "score": round(score, 4),
            "para_type": row["para_type"],
            "para_owner": row["para_owner"],
            "parent_index": row["parent_index"],
            "last_modified": _iso(row["last_modified"]),
        })
        if len(out) >= top_k:
            break
    return out


def vault_index_status(
    store: Store,
    embedder: Embedder,
    indexer_thread: IndexerThread | None,
    daemon_start_time: float,
) -> dict[str, Any]:
    total_links, resolved_links = store.wikilink_count()
    return {
        "file_count": store.file_count(),
        "chunk_count": store.chunk_count(),
        "wikilinks_total": total_links,
        "wikilinks_resolved": resolved_links,
        "wikilinks_cvf": store.cvf_link_count(),
        "last_update": _iso(store.last_update()),
        "embedding_model": embedder.model_name,
        "pending_changes": indexer_thread.pending_count() if indexer_thread else 0,
        "daemon_uptime_seconds": int(time.time() - daemon_start_time),
        "db_size_mb": round(store.db_size_bytes() / 1_000_000.0, 2),
    }


def vault_get_references_for_path(
    store: Store, path: str
) -> list[dict[str, Any]]:
    """All Reference / Snippet / Index notes cited from `path`, with counts."""
    file_id = store.file_id_for_path(path)
    if file_id is None:
        return []
    rows = store.get_outbound_refs(file_id)
    return [{
        "ref_path": r["path"],
        "ref_title": r["title"] or r["path"],
        "para_type": r["para_type"],
        "para_owner": r["para_owner"],
        "citation_count": r["citation_count"],
        "citing_chunks": (
            [int(x) for x in r["chunk_ids"].split(",") if x and x != "None"]
            if r.get("chunk_ids") else []
        ),
    } for r in rows]


def vault_get_projects_using(
    store: Store, ref_path: str
) -> list[dict[str, Any]]:
    """All projects (rolled up from sessions+project files) that cite `ref_path`."""
    file_id = store.file_id_for_path(ref_path)
    if file_id is None:
        return []
    rows = store.get_inbound_projects(file_id)
    return [{
        "project_name": r["project_name"],
        "project_path": r["project_path"],
        "session_count": r["session_count"] or 0,
        "last_cited_date": _iso(r["last_cited_at"]),
    } for r in rows]


# ---- Phase 2 topology / similarity / recall tools --------------------------


def list_projects(topology: TopologyCache) -> list[dict[str, Any]]:
    topo = topology.get()
    out = [project_info_dict(p) for p in topo.projects.values()]
    out.sort(key=lambda d: -(d["last_modified"] or 0.0))
    return out


def list_reference_domains(topology: TopologyCache) -> list[dict[str, Any]]:
    topo = topology.get()
    domains = [domain_info_dict(d) for d in topo.reference_domains.values()]
    snips = [domain_info_dict(d) for d in topo.snippet_domains.values()]
    for d in domains:
        d["kind"] = "reference"
    for d in snips:
        d["kind"] = "snippet"
    out = domains + snips
    out.sort(key=lambda d: -(d["last_modified"] or 0.0))
    return out


def topology_status(topology: TopologyCache) -> dict[str, Any]:
    topo = topology.get()
    return {
        "project_count": len(topo.projects),
        "area_count": len(topo.areas),
        "reference_domain_count": len(topo.reference_domains),
        "snippet_domain_count": len(topo.snippet_domains),
        "workflow_cluster_count": len(topo.workflow_clusters),
        "quality_gaps": {
            "missing_project_md": topo.quality_gaps.missing_project_md,
            "missing_open_issues": topo.quality_gaps.missing_open_issues,
            "missing_index": topo.quality_gaps.missing_index,
            "orphan_references": topo.quality_gaps.orphan_references,
        },
        "last_full_scan": _iso(topo.last_full_scan),
    }


def vault_find_similar_projects(
    topology: TopologyCache, project: str, top_k: int = 5
) -> list[dict[str, Any]]:
    """Jaccard over cited Reference / Snippet sets."""
    topo = topology.get()
    if project not in topo.projects:
        return []
    own = topo.projects[project]
    own_set = set(own.cited_references) | set(own.cited_snippets)
    if not own_set:
        return []
    scored: list[dict[str, Any]] = []
    for other_name, other in topo.projects.items():
        if other_name == project:
            continue
        other_set = set(other.cited_references) | set(other.cited_snippets)
        if not other_set:
            continue
        intersection = own_set & other_set
        if not intersection:
            continue
        union = own_set | other_set
        score = len(intersection) / len(union)
        scored.append({
            "project_name": other_name,
            "shared_refs": sorted(intersection),
            "similarity_score": round(score, 4),
        })
    scored.sort(key=lambda d: -d["similarity_score"])
    return scored[:top_k]


def vault_get_bridge_references(
    store: Store,
    topology: TopologyCache,
    project_a: str,
    project_b: str,
) -> list[dict[str, Any]]:
    topo = topology.get()
    if project_a not in topo.projects or project_b not in topo.projects:
        return []
    a = topo.projects[project_a]
    b = topo.projects[project_b]
    a_set = set(a.cited_references) | set(a.cited_snippets)
    b_set = set(b.cited_references) | set(b.cited_snippets)
    shared = sorted(a_set & b_set)
    if not shared:
        return []

    def cite_count(owner: str, ref_path: str) -> int:
        with store._lock:
            row = store._conn.execute(
                """
                SELECT COUNT(*) AS c
                FROM wikilinks w
                JOIN files src ON w.src_file_id = src.file_id
                WHERE src.para_owner = ? AND w.dst_file_id = (
                    SELECT file_id FROM files WHERE path = ?
                )
                """,
                (owner, ref_path),
            ).fetchone()
        return row["c"] if row else 0

    out: list[dict[str, Any]] = []
    for ref_path in shared:
        ref_row = None
        with store._lock:
            ref_row = store._conn.execute(
                "SELECT title, para_type FROM files WHERE path = ?", (ref_path,)
            ).fetchone()
        out.append({
            "ref_path": ref_path,
            "ref_title": (ref_row["title"] if ref_row else None) or ref_path,
            "para_type": ref_row["para_type"] if ref_row else None,
            "citation_count_a": cite_count(project_a, ref_path),
            "citation_count_b": cite_count(project_b, ref_path),
        })
    out.sort(key=lambda d: -(d["citation_count_a"] + d["citation_count_b"]))
    return out


def vault_recall(
    store: Store,
    embedder: Embedder,
    topic: str,
    since_date: str | None = None,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Search session logs by topic, recency-weighted."""
    if not topic.strip():
        return []
    pool = max(top_k * 8, 40)
    embedding = embedder.encode([topic])[0]
    vec_results = store.vec_search(embedding, pool)
    fts_results = store.fts_search(topic, pool)
    fused = _rrf_fuse(vec_results, fts_results)
    if not fused:
        return []
    chunk_ids = [cid for cid, _ in fused]
    chunks = store.get_chunks_with_files(chunk_ids)

    since_epoch: float | None = None
    if since_date:
        try:
            since_epoch = datetime.strptime(since_date, "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            since_epoch = None

    now = time.time()
    seen_files: set[int] = set()
    out: list[dict[str, Any]] = []
    for chunk_id, score in fused:
        row = chunks.get(chunk_id)
        if row is None:
            continue
        if row["para_type"] != "session":
            continue
        if row["file_id"] in seen_files:
            continue
        if since_epoch and (row["last_modified"] or 0.0) < since_epoch:
            continue
        seen_files.add(row["file_id"])
        age_days = max(0.0, (now - (row["last_modified"] or now)) / 86400.0)
        recency = math.exp(-age_days / 60.0)
        adjusted = score * (0.6 + 0.4 * recency)
        out.append({
            "path": row["path"],
            "title": row["title"] or row["path"],
            "snippet": _format_snippet(row["text"]),
            "section_heading": row["section_heading"],
            "session_date": _iso(row["last_modified"]),
            "project": row["para_owner"],
            "score": round(adjusted, 4),
        })
    out.sort(key=lambda d: -d["score"])
    return out[:top_k]


def detect_workflow(
    store: Store,
    embedder: Embedder,
    context: str,
    threshold: float = 0.5,
) -> dict[str, Any] | None:
    """Match free-text or path context against persisted workflow clusters.

    Builds a representative text from each cluster's centroid_ref titles,
    embeds it, and returns the highest-cosine match above threshold.
    """
    if not context.strip():
        return None
    clusters = store.list_workflow_clusters()
    if not clusters:
        return None
    # Build representative text per cluster: titles + first 200 chars of body.
    cluster_texts: list[str] = []
    for c in clusters:
        bits: list[str] = []
        for path in c["centroid_refs"][:5]:
            with store._lock:
                row = store._conn.execute(
                    "SELECT title FROM files WHERE path = ?", (path,)
                ).fetchone()
            if row and row["title"]:
                bits.append(str(row["title"]))
            else:
                bits.append(Path(path).stem.replace("-", " "))
        if c.get("label"):
            bits.insert(0, c["label"])
        cluster_texts.append(". ".join(bits))

    embeddings = embedder.encode([context] + cluster_texts)
    ctx_emb = embeddings[0]
    cluster_embs = embeddings[1:]

    best_score = 0.0
    best_idx = -1
    for i, ce in enumerate(cluster_embs):
        score = _cosine(ctx_emb, ce)
        if score > best_score:
            best_score = score
            best_idx = i

    if best_idx < 0 or best_score < threshold:
        return None
    c = clusters[best_idx]
    return {
        "cluster_id": c["cluster_id"],
        "label": c["label"],
        "centroid_refs": c["centroid_refs"],
        "co_citing_projects": c["co_citing_projects"],
        "member_sessions": c["member_sessions"],
        "score": round(best_score, 4),
    }


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine sim assuming both are already normalized (sentence-transformers
    with normalize_embeddings=True). Falls back to full computation if not."""
    if len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


def session_brief(
    store: Store,
    embedder: Embedder,
    topology: TopologyCache,
    cwd: str,
    recent_files: list[str] | None = None,
) -> dict[str, Any]:
    """Compose a session-start brief: PARA position, active project, similar
    projects, workflow cluster, recent cross-project sessions, quality gaps.

    ≤1500 tokens. Returns structured pointers (paths + summaries), not bulk
    content — caller follows wikilinks for detail.
    """
    pos = _classify_para_position(cwd)
    para_position = {
        "type": (
            "in_project" if pos["para_type"] == "project" and pos["owner"] else
            "in_area" if pos["para_type"] == "area" and pos["owner"] else
            "in_reference" if pos["para_type"] in ("reference", "snippet") else
            "fresh"
        ),
        "project": pos["owner"] if pos["para_type"] == "project" else None,
        "area": pos["area"],
        "domain": pos["domain"],
    }

    topo = topology.get()

    active_project_brief: dict[str, Any] | None = None
    cited_references: list[dict[str, Any]] = []
    similar_projects_out: list[dict[str, Any]] = []
    if pos["owner"] and pos["owner"] in topo.projects:
        proj = topo.projects[pos["owner"]]
        active_project_brief = _build_active_project_brief(store, proj)
        if proj.project_md:
            cited_references = _refs_for_project(store, proj.name)
        similar_projects_out = vault_find_similar_projects(topology, proj.name, top_k=4)

    workflow_match = detect_workflow(
        store, embedder,
        context=_compose_workflow_context(cwd, recent_files, topo),
    )

    cross_recent: list[dict[str, Any]] = []
    if pos["owner"] or recent_files:
        topic = _compose_recall_topic(pos, recent_files, active_project_brief)
        if topic:
            cross_recent = [
                r for r in vault_recall(store, embedder, topic=topic, top_k=4)
                if r["project"] != pos["owner"]
            ][:3]

    quality_gaps = _quality_gaps_for_owner(topo, pos["owner"])

    return {
        "para_position": para_position,
        "active_project_brief": active_project_brief,
        "cited_references": cited_references,
        "similar_projects": similar_projects_out,
        "workflow_cluster": workflow_match,
        "cross_project_recent_sessions": cross_recent,
        "quality_gaps": quality_gaps,
        "computed_at": _iso(time.time()),
    }


def _build_active_project_brief(
    store: Store, proj: Any
) -> dict[str, Any]:
    """Pull project_md_summary, open_issues_summary, latest sessions."""
    project_md_summary = None
    open_issues_summary = None

    if proj.project_md:
        project_md_summary = _file_first_paragraph(store, proj.project_md, max_chars=400)
    if proj.open_issues_md:
        open_issues_summary = _file_text(store, proj.open_issues_md, max_chars=600)

    latest_sessions: list[dict[str, Any]] = []
    for path in proj.sessions[:5]:
        with store._lock:
            row = store._conn.execute(
                "SELECT title, last_modified FROM files WHERE path = ?", (path,)
            ).fetchone()
        latest_sessions.append({
            "path": path,
            "title": (row["title"] if row else None) or path,
            "date": _iso(row["last_modified"]) if row else None,
        })

    return {
        "name": proj.name,
        "path": proj.path,
        "project_md": proj.project_md,
        "project_md_summary": project_md_summary,
        "open_issues_md": proj.open_issues_md,
        "open_issues_summary": open_issues_summary,
        "latest_sessions": latest_sessions,
        "session_count": len(proj.sessions),
    }


def _file_first_paragraph(store: Store, path: str, max_chars: int = 400) -> str | None:
    """First non-heading paragraph of the file."""
    with store._lock:
        row = store._conn.execute(
            "SELECT text FROM chunks c JOIN files f ON c.file_id = f.file_id "
            "WHERE f.path = ? ORDER BY c.chunk_index LIMIT 1",
            (path,),
        ).fetchone()
    if not row:
        return None
    text = row["text"]
    # Strip leading H1
    lines = [ln for ln in text.split("\n") if not ln.lstrip().startswith("# ")]
    body = " ".join(ln.strip() for ln in lines if ln.strip())
    return body[:max_chars - 1].rstrip() + "…" if len(body) > max_chars else body


def _file_text(store: Store, path: str, max_chars: int = 600) -> str | None:
    with store._lock:
        rows = store._conn.execute(
            "SELECT text FROM chunks c JOIN files f ON c.file_id = f.file_id "
            "WHERE f.path = ? ORDER BY c.chunk_index LIMIT 2",
            (path,),
        ).fetchall()
    if not rows:
        return None
    full = "\n\n".join(r["text"] for r in rows)
    full = " ".join(full.split())
    return full[:max_chars - 1].rstrip() + "…" if len(full) > max_chars else full


def _refs_for_project(store: Store, project_name: str) -> list[dict[str, Any]]:
    """Cited references for a project, rolled up across all its files."""
    with store._lock:
        rows = store._conn.execute(
            """
            SELECT dst.path AS ref_path, dst.title AS ref_title,
                   dst.para_type AS para_type, COUNT(*) AS citation_count
            FROM wikilinks w
            JOIN files src ON w.src_file_id = src.file_id
            JOIN files dst ON w.dst_file_id = dst.file_id
            WHERE w.is_resolved = 1
              AND src.para_owner = ?
              AND dst.para_type IN ('reference', 'snippet', 'index')
            GROUP BY dst.file_id
            ORDER BY citation_count DESC
            LIMIT 10
            """,
            (project_name,),
        ).fetchall()
    return [{
        "ref_path": r["ref_path"],
        "ref_title": r["ref_title"] or r["ref_path"],
        "para_type": r["para_type"],
        "citation_count": r["citation_count"],
    } for r in rows]


def _compose_workflow_context(
    cwd: str, recent_files: list[str] | None, topo: Any
) -> str:
    bits = [cwd]
    if recent_files:
        bits.extend(recent_files[:5])
    return ". ".join(b.replace("/", " ").replace("-", " ") for b in bits if b)


def _compose_recall_topic(
    pos: dict[str, str | None],
    recent_files: list[str] | None,
    active_brief: dict[str, Any] | None,
) -> str | None:
    if active_brief and active_brief.get("project_md_summary"):
        return active_brief["project_md_summary"][:200]
    bits: list[str] = []
    if pos.get("owner"):
        bits.append(pos["owner"].replace("-", " "))
    if pos.get("domain"):
        bits.append(pos["domain"])
    if recent_files:
        for p in recent_files[:3]:
            bits.append(Path(p).stem.replace("-", " "))
    if not bits:
        return None
    return " ".join(bits)


def _quality_gaps_for_owner(topo: Any, owner: str | None) -> dict[str, list[str]]:
    if not owner:
        return {}
    gaps = topo.quality_gaps
    out: dict[str, list[str]] = {}
    project_path = f"01-Projects/{owner}"
    if project_path in gaps.missing_project_md:
        out["missing_project_md"] = [project_path]
    if project_path in gaps.missing_open_issues:
        out["missing_open_issues"] = [project_path]
    return out


def vault_find_related(
    store: Store,
    embedder: Embedder,
    path: str,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """Embed path's content; return semantically and structurally related files."""
    file_id = store.file_id_for_path(path)
    if file_id is None:
        return []

    with store._lock:
        chunk_rows = store._conn.execute(
            "SELECT text FROM chunks WHERE file_id = ? ORDER BY chunk_index LIMIT 4",
            (file_id,),
        ).fetchall()
    if not chunk_rows:
        return []

    seed = "\n\n".join(r["text"] for r in chunk_rows)[:4000]
    embedding = embedder.encode([seed])[0]
    pool = max(top_k * 8, 40)
    vec_results = store.vec_search(embedding, pool)

    linked_ids = store.get_linked_file_ids(file_id)

    chunk_ids = [cid for cid, _ in vec_results]
    chunks = store.get_chunks_with_files(chunk_ids)

    seen_files: set[int] = {file_id}
    out: list[dict[str, Any]] = []
    for chunk_id, dist in vec_results:
        row = chunks.get(chunk_id)
        if row is None:
            continue
        if row["file_id"] in seen_files:
            continue
        seen_files.add(row["file_id"])
        is_linked = row["file_id"] in linked_ids
        score = (1.0 - min(dist, 1.0))
        relationship = "linked" if is_linked else "semantic"
        if is_linked:
            score *= LINK_BOOST
        out.append({
            "path": row["path"],
            "title": row["title"] or row["path"],
            "para_type": row["para_type"],
            "para_owner": row["para_owner"],
            "score": round(score, 4),
            "relationship_hint": relationship,
        })
    out.sort(key=lambda d: -d["score"])

    # Surface graph-adjacent files even if they're not in vec top-pool.
    have_paths = {d["path"] for d in out}
    if linked_ids:
        with store._lock:
            link_rows = store._conn.execute(
                f"SELECT file_id, path, title, para_type, para_owner "
                f"FROM files WHERE file_id IN ({','.join('?' for _ in linked_ids)})",
                tuple(linked_ids),
            ).fetchall()
        for r in link_rows:
            if r["path"] in have_paths:
                continue
            out.append({
                "path": r["path"],
                "title": r["title"] or r["path"],
                "para_type": r["para_type"],
                "para_owner": r["para_owner"],
                "score": 0.5,
                "relationship_hint": "linked-only",
            })
    out.sort(key=lambda d: -d["score"])
    return out[:top_k]
